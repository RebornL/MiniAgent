"""
一个最精简的 Agent 核心实现
依赖: pip install openai

入口已迁移到 miniharness：`build_harness` 装配「Session + 工具 + provider + 策略插件」，
`chat_loop` 每轮只调 `loop.turn(user_input)`。策略（压缩/重试/超时/校验/终结/持久化/追踪）
全部挂在事件 seam 上，循环零改动——见 `miniharness.py` 与 `miniharness_plugins.py`。
"""
import json
from pathlib import Path
from typing import Callable

from openai import OpenAI

from Persistence import PersistenceManager, Store
from SkillManager import Skill
from Structure import sanitize_output, sanitize_string
from miniharness import Context, LLM, Loop, Session, ToolRuntime
from miniharness_deepseek import DeepSeekProvider
from miniharness_plugins import (
    CompactionPlugin,
    FinalOutputPlugin,
    PersistenceConsumer,
    RetryPlugin,
    SkillRegistry,
    SystemPromptPlugin,
    ToolTimeoutPlugin,
    TraceConsumer,
    ValidationPlugin,
)


def _load_config() -> dict:
    """从 config.json 加载配置，文件不存在则报错提示"""
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件缺失: {config_path}\n"
            "请参考 README.md 创建 config.json，包含 base_url 和 api_key"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """惰性构造 client：只有真正要调真实 LLM 时才读 config.json。

    导入期不得触碰 config.json（它在 .gitignore 里，全新 clone 上不存在），
    否则 `import MiniAgent` 会直接 FileNotFoundError。
    """
    global _client
    if _client is None:
        config = _load_config()
        _client = OpenAI(base_url=config["base_url"], api_key=config["api_key"])
    return _client


# ─── 1. 定义工具 ─────────────────────────────────
# 工具就是一个函数 + 一段描述（给 LLM 看的）
def search_web(query: str) -> str:
    """模拟搜索工具，实际可接 Google/Bing API"""
    # 真实场景这里调 API，这里用假数据演示
    fake_db = {
        "北京天气": "北京今天晴，25°C，微风",
        "上海天气": "上海今天小雨，22°C",
    }
    if "北京天气" in query:
        return fake_db.get("北京天气")
    return fake_db.get(query, f"未找到'{query}'的相关结果")

def calculate(expression: str) -> str:
    """安全的数学计算"""
    try:
        # 只允许数字和基本运算符，防止代码注入
        allowed = set("0123456789+-*/().% ")
        if not all(c in allowed for c in expression):
            return "错误：表达式包含不允许的字符"
        return str(eval(expression))
    except Exception as e:
        return f"计算错误: {e}"

def read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取文件失败: {e}"

def write_file(path: str, content: str) -> str:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入 {path}（{len(content)} 字符）"
    except Exception as e:
        return f"写入文件失败: {e}"

# ─── 2. 工具表（LLM 通过描述知道有什么工具可用）───
# 工具名 → (描述, JSON Schema, 函数)：ToolRuntime 的定义与 Skill 的 tool 定义同源派生
TOOLS: dict[str, tuple[str, dict, Callable[..., str]]] = {
    "search_web": (
        "搜索互联网获取信息，输入中文关键词",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
        search_web,
    ),
    "calculate": (
        "执行数学计算，输入数学表达式",
        {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "数学表达式，如 '3*15+2'"}},
            "required": ["expression"],
        },
        calculate,
    ),
    "read_file": (
        "读取文件内容",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径"}},
            "required": ["path"],
        },
        read_file,
    ),
    "write_file": (
        "写入文件内容",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["path", "content"],
        },
        write_file,
    ),
}


def _skill_tool(name: str) -> dict:
    """Skill 的 tool 定义（OpenAI 形状），与 ToolRuntime 里的描述同源。"""
    description, parameters, _ = TOOLS[name]
    return {"type": "function",
            "function": {"name": name, "description": description, "parameters": parameters}}


def _skill_tool_map(*names: str) -> dict[str, Callable[..., str]]:
    """技能的工具名 → 函数（legacy 工具是 `fn(**args)`）。"""
    return {name: TOOLS[name][2] for name in names}


# final_output：模型用来交付结构化最终答案的终结工具
OUTPUT_TOOL_NAMES = {"final_output"}


def make_final_output_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "final_output",
            "description": (
                "以结构化格式输出最终答案。调用此工具表示回答完成。"
                "result 字段放结构化数据，summary 字段放给用户看的总结。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "object",
                        "description": "最终答案的 JSON 结构化数据",
                    },
                    "summary": {
                        "type": "string",
                        "description": "给用户看的一句话总结",
                    },
                },
                "required": ["result"],
            },
        },
    }

def final_output_handler(result: dict, summary: str = "") -> str:
    sanitize_output(result)       # 递归检测字符串值
    if summary:
        sanitize_string(summary)  # 检测 summary
    return json.dumps({"result": result, "summary": summary}, ensure_ascii=False)


def register_skills(skills: SkillRegistry) -> None:
    """注册 4 个技能：装载即把技能工具挂进 ToolRuntime（可逆），卸载即撤销。"""
    skills.register(Skill(
        name="web-search",
        description="互联网搜索能力",
        tools=[_skill_tool("search_web")],
        tool_map=_skill_tool_map("search_web"),
        system_prompt="你拥有搜索能力。遇到不确定的事实性问题，请先搜索再回答，不要猜测。",
    ))

    skills.register(Skill(
        name="calculator",
        description="数学计算能力",
        tools=[_skill_tool("calculate")],
        tool_map=_skill_tool_map("calculate"),
        system_prompt="你拥有计算能力。遇到数学计算请调用 calculate 工具，不要心算。",
    ))

    skills.register(Skill(
        name="file-ops",
        description="文件读写能力",
        tools=[_skill_tool("read_file"), _skill_tool("write_file")],
        tool_map=_skill_tool_map("read_file", "write_file"),
        system_prompt="你拥有文件读写能力。操作文件前请确认路径正确。",
    ))

    skills.register(Skill(
        name="structured-output",
        description="结构化输出能力",
        tools=[make_final_output_tool()],
        tool_map={"final_output": final_output_handler},
        system_prompt=(
            "回答问题时，请调用 final_output 以结构化格式输出最终结果。"
            "result 字段放 JSON 结构化数据，summary 字段放给用户看的自然语言总结。"
            "调用 final_output 后不要再返回其他文本。"
        ),
    ))


# ─── 3. 装配 miniharness ─────────────────────────
def build_harness(
    *,
    model: str = "deepseek-v4-flash",
    session_id: str | None = None,
    store_dir: str = "./agent_sessions",
    client: OpenAI | None = None,
    llm: LLM | None = None,
    base_system_prompt: str = "",
) -> tuple[Context, Session, Loop]:
    """装配 harness：Session + 工具 + LLM provider + 策略插件 + 日志消费者。

    策略全挂在事件 seam 上（Loop 零改动）：压缩 → `agent/pre-step`，
    重试/超时 → `tools/execute`，输出校验 → `tools/post-execute`，
    终结工具 → `agent/post-tool`，持久化/追踪 → Session 日志订阅。
    工具不预注册：只随技能装载经 `SkillRegistry` 可逆注册（`unload_skill` 即撤销），
    与 legacy `get_active_tools()`（meta tools + 仅已激活技能的工具）一致。
    不装 PermissionPlugin——legacy 没有审批，装了会改变行为。
    """
    ctx = Context()
    session = Session()
    ctx.provide("session", session)

    loop = Loop()
    ctx.load(loop)                    # 依赖未就绪 → 挂起，provider/工具齐后自动激活
    ctx.load(ToolRuntime())
    ctx.load(llm if llm is not None else _default_llm(client, model))

    ctx.load(CompactionPlugin())
    ctx.load(RetryPlugin())
    ctx.load(ToolTimeoutPlugin())
    ctx.load(ValidationPlugin())
    ctx.load(FinalOutputPlugin(OUTPUT_TOOL_NAMES))
    ctx.load(PersistenceConsumer(PersistenceManager(Store(store_dir)), session_id))
    ctx.load(TraceConsumer())

    skills = SkillRegistry()
    register_skills(skills)
    ctx.load(skills)
    ctx.load(SystemPromptPlugin(base_system_prompt))

    return ctx, session, loop


def _print_delta(text: str) -> None:
    """默认流式回调：边收边打印（与 legacy 的流式输出一致）。"""
    print(text, end="", flush=True)


def _default_llm(api: OpenAI | None, model: str) -> DeepSeekProvider:
    """默认 provider：用传入的 client，否则惰性构造（config.json 的凭据）。"""
    return DeepSeekProvider(api or _get_client(), model, on_delta=_print_delta)


def _open_harness(
    pm: PersistenceManager,
    session_id: str,
    *,
    model: str,
    store_dir: str,
    client: OpenAI | None,
    base_system_prompt: str,
    llm: LLM | None = None,
) -> Loop:
    """装配 harness 并接上持久化状态：messages / summary / active_skills 一并还原。"""
    ctx, session, loop = build_harness(model=model, session_id=session_id,
                                       store_dir=store_dir, client=client, llm=llm,
                                       base_system_prompt=base_system_prompt)
    state = pm.load_session(session_id)
    if state["messages"]:
        session.restore(state["messages"])
    if state["summary"]:
        ctx.get("compaction").context.restore(state["summary"])
    skills = ctx.get("skills")
    for name in state["active_skills"]:
        try:
            skills.load(name)
        except KeyError:      # 技能已不存在（改名/删除）→ 跳过，不影响恢复
            pass
    return loop


def resume_session(
    session_id: str,
    new_input: str,
    *,
    model: str = "deepseek-v4-flash",
    base_system_prompt: str = "",
    store_dir: str = "./agent_sessions",
    client: OpenAI | None = None,
    llm: LLM | None = None,
) -> str:
    """恢复历史会话并继续对话：装配 harness + 还原 messages/summary/active_skills 后跑一轮。"""
    pm = PersistenceManager(Store(store_dir))
    if not pm.load_messages(session_id):
        return f"❌ 会话 {session_id} 不存在或为空"
    loop = _open_harness(pm, session_id, model=model, store_dir=store_dir,
                         client=client, llm=llm,
                         base_system_prompt=base_system_prompt)
    return loop.turn(new_input)


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

    loop = _open_harness(pm, session_id, model=model, store_dir=store_dir,
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
            loop = _open_harness(
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
            loop = _open_harness(
                pm, session_id, model=model, store_dir=store_dir,
                client=client, base_system_prompt=base_system_prompt)
            print(f"✅ 已切换到 {session_id}（{len(state['messages'])} 条消息）")

        # ── 正常对话 ──
        else:
            answer = loop.turn(user_input)
            print(f"Agent: {answer}\n")


# ─── 4. 跑起来 ────────────────────────────────────
if __name__ == "__main__":
    base_prompt = (
        "你是一个有用的助手。"
        "你可以使用 load_skill 加载需要的技能模块，用 unload_skill 释放不再需要的模块。每次回答前先加载 structured-output"
        "遇到不确定的事实时，请先加载对应技能再操作，不要猜测。"
    )

    chat_loop(_get_client(), base_prompt, "deepseek-v4-flash")
