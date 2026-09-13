"""app.cli —— 交互式对话循环。

每轮只调 `loop.turn(user_input)`；装配与恢复都交给 `app.assembly`。

本模块另外在 harness 上装 `tools/approve` 审批者（`install_approver`）：装配层为
`run_command` 装了 `ask`，由这里的人看过**完整 argv 与 cwd** 后放行 / 拒绝。审批只在
事件总线上裁决，不改工具流水线、不进 Loop；拒绝与非交互输入一律不放行，工具体不执行。
"""
import os
import sys
from typing import Any, Callable

from openai import OpenAI

from app import assembly
from capabilities.persistence.definition import PersistenceManager, Store
from capabilities.shell.definition import RUN_COMMAND_NAME
from miniharness.core import Context
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


def chat_loop(
    client: OpenAI | None = None,
    base_system_prompt: str = "",
    model: str = "deepseek-v4-flash",
    session_id: str | None = None,
    store_dir: str = "./agent_sessions",
):
    """
    交互式多轮对话：每轮走 harness 的 `loop.turn`。

    用法:
      >>> chat_loop(client, "你是助手...")
      You: 北京天气怎么样？
      Agent: 北京今天晴，25°C
      You: /exit
    """
    pm = PersistenceManager(Store(store_dir))

    # 如果没有传入 session_id，新建一个；传了则恢复该会话的历史
    if session_id is None:
        session_id = pm.new_session_id()
        print(f"🆕 新会话: {session_id}")
    else:
        print(f"📂 恢复会话: {session_id}")

    print("输入 /exit 退出，/history 查看历史会话，/switch <id> 切换会话\n")

    def _open(sid: str) -> Loop:
        """开一个 harness 并装上审批者——每次重开都是新的 Context，要重新装。"""
        opened, ctx = assembly.open_harness(
            pm, sid, model=model, store_dir=store_dir,
            client=client, base_system_prompt=base_system_prompt)
        install_approver(ctx)
        return opened

    loop = _open(session_id)

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
            loop = _open(session_id)
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
            loop = _open(session_id)
            print(f"✅ 已切换到 {session_id}（{len(state['events'])} 条事件）")

        # ── 正常对话 ──
        else:
            answer = loop.turn(user_input)
            print(f"Agent: {answer}\n")
