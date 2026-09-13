"""跨包集成：CLI 取消源（`app.cli.InterruptSource`）——回合执行期间的中断＝一次取消请求。

issue #13 的验收都在这里：

- 长命令执行中中断 → 结构化 `cancelled`、整棵进程树（含后代）**由系统侧独立确认**真的没了，
  本回合就此结束（不再采样），会话照常继续；
- 取消与超时共用同一条路径——这里验的是「请求取消」那一半，#10 的终止路径在
  `tests/test_timeout.py`；
- `input()` 提示处的 Ctrl-C 仍是既有行为（退出聊天循环）；
- 没有命令在跑时：只作用于当前回合（不再执行新的工具调用），不退出聊天循环；
- **取消粘到本轮**：退避期间的中断之后，重试重入 `tools/execute` 也不得再起进程树；取消之后
  模型只回的文本不得当成一次正常答复（本轮以取消说明收场，模型可见历史与日志都写着）；
- 取消守卫跑在审批**之前**（`tools/guard`）：本轮已取消时，审批名单里的工具既不执行、也不弹框
  ——挂在 `tools/pre-execute` 上会被审批策略的 `ask` 短路掉（那条复现路径见对应的用例）；
- 边界态：中断在工具收尾之后到达（结果不被改写，但本轮仍以取消收场）、连续两次中断（幂等）。

**中断时刻可注入**：`InterruptSource.interrupt()` 就是「中断到达的那一刻」，测试直接调它——
不起真 TTY、也不绑死信号；信号那一半（SIGINT → 中断、装上 / 还原）用注入的 `install` seam 验，
另有一条**真实子进程**用例把真 SIGINT（`signal.raise_signal`）递给正在跑的命令。

进程死活问操作系统（`providers.process.probe._alive` / `_assert_gone`，全仓唯一一份实现），
不采信被测实现自己的返回值；轮询 helper 与装配 helper 同在 `tests/support.py`（§6 一份）。

运行：`python -m pytest tests/test_cli_cancel.py -v`
"""
from __future__ import annotations

import builtins
import json
import signal
import sys
import threading
import time
from typing import Any, Callable

from app import assembly, cli
from app.cli import INTERRUPT_REASON, InterruptSource, chat_loop
from capabilities.permission.provider import PermissionPlugin
from capabilities.retry.provider import RetryPlugin
from capabilities.shell.definition import RUN_COMMAND_NAME
from capabilities.timeout.provider import ToolTimeoutPlugin, TurnCancelPlugin
from miniharness.core import Context
from miniharness.loop import Loop
from miniharness.tools.contract import CANCELLED, OK, ToolDefinition
from providers.mock import MockLLM
from providers.process import SubprocessRange, SubprocessSeam
from providers.process.probe import _assert_gone
from tests.support import _marker_command, _shell_harness, _wait_until

#: 进程树剧本：组长派生一个长睡后代、把两个 pid 写进 argv[1]，然后两个一起长睡。
_TREE_SCRIPT = """\
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}")
time.sleep(300)
"""

#: 一条真实、快速、输出可判的命令：证明取消之后进程型工具照常可用。
_QUICK = [sys.executable, "-c", "print('after')"]


class _Watcher:
    """后台线程里的一次注入：它抛的错由调用方 `join()` 后重抛（线程里的断言不会自己冒出来）。"""

    def __init__(self, action: Callable[[], None]) -> None:
        self._failures: list[BaseException] = []

        def run() -> None:
            try:
                action()
            except BaseException as exc:          # noqa: BLE001 - 交给主线程断言
                self._failures.append(exc)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def join(self, timeout_s: float = 30.0) -> None:
        self._thread.join(timeout_s)
        assert not self._thread.is_alive(), "注入线程没有收场"
        if self._failures:
            raise self._failures[0]


def _chat(monkeypatch, tmp_path, llm: MockLLM, inputs: list[str], *,
          interrupts: InterruptSource, on_open: Callable[[Context], None] | None = None
          ) -> Context:
    """跑一遍真实的 `chat_loop`：`input()` 按剧本喂、审批一律放行，返回装配出的 Context。

    `input` 是生产路径上唯一读人的地方：脚本化它就等于有个脚本化的终端（本票验的是中断，不是
    终端行为）。`run_command` 的审批另需人放行，这里换成一律放行的审批者——本票验的也不是审批
    （那是 `tests/test_cli_approval.py` 的事）。
    """
    typed = list(inputs)
    captured: list[Context] = []
    real_install, real_open = cli.install_approver, assembly.open_harness

    def fake_input(prompt: str = "") -> str:
        if not typed:
            raise EOFError                        # 剧本走完：沿用既有行为（EOF → 再见）
        return typed.pop(0)

    def open_capturing(*args: Any, **kwargs: Any):
        loop, ctx = real_open(*args, **kwargs)
        captured.append(ctx)
        if on_open is not None:
            on_open(ctx)
        return loop, ctx

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(cli, "install_approver",
                        lambda ctx: real_install(ctx, lambda payload, next_: {"kind": "allow"}))
    monkeypatch.setattr(assembly, "open_harness", open_capturing)
    chat_loop(model="mock", session_id="s1", store_dir=str(tmp_path),
              llm=llm, interrupts=interrupts)
    return captured[0]


def _results(ctx: Context) -> list[dict]:
    return [event for event in ctx.get("session").events if event["type"] == "tool/result"]


def _command_results(ctx: Context) -> list[dict]:
    """只看 `run_command` 的权威结果（装载技能之类的调用也落 `tool/result`）。"""
    return [event for event in _results(ctx) if event["name"] == RUN_COMMAND_NAME]


# ═══════════════ AC1 / AC2 / AC5：中断 → 终止 → 会话照常 ═══════════════
def test_an_interrupt_during_a_running_command_cancels_the_turn_and_kills_the_tree(
        monkeypatch, tmp_path, capsys):
    """长命令执行中注入中断 → 结构化 `cancelled`；进程树（含后代）真没了；下一次调用照常。"""
    script, pidfile = tmp_path / "tree.py", tmp_path / "tree.pid"
    script.write_text(_TREE_SCRIPT, encoding="utf-8")
    llm = (MockLLM()
           .then_tool_call("load_skill", {"name": "shell"})      # run_command 随技能装载才可见
           .then_tool_call(RUN_COMMAND_NAME,
                           {"argv": [sys.executable, str(script), str(pidfile)]})
           .then_tool_call(RUN_COMMAND_NAME, {"argv": _QUICK})
           .then_text("第二次也跑完了"))
    interrupts = InterruptSource()
    pids: list[int] = []

    def interrupt_when_running() -> None:
        """中断在**命令真的跑起来之后**到达：盯剧本写出的 pid 文件，再注入。"""
        _wait_until(pidfile.exists)
        pids.extend(int(pid) for pid in pidfile.read_text(encoding="utf-8").split())
        assert interrupts.interrupt("测试注入的中断")

    watcher = _Watcher(interrupt_when_running)
    ctx = _chat(monkeypatch, tmp_path, llm, ["跑一个长命令", "再来一次"],
                interrupts=interrupts)
    watcher.join()

    results = _command_results(ctx)
    assert [r["status"] for r in results] == [CANCELLED, OK]
    assert "被取消" in results[0]["content"] and "测试注入的中断" in results[0]["content"]
    # 取消即收尾：第一轮只采样两次（装载技能 → 长命令），没有「取消之后还去问模型」的第三次
    assert len(llm.calls) == 4, "第一轮 2 次（装载、长命令）；第二轮 2 次（命令、答复）"
    assert [e["status"] for e in ctx.get("session").events if e["type"] == "turn/end"] == ["done", "done"]
    assert "⏹️ 已取消本轮" in capsys.readouterr().out

    # 系统侧独立确认：组长与后代都不再存在（不采信被测实现自己的返回值）
    assert len(pids) == 2
    _assert_gone(*pids)

    # 会话照常继续：第二次调用真跑了一条命令，输出就是它的
    second = json.loads(results[1]["content"])
    assert results[1]["name"] == RUN_COMMAND_NAME
    assert second["exit_code"] == 0 and second["stdout"].strip() == "after"


# ═══════════════ AC6：提示处的 Ctrl-C 既有行为不变 ═══════════════
def test_a_sigint_at_the_prompt_still_exits_the_chat_loop(monkeypatch, tmp_path, capsys):
    """`input()` 提示处的 Ctrl-C 仍是退出聊天循环；窗口之外的中断不是取消请求。"""
    interrupts = InterruptSource()
    at_prompt: list[bool] = []

    def interrupt_then_raise(prompt: str = "") -> str:
        at_prompt.append(interrupts.interrupt("提示处的中断"))
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupt_then_raise)
    chat_loop(model="mock", session_id="s1", store_dir=str(tmp_path),
              llm=MockLLM().then_text("不应走到这一步"), interrupts=interrupts)

    assert "👋 再见" in capsys.readouterr().out
    assert at_prompt == [False] and not interrupts.cancelled


# ═══════════════ AC4：没有命令在跑时按 Ctrl-C ═══════════════
def test_an_interrupt_with_nothing_running_cancels_the_turn_without_exiting(
        monkeypatch, tmp_path, capsys):
    """没有命令在跑时：取消请求无处可终止，但本轮就此打住（不再执行新的工具调用），不退出。"""
    interrupts = InterruptSource()
    landed: list[bool] = []

    def interrupt_while_sampling(messages: list[dict]) -> dict:
        landed.append(interrupts.interrupt("采样期间的中断"))   # 此刻没有命令在跑
        return {"text": "", "tool_calls": [
            {"id": "c1", "name": "load_skill", "args": {"name": "calculator"}}]}

    llm = MockLLM([interrupt_while_sampling, {"text": "第二轮照常"}])
    ctx = _chat(monkeypatch, tmp_path, llm, ["第一轮", "第二轮"], interrupts=interrupts)

    assert landed == [False], "没有在跑的命令：取消请求无处可落"
    events = ctx.get("session").events
    assert not [e for e in events if e["type"] == "skill/loaded"], "新的工具调用没有执行"
    assert [e["reason"] for e in events if e["type"] == "tool/denied"] == [
        f"回合已取消（采样期间的中断）：工具未执行"]
    assert [e["status"] for e in events if e["type"] == "turn/end"] == ["denied", "done"]
    assert "回合已取消" in capsys.readouterr().out


# ═══════════════ 边界态：中断在工具收尾之后到达 ═══════════════
def test_a_late_interrupt_after_the_tool_finished_does_not_rewrite_the_result(
        monkeypatch, tmp_path, capsys):
    """结果已经做成：取消不改写它、也不误报取消——但它**粘到本轮**：本轮以取消收场。

    已经做成的工作不重写（那条 `tool/result` 仍是 `ok`）；可本轮余下部分就此打住：取消之后
    模型再回什么都算不上这一轮的答复。
    """
    interrupts = InterruptSource()
    landed: list[bool] = []

    def watch_result(ctx: Context) -> None:
        def on_result(payload: dict) -> None:
            if payload["call"]["name"] == RUN_COMMAND_NAME:      # 只看那条真命令的结果
                landed.append(interrupts.interrupt("迟到中断"))
        ctx.on("tools/result", on_result)

    llm = (MockLLM()
           .then_tool_call("load_skill", {"name": "shell"})      # run_command 随技能装载才可见
           .then_tool_call(RUN_COMMAND_NAME, {"argv": _QUICK})
           .then_text("跑完了"))
    ctx = _chat(monkeypatch, tmp_path, llm, ["跑一条快命令"], interrupts=interrupts,
                on_open=watch_result)

    assert landed == [False]                       # 没有在跑的调用：no-op
    assert [r["status"] for r in _command_results(ctx)] == [OK]   # 做成的结果不被改写
    events = ctx.get("session").events
    assert [e["status"] for e in events if e["type"] == "turn/end"] == ["done"]
    shown = capsys.readouterr().out
    assert "⏹️ 已取消本轮：迟到中断" in shown       # 本轮不是一次「什么都没发生」的正常收尾
    assert "跑完了" not in shown                    # 取消之后模型回的文本被换成取消说明


# ═══════════════ 边界态：连续两次中断（幂等） ═══════════════
def test_two_interrupts_in_one_turn_are_idempotent(tmp_path):
    """第二次中断落在正在收场的那次调用上：幂等——结局仍是一个 `cancelled`，进程照样不留。"""
    interrupts = InterruptSource()
    ctx, runtime = _shell_harness(plugin=ToolTimeoutPlugin(grace_ms=2000))
    llm = MockLLM().then_tool_call("spawn_and_wait", {}).then_text("取消后不应再采样")
    loop = Loop()
    ctx.load(loop)
    ctx.load(llm)
    ctx.load(TurnCancelPlugin())                   # 取消的收尾与守卫：生产装配里同源的那一个
    spawned: list[int] = []
    first: list[bool] = []
    second: list[bool] = []

    def body(args: dict) -> dict:
        range_ = ctx.get("process").spawn([sys.executable, "-c", "import time; time.sleep(300)"])
        spawned.append(range_.pid)
        code = range_.wait_for_exit()              # 第一次中断终止了它，这里才返回
        second.append(interrupts.interrupt("第二次中断"))   # 工具体还没静止：本次调用仍在跑
        return {"exit_code": code}

    runtime.register(ToolDefinition("spawn_and_wait", "", {}, body))

    def interrupt_when_running() -> None:
        _wait_until(lambda: bool(spawned))
        first.append(interrupts.interrupt("第一次中断"))

    watcher = _Watcher(interrupt_when_running)
    with interrupts.armed(ctx):
        answer = loop.turn("跑一个长命令")
    watcher.join()

    assert first == [True] and second == [True]    # 两次都落到了在跑的工具调用上
    assert [r["status"] for r in _results(ctx)] == [CANCELLED]   # 只终止一次、只有一个结局
    assert answer.startswith("⏹️ 已取消本轮")
    assert len(llm.calls) == 1
    _assert_gone(*spawned)


# ═══════════════ 信号那一半：装上 / 触发 / 还原 ═══════════════
def test_the_sigint_handler_is_armed_only_for_the_turn(monkeypatch, tmp_path, capsys):
    """用注入的 `install` seam 验（不真发信号）：每个回合装上 SIGINT 处理、退出即还原；
    处理函数一被触发，这次中断就是一次取消请求。"""
    previous = signal.default_int_handler
    installs: list[tuple[int, Any]] = []

    def fake_install(signum: int, handler: Any) -> Any:
        installs.append((signum, handler))
        return previous

    interrupts = InterruptSource(install=fake_install)
    fired: list[bool] = []

    def fire_sigint(messages: list[dict]) -> dict:
        installs[-1][1](signal.SIGINT, None)       # 相当于内核把 SIGINT 递进来
        fired.append(interrupts.cancelled)
        return {"text": "", "tool_calls": [
            {"id": "c1", "name": "load_skill", "args": {"name": "calculator"}}]}

    llm = MockLLM([fire_sigint, {"text": "第二轮照常"}])
    ctx = _chat(monkeypatch, tmp_path, llm, ["第一轮", "第二轮"], interrupts=interrupts)

    assert [signum for signum, _ in installs] == [signal.SIGINT] * 4   # 两回合：各装一次、还原一次
    assert installs[0][1] == installs[2][1]        # 每回合装的是同一个处理函数
    assert installs[1] == (signal.SIGINT, previous) and installs[3] == installs[1]
    assert fired == [True]                         # 触发即取消（窗口之内）
    assert [e["reason"] for e in ctx.get("session").events if e["type"] == "tool/denied"] == [
        f"回合已取消（{INTERRUPT_REASON}）：工具未执行"]
    assert interrupts.interrupt() is False          # 窗口之外：不再是取消请求


# ═══════════════ 真实 SIGINT：真子进程 + 及时性 ═══════════════
def test_a_real_sigint_during_a_running_command_is_a_cancel_request(monkeypatch, tmp_path):
    """真 SIGINT（`signal.raise_signal`：Ctrl-C 递给进程的那条真实路径，由**另一个线程**递出）
    在长命令执行中到达：不抛 `KeyboardInterrupt` 掀翻回合，而是终止命令、以 `cancelled` 收场，
    且**及时**（不等 30 秒时限）。"""
    script, pidfile = tmp_path / "tree.py", tmp_path / "tree.pid"
    script.write_text(_TREE_SCRIPT, encoding="utf-8")
    llm = (MockLLM()
           .then_tool_call("load_skill", {"name": "shell"})      # run_command 随技能装载才可见
           .then_tool_call(RUN_COMMAND_NAME,
                           {"argv": [sys.executable, str(script), str(pidfile)]})
           .then_text("取消后不应再采样"))
    interrupts = InterruptSource()                 # 生产路径：真装 SIGINT 处理
    pids: list[int] = []
    sent_at: list[float] = []

    def send_real_sigint() -> None:
        _wait_until(pidfile.exists)
        pids.extend(int(pid) for pid in pidfile.read_text(encoding="utf-8").split())
        sent_at.append(time.monotonic())
        signal.raise_signal(signal.SIGINT)         # 这就是 Ctrl-C

    watcher = _Watcher(send_real_sigint)
    ctx = _chat(monkeypatch, tmp_path, llm, ["跑一个长命令"], interrupts=interrupts)
    watcher.join()

    assert [r["status"] for r in _command_results(ctx)] == [CANCELLED]
    assert len(llm.calls) == 2, "取消即收尾：装载 + 长命令，没有第三次采样"
    assert time.monotonic() - sent_at[0] < 5, "取消不该等到时限（默认 30 秒）才被看见"
    _assert_gone(*pids)


# ═══════════════ F1：守卫必须跑在审批之前（`tools/guard`） ═══════════════
def test_a_cancelled_turn_refuses_an_approval_gated_tool_before_the_prompt(tmp_path):
    """本轮已取消（没有命令在跑，`interrupt()` 返回 False）→ 审批名单里的工具**既不执行、
    也不弹批准框**。

    复现路径：审批策略对 `run_command` 直接返回 `{"kind": "ask"}`、不调 `next_`——挂在
    `tools/pre-execute` 上的守卫会被这层瀑布短路，**永不被调用**（失败注入实测：批准框照弹；
    连 `tools/execute` 的入口复查也一并去掉时，命令真的会跑）。守卫因此挂在 `tools/guard`：
    它在 pre-execute 决策**之后**、审批**之前**运行，而且 guard 的监听器不参与瀑布短路；
    `deny` 比 `ask` 更严，单调收紧的语义不变。
    """
    interrupts = InterruptSource()
    asked: list[str] = []
    landed: list[bool] = []
    marker = tmp_path / "ran.txt"

    def request_after_interrupt(messages: list[dict]) -> dict:
        landed.append(interrupts.interrupt("采样期间的中断"))    # 此刻没有命令在跑
        return {"text": "", "tool_calls": [
            {"id": "c1", "name": RUN_COMMAND_NAME,
             "args": {"argv": _marker_command(marker)}}]}

    ctx, _ = _shell_harness()
    ctx.load(ToolTimeoutPlugin())                                       # `abort` 入口（取消的登记处）
    ctx.load(PermissionPlugin(approval_required={RUN_COMMAND_NAME}))   # 先注册：它才是短路的那层
    ctx.load(TurnCancelPlugin())
    ctx.on("tools/approve", lambda payload, next_: asked.append(
        payload["call"]["name"]) or {"kind": "allow"})   # 被问到就是失败：这里放行只为看住空洞
    llm = MockLLM([request_after_interrupt])
    ctx.load(llm)
    loop = Loop()
    ctx.load(loop)

    with interrupts.armed(ctx):
        answer = loop.turn("跑一条命令")

    events = ctx.get("session").events
    assert landed == [False], "没有在跑的命令：取消请求无处可落"
    assert asked == [], "本轮已取消：批准框不该弹（用户已经喊停）"
    assert not marker.exists(), "工具体没有执行：标记文件不该出现"
    assert [e["reason"] for e in events if e["type"] == "tool/denied"] == [
        "回合已取消（采样期间的中断）：工具未执行"]
    assert [e["status"] for e in events if e["type"] == "turn/end"] == ["denied"]
    assert answer == "回合已取消（采样期间的中断）：工具未执行"
    assert len(llm.calls) == 1, "取消即收尾：没有再去问模型"


# ═══════════════ F2：取消粘到本轮 ═══════════════
class _RecordingSeam(SubprocessSeam):
    """记下每一次 `spawn` 产出的受管范围：本票据它独立确认「取消之后没有再起进程」。"""

    def __init__(self) -> None:
        self.spawns: list[int] = []

    def spawn(self, argv, **kwargs) -> SubprocessRange:
        range_ = super().spawn(argv, **kwargs)
        self.spawns.append(range_.pid)
        return range_


def test_a_cancel_during_retry_backoff_starts_no_new_process(capsys):
    """退避期间的中断之后，重试重入 `tools/execute` 也不得再起进程树。

    重试是**重新进 `tools/execute`**（不重跑 pre-execute / guard），所以只看 guard 挡不住它：
    第 2 次尝试必须在超时护栏的**入口**就被收住。第 1 次尝试真起一个子进程、等它退出后抛一个
    可重试的瞬时故障 → 退避；中断在退避期间到达（没有在跑的调用）。
    """
    interrupts = InterruptSource()
    seam = _RecordingSeam()
    ctx, runtime = _shell_harness(seam=seam,
                                 plugin=RetryPlugin(max_retries=2, base_delay=1.0))
    ctx.load(ToolTimeoutPlugin(default_ms=60_000))   # 时限很长：结局只能来自取消
    ctx.load(TurnCancelPlugin())
    llm = MockLLM().then_tool_call("flaky", {}).then_text("取消后不应再采样")
    ctx.load(llm)
    loop = Loop()
    ctx.load(loop)
    finished: list[int] = []

    def body(args: dict) -> dict:
        range_ = ctx.get("process").spawn([sys.executable, "-c", "pass"])
        code = range_.wait_for_exit()              # 真等它退出：本用例不留活口
        finished.append(range_.pid)
        raise ConnectionError("瞬时故障")           # 瞬时故障 → 重试策略退避后重入 `tools/execute`

    runtime.register(ToolDefinition("flaky", "", {}, body))

    def interrupt_during_backoff() -> None:
        _wait_until(lambda: bool(finished))
        time.sleep(0.05)                           # 第 1 次尝试确实收场了，退避已经开始
        assert interrupts.interrupt("退避期间的中断") is False   # 没有在跑的调用

    watcher = _Watcher(interrupt_during_backoff)
    with interrupts.armed(ctx):
        answer = loop.turn("起一个会失败的进程型工具")
    watcher.join()

    assert len(seam.spawns) == 1, "取消之后不得再起进程树"
    assert seam.spawns == finished
    assert [r["status"] for r in _results(ctx)] == [CANCELLED]
    assert "退避期间的中断" in _results(ctx)[0]["content"]
    assert answer.startswith("⏹️ 已取消本轮")
    assert "次重试" in capsys.readouterr().out, "退避确实发生过：第 2 次尝试是被入口复查收住的"
    _assert_gone(*finished)


def test_a_cancel_after_sampling_does_not_let_plain_text_pass_as_the_turn_answer(
        monkeypatch, tmp_path, capsys):
    """取消之后模型只回文本：那条文本不得当成本轮答复——本轮以取消说明收场。

    这条路径没有工具结果，`agent/post-tool` 的收尾不会被派发，`Loop` 收到纯文本就自己写
    `turn/end=done` 并把它当答案返回。取消因此必须粘到**本轮**：模型可见历史（日志里的
    `assistant/message`）与用户看到的答复都写着这是一次取消。
    """
    interrupts = InterruptSource()
    landed: list[bool] = []

    def text_after_interrupt(messages: list[dict]) -> dict:
        landed.append(interrupts.interrupt("采样期间的中断"))
        return {"text": "这条文本不该当成本轮答复"}

    llm = MockLLM([text_after_interrupt])
    ctx = _chat(monkeypatch, tmp_path, llm, ["问一句"], interrupts=interrupts)

    assert landed == [False]
    events = ctx.get("session").events
    assert [e["content"] for e in events if e["type"] == "assistant/message"] == [
        "⏹️ 已取消本轮：采样期间的中断"], "模型可见历史要能分辨这轮是被人取消的"
    assert [e["status"] for e in events if e["type"] == "turn/end"] == ["done"]
    shown = capsys.readouterr().out
    assert "⏹️ 已取消本轮：采样期间的中断" in shown
    assert "不该当成本轮答复" not in shown
