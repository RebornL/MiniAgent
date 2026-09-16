"""跨包集成：CLI 审批者（`app.cli`）——`run_command` 的人工放行 / 拒绝。

issue #12 的验收都在这里：审批请求给出**完整 argv 与 cwd**；放行才执行；拒绝沿用既有的
`denied` 结果通道且工具体未执行；**非交互输入（非 TTY / 管道 / EOF）不得默认放行**；
审批者不改变其它工具的行为。交互路径用注入的输入真跑一遍，不必起真 TTY。

审批闸门跑在**装配好的**流水线上（`app.assembly` 的 `ask` + `cli` 的审批者）；只有
`run_command` 的**工具体**换成替身（执行即在 cwd 写标记文件）——本票验的是闸门本身，
而真起外部命令是 `tests/test_shell.py` 的事（也免得与进程后端的改动耦合）。

运行：`python -m pytest tests/test_cli_approval.py -v`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app import assembly
from app.assembly import build_harness
from app.cli import APPROVAL_SCOPE, cli_approver, install_approver
from capabilities.persistence.provider import PersistenceManager, Store
from capabilities.shell.definition import RUN_COMMAND_NAME
from miniharness.core import Context
from miniharness.loop import Loop
from miniharness.tools.contract import DENIED, OK, ToolDefinition
from providers.mock import MockLLM


#: 带空格与引号的 argv：证明审批请求展示的是**完整**参数，而不是被截断的摘要。
ARGV = [sys.executable, "-c", "print('hello world')", "a b"]


def _assembled(tmp_path, approver):
    """装配整个 app 的 harness，按 cli 的方式装上审批者，并给 `run_command` 换替身工具体。

    替身执行时在 `cwd`（模型给的参数）写标记文件——于是「工具体到底跑没跑」可观察：
    放行才写得出，拒绝写不出来。返回 `(session, loop, marker)`。
    """
    llm = (MockLLM()
           .then_tool_call(APPROVAL_SCOPE, {"argv": ARGV, "cwd": str(tmp_path)})
           .then_text("完成"))
    ctx, session, loop = build_harness(session_id="s1",
                                       store_dir=str(tmp_path), llm=llm)
    install_approver(ctx, approver)
    marker = tmp_path / "ran.txt"

    def execute(args: dict) -> str:
        # 把真正拿到的 argv 落成标记：**批准的就是执行的**（顺带证明工具体到底跑没跑）
        (Path(args.get("cwd") or ".") / "ran.txt").write_text(
            json.dumps(args.get("argv")), encoding="utf-8")
        return "ran"

    ctx.get("tools").register(ToolDefinition(APPROVAL_SCOPE, "替身：执行即写标记文件",
                                             {"argv": {"type": "array"}, "cwd": {"type": "string"}},
                                             execute))
    return session, loop, marker


def _events(session, type_: str) -> list[dict]:
    return [event for event in session.events if event["type"] == type_]


def test_approval_shows_full_argv_and_cwd_and_then_runs_the_tool(tmp_path, capsys):
    """放行路径：人先看到完整 argv 与 cwd，回答 y 后工具体才真的跑。"""
    # 测试进程里没有真 TTY，"在终端前"与回答两半都注入（见 issue #12 的验收说明）
    session, loop, marker = _assembled(tmp_path, cli_approver(prompt=lambda _: "y",
                                                             interactive=lambda: True))

    outcome = loop.turn("跑一下")

    assert outcome["text"] == "完成"
    assert json.loads(marker.read_text(encoding="utf-8")) == ARGV   # 放行 → 工具体执行，且执行的就是批准的那份 argv
    assert [e["status"] for e in _events(session, "tool/result")
            if e["name"] == APPROVAL_SCOPE] == [OK]

    shown = capsys.readouterr().out
    assert Path(sys.executable).name in shown       # 可执行文件可见
    assert "print('hello world')" in shown          # 带引号的参数原样可见
    assert "a b" in shown                           # 带空格的参数没被切开或省略
    assert repr(str(tmp_path)) in shown             # 生效的 cwd 可见（以转义形式显示）


def test_a_rejected_command_never_runs(tmp_path):
    """拒绝路径：工具体不执行，结局沿既有结果通道为 `denied`。"""
    session, loop, marker = _assembled(tmp_path, cli_approver(prompt=lambda _: "",
                                                             interactive=lambda: True))

    outcome = loop.turn("跑一下")

    assert not marker.exists()                                  # 拒绝 → 工具体没跑
    assert not [e for e in _events(session, "tool/result") if e["name"] == APPROVAL_SCOPE]
    denied = _events(session, "tool/denied")
    assert len(denied) == 1 and denied[0]["name"] == APPROVAL_SCOPE
    assert outcome["status"] == "denied"                        # 结局沿既有结果通道为 denied
    assert outcome["text"] == denied[0]["reason"] and APPROVAL_SCOPE in outcome["text"]


def test_non_tty_input_is_denied_without_asking_or_running(tmp_path):
    """非 TTY（管道 / 重定向）：不询问、不执行，直接拒绝——不得默认放行。"""
    def forbidden(_: str) -> str:
        raise AssertionError("非交互输入不应被询问")

    session, loop, marker = _assembled(tmp_path, cli_approver(prompt=forbidden,
                                                             interactive=lambda: False))

    loop.turn("跑一下")

    assert not marker.exists()
    assert [e["name"] for e in _events(session, "tool/denied")] == [APPROVAL_SCOPE]


def test_eof_on_the_approval_prompt_is_denied(tmp_path):
    """读到 EOF（输入流已结束）：拒绝，不执行。"""
    def eof(_: str) -> str:
        raise EOFError

    session, loop, marker = _assembled(tmp_path, cli_approver(prompt=eof,
                                                             interactive=lambda: True))

    loop.turn("跑一下")

    assert not marker.exists()
    assert [e["name"] for e in _events(session, "tool/denied")] == [APPROVAL_SCOPE]


def test_the_default_approver_denies_a_pipe_without_reading_it(monkeypatch):
    """生产默认路径（不注入任何东西）：`stdin` 是管道 → 拒绝，且不读取那根管道。"""
    class _Pipe:
        def isatty(self) -> bool:
            return False

        def readline(self) -> str:
            raise AssertionError("非交互输入不应被读取")

    monkeypatch.setattr(sys, "stdin", _Pipe())
    # 后继被调用会放行：用它证明审批者自己拒绝了，而不是把 run_command 甩给后继
    decision = cli_approver()({"call": {"name": APPROVAL_SCOPE}, "args": {"argv": ARGV}},
                              lambda: {"kind": "allow"})

    assert decision["kind"] == "deny"
    assert APPROVAL_SCOPE in decision["reason"]


def test_installing_the_approver_leaves_other_tools_unapproved(tmp_path):
    """审批者只裁决 `run_command`：其它工具不经审批、不询问，仍按既有 `allow` 执行。"""
    asked: list[str] = []

    def prompt(text: str) -> str:
        asked.append(text)
        return "n"

    ctx, _, loop = build_harness(session_id="s1", store_dir=str(tmp_path),
                                 llm=MockLLM())
    install_approver(ctx, cli_approver(prompt=prompt))
    ctx.get("skills").load("calculator")

    result = ctx.get("tools").run({"id": "c1", "name": "calculate",
                                   "args": {"expression": "6*7"}})

    assert result["status"] == OK and result["content"] == "42"
    assert asked == []


def test_open_harness_hands_back_the_context_the_cli_wires_approval_on(tmp_path):
    """A1：公开装配入口交出 `(loop, ctx)`——CLI 的接线点是 ctx，不借道 `Loop` 的私有面。"""
    pm = PersistenceManager(Store(str(tmp_path)))
    loop, ctx = assembly.open_harness(
        pm, "s1", store_dir=str(tmp_path),
        base_system_prompt="", llm=MockLLM())

    assert isinstance(loop, Loop) and isinstance(ctx, Context)

    asked: list[str] = []
    unsubscribe = install_approver(ctx, cli_approver(
        prompt=lambda text: asked.append(text) or "n", interactive=lambda: True))
    # 装在返回的 ctx 上就是真的接线：这条总线上的审批请求会被问到
    decision = ctx.waterfall("tools/approve",
                             {"call": {"name": RUN_COMMAND_NAME}, "args": {"argv": ["x"]}},
                             lambda payload: {"kind": "deny"})

    assert decision["kind"] == "deny" and asked
    unsubscribe()


def test_the_approval_gate_covers_the_name_exported_by_the_shell_definition(tmp_path):
    """A2：审批名单与工具定义同源——装配层若按别的名字开闸，这条会红。"""
    ctx, _, _ = build_harness(session_id="s1", store_dir=str(tmp_path),
                              llm=MockLLM())
    asked: list[str] = []
    install_approver(ctx, cli_approver(prompt=lambda text: asked.append(text) or "n",
                                       interactive=lambda: True))
    ctx.get("skills").load("shell")

    result = ctx.get("tools").run({"id": "c1", "name": RUN_COMMAND_NAME,
                                   "args": {"argv": [sys.executable, "-c", "print(1)"]}})

    assert asked, f"审批门没有对定义导出的工具名 {RUN_COMMAND_NAME!r} 生效"
    assert result["status"] == DENIED


def test_the_approval_box_escapes_control_characters_in_cwd(capsys):
    """A3：cwd 是模型可控字符串——它不能往批准框里塞真换行伪造出一行显示。"""
    forged = "ok\n🔐 需要批准执行命令（伪造的一行）"

    decision = cli_approver(prompt=lambda _: "n", interactive=lambda: True)(
        {"call": {"name": APPROVAL_SCOPE}, "args": {"argv": ARGV, "cwd": forged}},
        lambda: {"kind": "allow"})

    shown = capsys.readouterr().out
    assert decision["kind"] == "deny"
    assert "ok\n🔐 需要批准执行命令" not in shown       # 伪造的整行没有出现
    assert "'ok\\n" in shown                            # cwd 以转义形式显示
