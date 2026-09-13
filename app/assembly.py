"""app.assembly —— 装配：把骨架、能力与后端装成 harness。

`build_harness()` 装配 Session + 工具 + LLM provider + 策略插件 + 日志消费者；
`resume_session()` 在其上重放事件日志——投影、压缩摘要与技能状态全部只由日志重建，
磁盘上没有第二份模型可见状态。

策略全挂在事件 seam 上（Loop 零改动）：压缩 → `agent/pre-step`，重试 / 超时 → `tools/execute`，
审批 → `tools/pre-execute`（只对 `run_command`），输出校验 → `tools/post-execute`，
终结工具 → `agent/post-tool`，持久化 / 追踪 → Session 日志订阅。
"""
from openai import OpenAI

from app import config, tools
from capabilities.compaction.provider import CompactionPlugin
from capabilities.final_output.provider import FinalOutputPlugin
from capabilities.permission.provider import PermissionPlugin
from capabilities.persistence.definition import PersistenceManager, Store
from capabilities.persistence.provider import PersistenceConsumer
from capabilities.retry.provider import RetryPlugin
from capabilities.shell.definition import RUN_COMMAND_TOOL
from capabilities.shell.provider import ShellTool
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
from providers.process import SubprocessSeam


def register_skills(skills: SkillRegistry, shell: ShellTool) -> None:
    """注册 5 个技能：装载即把技能工具挂进 ToolRuntime（可逆），卸载即撤销。

    `shell` 技能的工具要 `shell` 实例本身（`run_command` 是它的方法）；其余技能的工具
    都是 `app.tools` 里的纯函数。
    """
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

    skills.register(Skill(
        name="shell",
        description="在受管范围里执行外部命令",
        tools=[RUN_COMMAND_TOOL],
        tool_map={"run_command": shell.run_command},
        system_prompt=(
            "你可以执行外部命令。命令必须以 argv 列表逐项给出（不经 shell），"
            '例如 ["git", "status"]；不要拼 shell 字符串。'
            "命令需要人工批准，被拒绝时不要重试。"
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
    审批策略只覆盖 `run_command`（`ask`；没有审批者时默认拒绝，工具体不执行），其余工具的
    行为与 legacy 一致——legacy 没有审批概念，只有真正执行外部命令的工具需要这道门槛。
    """
    ctx = Context()
    session = Session()
    ctx.provide("session", session)

    loop = Loop()
    ctx.load(loop)                    # 依赖未就绪 → 挂起，provider/工具齐后自动激活
    ctx.load(ToolRuntime())
    ctx.load(llm if llm is not None else _default_llm(client, model))
    ctx.load(SubprocessSeam())        # 受管范围后端：shell 技能的执行地基

    ctx.load(CompactionPlugin())
    ctx.load(RetryPlugin())
    ctx.load(ToolTimeoutPlugin())
    ctx.load(ValidationPlugin())
    ctx.load(PermissionPlugin(approval_required={"run_command"}))
    ctx.load(FinalOutputPlugin(tools.OUTPUT_TOOL_NAMES))
    ctx.load(PersistenceConsumer(PersistenceManager(Store(store_dir)), session_id))
    ctx.load(TraceConsumer())

    shell = ShellTool()
    ctx.load(shell)                   # 提供 run_command；工具本身随 shell 技能装载才可见

    skills = SkillRegistry()
    register_skills(skills, shell)
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
    """装配 harness 并从事件日志重放会话状态。

    重放是恢复的唯一来源：`Session.replay` 重建模型可见历史，
    压缩摘要与技能状态分别由 `CompactionPlugin.restore` / `SkillRegistry.restore`
    折叠同一份日志里的压缩事件与 `skill/*` 事件——不读任何旁路元数据。
    """
    ctx, session, loop = build_harness(model=model, session_id=session_id,
                                       store_dir=store_dir, client=client, llm=llm,
                                       base_system_prompt=base_system_prompt)
    events = pm.load_events(session_id)
    if events:
        session.replay(events)
        ctx.get("compaction").restore(events)
        ctx.get("skills").restore(events)
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
    """恢复历史会话并继续对话：重放日志（投影 / 压缩摘要 / 技能状态）后跑一轮。"""
    pm = PersistenceManager(Store(store_dir))
    if not pm.load_events(session_id):
        return f"❌ 会话 {session_id} 不存在或为空"
    loop = _open_harness(pm, session_id, model=model, store_dir=store_dir,
                         client=client, llm=llm,
                         base_system_prompt=base_system_prompt)
    return loop.turn(new_input)
