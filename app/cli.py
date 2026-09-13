"""app.cli —— 交互式对话循环。

每轮只调 `loop.turn(user_input)`；装配与恢复都交给 `app.assembly`。

本模块另外在 harness 上装两样东西，都是装配层的事（`Loop` 仍零策略）：

- `tools/approve` 审批者（`install_approver`）：装配层为 `run_command` 装了 `ask`，由这里的
  人看过**完整 argv 与 cwd** 后放行 / 拒绝。审批只在事件总线上裁决，不改工具流水线、不进 Loop；
  拒绝与非交互输入一律不放行，工具体不执行。
- **取消源**（`InterruptSource`）：**回合执行期间**的 Ctrl-C 不再是「整个进程退出」，而是一次
  取消请求——`ctx.get("abort").cancel(...)`（`ToolTimeoutPlugin`，与超时同一条终止路径）终止
  正在跑的受管范围，该回合以结构化的 `cancelled` 结局收场，聊天循环照常继续。

两处中断语义的分界（都写死在 `chat_loop` 里）：`input()` 提示处的 Ctrl-C 仍是
`KeyboardInterrupt` → 退出聊天循环（既有行为，不变）；回合执行期间的 Ctrl-C 由取消源接管 →
只作用于当前回合。

本类只做**信号装配**——装 / 还原 SIGINT、划出「回合执行期间」这个窗口、把窗口内的中断换成一次
取消请求。取消**之后**的收尾是策略，归能力层（`capabilities.timeout.provider.TurnCancelPlugin`）：
本轮不再执行新的工具调用、不再起进程树、不以一次看起来正常的 `done` 蒙混收场。没有命令在跑时
取消无处可终止，但本轮同样就此打住——这两句话的落点都在能力层，`Loop` 仍是零策略。
"""
import os
import signal
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from openai import OpenAI

from app import assembly
from capabilities.persistence.provider import PersistenceManager, Store
from capabilities.shell.definition import RUN_COMMAND_NAME
from miniharness.core import Context
from miniharness.llm.contract import LLM
from miniharness.loop import Loop


#: 本审批者裁决的工具：装配层只为它装了 `ask`（其余 `tools/approve` 请求转交后继，无人裁决即拒绝）。
#: 名字取自能力定义（`RUN_COMMAND_NAME`），与装配层的审批名单同源——不各自字面写一遍。
APPROVAL_SCOPE = RUN_COMMAND_NAME

#: 视为放行的回答（去空白、大小写不敏感）；其余一切回答都不放行。
_APPROVE_ANSWERS = frozenset({"y", "yes"})


# ═══════════════ 审批：run_command 的人工放行 / 拒绝 ═══════════════
def cli_approver(*, prompt: Callable[[str], str] = input,
                 interactive: Callable[[], bool] | None = None
                 ) -> Callable[[dict, Callable[[], Any]], dict]:
    """构造 `tools/approve` 监听器：让人看着 argv 与 cwd 决定放行 / 拒绝。

    这是**安全决定**，不是确认框，所以请求里给出完整 argv（逐项、保留引号）与生效的 cwd。
    只有明确回答 `y` / `yes` 才放行；以下情形一律拒绝，工具体不执行（工具体未启动，
    结局沿既有结果通道为 `denied`）：

    - `stdin` 不是终端（管道 / 重定向）——不询问、不读输入，直接拒绝；
    - 读到 EOF 或 Ctrl-C；
    - 任何其它回答（缺省就是拒绝）。

    不归本审批者裁决的工具名转交后继裁决。`prompt` / `interactive` 只为测试注入：
    生产路径用真实 `input` 与 `sys.stdin.isatty()`。

    注意：本审批者跑在**回合执行期间**，那时 SIGINT 由取消源接管（`InterruptSource`）——在
    这里按 Ctrl-C 不会让 `input` 抛 `KeyboardInterrupt`，而是登记一次取消请求（本轮不再执行新的
    工具调用）。拒绝照旧：回车（空回答）或任何非 `y` 回答都不放行。
    """
    def is_interactive() -> bool:
        if interactive is not None:
            return interactive()
        return sys.stdin is not None and sys.stdin.isatty()

    def approve(payload: dict, next_: Callable[[], Any]) -> dict:
        call = payload.get("call") or {}
        if call.get("name") != APPROVAL_SCOPE:
            return next_()
        print(_describe_request(payload.get("args") or {}))
        if not is_interactive():
            return _deny("stdin 不是交互终端（管道 / 重定向）")
        try:
            answer = prompt("批准执行? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return _deny("审批输入结束（EOF / 中断）")
        if answer.strip().lower() in _APPROVE_ANSWERS:
            return {"kind": "allow"}
        return _deny("用户未批准")

    return approve


def _describe_request(args: dict) -> str:
    """审批请求里足以判断的信息：完整 argv 与生效的 cwd（未指定即进程当前目录）。"""
    return "\n".join([
        "",
        f"🔐 需要批准执行命令（{APPROVAL_SCOPE}）",
        f"   argv: {args.get('argv')!r}",
        f"   cwd : {(args.get('cwd') or os.getcwd())!r}",
    ])


def _deny(reason: str) -> dict:
    """拒绝决定：打印给人看，原因沿既定结果通道交给 `ToolRuntime`（工具体不执行）。"""
    print(f"⛔ {reason}: {APPROVAL_SCOPE}")
    return {"kind": "deny", "reason": f"{reason}: {APPROVAL_SCOPE}"}


def install_approver(ctx: Context,
                     approver: Callable[[dict, Callable[[], Any]], dict] | None = None
                     ) -> Callable[[], None]:
    """在 harness 的事件总线上装 `tools/approve` 审批者，返回退订器。

    `assembly.open_harness` 把装配好的 `(loop, ctx)` 一并交出，事件总线就在 ctx 上——
    接线点是它，不必去够 `Loop` 的私有面。每开一次新 harness（`/new`、`/switch`）都要重新装。
    `approver` 缺省是读真实 stdin 的 `cli_approver()`，测试可注入替代实现。
    """
    return ctx.on("tools/approve",
                  cli_approver() if approver is None else approver)


# ═══════════════ 取消源：回合执行期间的中断 → 取消请求 ═══════════════
#: 取消请求的说明：进工具的中止结局（`cancelled`），人看得出是谁请求的。
INTERRUPT_REASON = "用户中断"


class InterruptSource:
    """取消源：把回合执行期间的中断变成一次取消请求（**只做信号装配**）。

    接线点是 `assembly.open_harness` 交出的 ctx（与审批者同一层）：`ctx.get("abort")` 就是
    `ToolTimeoutPlugin`——**终止整棵进程树、独立确认、等工具体静止全在它那里**；取消**粘住
    本轮**之后的收尾（不再执行新工具调用、不再起进程树、不以正常 `done` 收场）在
    `capabilities.timeout.provider.TurnCancelPlugin` 里。本类只发起请求，不另写一套终止或收尾
    逻辑：中断与超时共用同一条路径，区别只在结局码（`cancelled`）。

    **窗口**：只有 `armed(ctx)` 期间的中断才是取消请求。窗口之外 SIGINT 不被接管——`input()`
    提示处仍是 `KeyboardInterrupt`，既有行为不变（退出聊天循环）。

    **可注入**：`interrupt()` 就是「中断到达的那一刻」，测试直接调它，不必起真 TTY、也不真发
    信号；生产路径由 SIGINT 处理函数调它。`install` 是装信号处理器的 seam（缺省
    `signal.signal`）；`signal.signal` 只在主线程可用，非主线程里装不上，那时只剩注入入口。

    **没有命令在跑时**：取消请求无处可终止（`interrupt()` 返回 False）——本轮仍就此打住（能力
    层的收尾做到的），聊天循环**绝不**因回合期间的中断而退出（退出只发生在 `input()` 提示处）。
    """

    def __init__(self, *, install: Callable[[int, Any], Any] | None = None) -> None:
        self._install = signal.signal if install is None else install
        self._ctx: Context | None = None
        self._cancelled = False                # 本窗口内是否收到过中断

    @property
    def cancelled(self) -> bool:
        """本窗口（`armed` 之内）是否收到过中断。"""
        return self._cancelled

    # ── 中断到达的那一刻（生产：SIGINT；测试：直接调）──────
    def interrupt(self, reason: str = INTERRUPT_REASON) -> bool:
        """一次中断：请求取消当前在跑的工具调用；返回它是否落到了一个在跑的调用上。

        窗口之外（`input()` 提示处、回合之间）是 no-op：不登记、返回 False——「取消只在回合
        执行期间生效」在这里落实。窗口之内即使没有在跑的调用（`False`），本轮也已被标为取消
        （状态在 `ctx.get("abort")` 那里）：本轮余下的工具调用因此不再执行。
        """
        if self._ctx is None:
            return False
        self._cancelled = True
        abort = self._ctx.get("abort")          # 没装超时护栏的装配：没有取消入口
        return bool(abort is not None and abort.cancel(reason))

    def _on_signal(self, signum: int, frame: Any) -> None:
        """SIGINT：只登记取消请求，**不抛** `KeyboardInterrupt`。

        抛了就会在任意一条字节码上把 `loop.turn` 掀翻：正在跑的受管范围没人终止（#10 的终止
        路径被绕过，进程留成孤儿），而回合断在 `tool_calls` 与配对结果之间会让下一轮上行不完整。
        """
        self.interrupt()

    # ── 回合执行期间：接管 SIGINT ──────────────────────
    @contextmanager
    def armed(self, ctx: Context) -> Iterator["InterruptSource"]:
        """划定「回合执行期间」：接管 SIGINT，退出时还原。

        窗口只决定**中断算不算取消请求**（窗口之外 `interrupt()` 是 no-op）。取消之后的收尾
        不在这里装卸：它是能力层的策略（`TurnCancelPlugin`），状态由回合边界（`agent/pre-step`）
        清除——`armed` 因此只剩信号装配这一件事。
        """
        self._ctx = ctx
        self._cancelled = False
        installed, previous = self._arm_signal()
        try:
            yield self
        finally:
            if installed:
                self._install(signal.SIGINT, previous)
            self._ctx = None

    def _arm_signal(self) -> tuple[bool, Any]:
        """装上 SIGINT 处理：返回（是否装上，原处理函数）。非主线程装不上。"""
        try:
            return True, self._install(signal.SIGINT, self._on_signal)
        except ValueError:            # `signal.signal` 只在主线程可用：注入入口仍在
            return False, None


def chat_loop(
    client: OpenAI | None = None,
    base_system_prompt: str = "",
    model: str = "deepseek-v4-flash",
    session_id: str | None = None,
    store_dir: str = "./agent_sessions",
    llm: LLM | None = None,
    interrupts: InterruptSource | None = None,
):
    """
    交互式多轮对话：每轮走 harness 的 `loop.turn`。

    回合执行期间的 Ctrl-C 是**取消请求**（`InterruptSource`），不是退出：正在跑的命令被终止、
    该回合以 `cancelled` 结局收场，聊天循环照常继续。`input()` 提示处的 Ctrl-C 仍是退出聊天
    循环（既有行为，见模块说明）。`llm` / `interrupts` 只为测试注入（`llm` 与
    `assembly.open_harness` 的同一个 seam；`interrupts` 用来在指定时刻注入一次中断）。

    用法:
      >>> chat_loop(client, "你是助手...")
      You: 北京天气怎么样？
      Agent: 北京今天晴，25°C
      You: /exit
    """
    pm = PersistenceManager(Store(store_dir))
    interrupts = InterruptSource() if interrupts is None else interrupts

    # 如果没有传入 session_id，新建一个；传了则恢复该会话的历史
    if session_id is None:
        session_id = pm.new_session_id()
        print(f"🆕 新会话: {session_id}")
    else:
        print(f"📂 恢复会话: {session_id}")

    print("输入 /exit 退出，/history 查看历史会话，/switch <id> 切换会话\n")

    def _open(sid: str) -> tuple[Loop, Context]:
        """开一个 harness 并装上审批者——每次重开都是新的 Context，要重新装。"""
        opened, ctx = assembly.open_harness(
            pm, sid, model=model, store_dir=store_dir,
            client=client, llm=llm, base_system_prompt=base_system_prompt)
        install_approver(ctx)
        return opened, ctx

    loop, ctx = _open(session_id)

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见")
            break

        if not user_input:
            continue

        # ── /exit ──
        if user_input == "/exit":
            print("👋 再见")
            break

        # ── /help ──
        elif user_input == "/help":
            print("""
      命令:
        /exit              退出
        /history           查看所有历史会话
        /switch <id>       切换到指定会话
        /new               新建会话（放弃当前）
        /help              显示此帮助
      直接输入文字即可对话。
            """.strip())
            continue

        # ── /new ──
        elif user_input == "/new":
            session_id = pm.new_session_id()
            loop, ctx = _open(session_id)
            print(f"🆕 新会话: {session_id}")
            continue

        # ── /history ──
        elif user_input == "/history":
            sessions = pm.list_sessions()
            if not sessions:
                print("📭 暂无历史会话")
                continue
            for s in sessions:
                marker = " ← 当前" if s["id"] == session_id else ""
                print(f"  {s['id']} | {s['message_count']}条 | {s['updated']} | {s['last_message']}{marker}")
            continue

        # ── /switch ──
        elif user_input.startswith("/switch"):
            parts = user_input.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                print("⚠️ 用法: /switch <session_id>")
                print("   先用 /history 查看可用会话，再切换")
                continue

            new_id = parts[1].strip()

            if new_id == session_id:
                print(f"⚠️ 已经是当前会话: {session_id}")
                continue

            state = pm.load_session(new_id)
            if not state["events"]:
                print(f"❌ 会话 {new_id} 不存在或为空")
                continue

            session_id = new_id
            loop, ctx = _open(session_id)
            print(f"✅ 已切换到 {session_id}（{len(state['events'])} 条事件）")

        # ── 正常对话 ──
        else:
            with interrupts.armed(ctx):      # 回合执行期间：中断＝取消请求（装配层的收尾，Loop 零策略）
                answer = loop.turn(user_input)
            print(f"Agent: {answer}\n")
