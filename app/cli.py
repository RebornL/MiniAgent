"""app.cli —— 交互式对话循环。

每轮只调 `loop.turn(user_input)`；装配与恢复都交给 `app.assembly`。
"""
from openai import OpenAI

from app import assembly
from capabilities.persistence.definition import PersistenceManager, Store


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

    loop = assembly._open_harness(pm, session_id, model=model, store_dir=store_dir,
                         client=client, base_system_prompt=base_system_prompt)

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
            loop = assembly._open_harness(
                pm, session_id, model=model, store_dir=store_dir,
                client=client, base_system_prompt=base_system_prompt)
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
            if not state["messages"]:
                print(f"❌ 会话 {new_id} 不存在或为空")
                continue

            session_id = new_id
            loop = assembly._open_harness(
                pm, session_id, model=model, store_dir=store_dir,
                client=client, base_system_prompt=base_system_prompt)
            print(f"✅ 已切换到 {session_id}（{len(state['messages'])} 条消息）")

        # ── 正常对话 ──
        else:
            answer = loop.turn(user_input)
            print(f"Agent: {answer}\n")
