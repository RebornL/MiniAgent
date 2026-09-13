"""app.assembly —— 装配：把骨架、能力与后端装成 harness。

`build_harness()` 装配 Session + 工具 + LLM provider + 策略插件 + 日志消费者；
`resume_session()` 在其上还原 messages / summary / active_skills 并续跑一轮。

策略全挂在事件 seam 上（Loop 零改动）：压缩 → `agent/pre-step`，重试 / 超时 → `tools/execute`，
输出校验 → `tools/post-execute`，终结工具 → `agent/post-tool`，持久化 / 追踪 → Session 日志订阅。
"""
from openai import OpenAI

from app import config, tools
from capabilities.compaction.provider import CompactionPlugin
from capabilities.final_output.provider import FinalOutputPlugin
from capabilities.persistence.definition import PersistenceManager, Store
from capabilities.persistence.provider import PersistenceConsumer
from capabilities.retry.provider import RetryPlugin
from capabilities.skills.consumer import SystemPromptPlugin
from capabilities.skills.definition import Skill
from capabilities.skills.provider import SkillRegistry
from capabilities.timeout.provider import ToolTimeoutPlugin
from capabilities.tracing.provider import TraceConsumer
from capabilities.validation.provider import ValidationPlugin
from miniharness.core import Context
from miniharness.llm.contract import LLM
from miniharness.loop import Loop
from miniharness.session import Session
from miniharness.tools.runtime import ToolRuntime
from providers.deepseek import DeepSeekProvider


def register_skills(skills: SkillRegistry) -> None:
    """注册 4 个技能：装载即把技能工具挂进 ToolRuntime（可逆），卸载即撤销。"""
    skills.register(Skill(
        name="web-search",
        description="互联网搜索能力",
        tools=[tools._skill_tool("search_web")],
        tool_map=tools._skill_tool_map("search_web"),
        system_prompt="你拥有搜索能力。遇到不确定的事实性问题，请先搜索再回答，不要猜测。",
    ))

    skills.register(Skill(
        name="calculator",
        description="数学计算能力",
        tools=[tools._skill_tool("calculate")],
        tool_map=tools._skill_tool_map("calculate"),
        system_prompt="你拥有计算能力。遇到数学计算请调用 calculate 工具，不要心算。",
    ))

    skills.register(Skill(
        name="file-ops",
        description="文件读写能力",
        tools=[tools._skill_tool("read_file"), tools._skill_tool("write_file")],
        tool_map=tools._skill_tool_map("read_file", "write_file"),
        system_prompt="你拥有文件读写能力。操作文件前请确认路径正确。",
    ))

    skills.register(Skill(
        name="structured-output",
        description="结构化输出能力",
        tools=[tools.make_final_output_tool()],
        tool_map={"final_output": tools.final_output_handler},
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
    ctx.load(FinalOutputPlugin(tools.OUTPUT_TOOL_NAMES))
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
    return DeepSeekProvider(api or config._get_client(), model, on_delta=_print_delta)


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
