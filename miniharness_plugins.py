"""miniharness_plugins.py — 现有模块迁移到 miniharness 的策略插件与日志消费者

对应 `miniharness-spec.md` §Implementation Decisions「现有模块迁移而不重写行为」与
`MiniAgent-Harness-Design.md` §6 迁移对照表：

    MiniAgent.py 内联策略              本文件
    ──────────────────────────────    ───────────────────────────────────────────
    ctx.maybe_compact(...)            CompactionPlugin    → agent/pre-step（surface 替换）
    with_retry(...)                   RetryPlugin         → tools/execute（around）
    call_with_timeout(...)            ToolTimeoutPlugin   → tools/execute（around）
                                       ↑ 有意偏离：超时改抛 ToolTimeout，不再伪装成 ok 字符串
    Structure 校验 / sanitize         ValidationPlugin    → tools/post-execute
    final_output 即返回（内联）         FinalOutputPlugin   → agent/post-tool（声明式终结工具）
    pm.save_session(...)              PersistenceConsumer → Session 日志订阅
    tracer.*(...)                     TraceConsumer       → Session 日志订阅
    skills.load/unload + 工具注册      SkillRegistry       → ToolRuntime 可逆注册

**全部只订阅 miniharness 事件，Loop 零改动。** 唯一的结构性改动：`AgentTrace.log_llm_call`
只接收已归一化的纯数据（不再吃 SDK 响应对象），把 SDK 形状的耦合从追踪器里移除。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Any, Callable, Iterable

import AgentTrace
import CallFunc
import Compaction
import Persistence
import RetryFunc
import SkillManager
import Structure
from miniharness import Context, Loop, Plugin, Session, ToolDefinition, ToolRuntime

__all__ = [
    "stub_summarizer",
    "CompactionPlugin",
    "RetryPlugin",
    "ToolTimeout",
    "ToolTimeoutPlugin",
    "ValidationPlugin",
    "PersistenceConsumer",
    "TraceConsumer",
    "SkillRegistry",
    "FinalOutputPlugin",
    "SystemPromptPlugin",
]


# ═══════════════ 上下文治理：Compaction → agent/pre-step ═══════════════
def stub_summarizer(existing: str, new_text: str) -> str:
    """确定性摘要 stub（不调 LLM）：增量合并已有摘要 + 新对话首几行。

    形状与 `Compaction.ContextManager._generate_summary` 一致：
    输入 `(已有摘要, 新增对话文本)`，输出合并后的完整摘要。
    """
    lines = [line.strip() for line in new_text.splitlines() if line.strip()]
    parts = ([existing] if existing else []) + lines[:3]
    if len(lines) > 3:
        parts.append(f"…（共 {len(lines)} 行）")
    return " | ".join(parts)


class CompactionPlugin(Plugin):
    """上下文治理策略：订阅 `agent/pre-step`，超阈值时对 Session 做 surface 替换。

    阈值、切分点、keep_last_n、增量摘要语义全部复用 `Compaction.ContextManager`
    （`count_tokens` / `config` / `summary` / `total_compactions` / `Compaction.to_text`）。
    摘要函数可注入，默认 `stub_summarizer`（确定性，不调真实 LLM）。
    """

    inject = ("session",)

    def __init__(
        self,
        config: Compaction.CompactionConfig | None = None,
        summarizer: Callable[[str, str], str] | None = None,
        context_manager: Compaction.ContextManager | None = None,
    ) -> None:
        self.context = context_manager or Compaction.ContextManager(config)
        self.summarizer = summarizer or stub_summarizer

    def apply(self, ctx: Context) -> None:
        self._session: Session = ctx.get("session")
        ctx.provide("compaction", self)      # 供持久化消费者读取当前摘要
        ctx.on("agent/pre-step", self._pre)

    @property
    def summary(self) -> str:
        """当前增量摘要（`Persistence.save_session` 恢复/保存时需要）。"""
        return self.context.summary

    def _pre(self, payload: dict, next_: Callable[[], Any]) -> dict:
        self.compact_if_needed()
        return next_()

    def compact_if_needed(self) -> dict | None:
        """超阈值则压缩；返回 `context/compacted` 事件，未触发则返回 None。"""
        entries = self._session.derive_entries()
        messages = [message for _, message in entries]
        config = self.context.config

        if self.context.count_tokens(messages) <= config.max_tokens:
            return None                                    # 未超阈值：不动

        keep = config.keep_last_n
        if len(messages) <= keep + 2:
            return None                                    # 与 summarize_and_compress 的守卫一致

        split = max(1, len(messages) - keep)
        # system prompt 永不压缩（legacy 也是把它们原样提到最前面）
        replaced = [event["seq"] for event, message in entries[:split]
                    if message["role"] != "system"]
        if not replaced:
            return None

        old_text = "\n".join(
            text for message in messages[:split] if (text := Compaction.to_text(message))
        )
        summary = self.summarizer(self.context.summary, old_text)
        self.context.summary = summary                      # 增量合并语义：旧摘要进下一次合并
        self.context.total_compactions += 1
        return self._session.compact(summary, replaced)


# ═══════════════ 耐用性：Retry → tools/execute ═══════════════
class RetryPlugin(Plugin):
    """重试策略：订阅 `tools/execute`（around），复用 `RetryFunc.with_retry` 的语义。

    只重试 `RetryFunc.is_retryable` 认定的瞬时故障（429/5xx、网络错误），指数退避；
    不可重试的错误立即抛出，由 ToolRuntime 收敛为结构化失败结果。
    """

    inject = ("tools",)

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0,
                 max_delay: float = 30.0) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/execute", self._wrap)

    def _wrap(self, payload: dict, next_: Callable[[], Any]) -> Any:
        return RetryFunc.with_retry(
            next_,
            max_retries=self.max_retries,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            label=payload["call"].get("name", ""),
        )


# ═══════════════ 耐用性：超时 → tools/execute ═══════════════
class ToolTimeout(RuntimeError):
    """工具执行超时（由 `ToolTimeoutPlugin` 抛出，`ToolRuntime` 收敛为结构化 error）。"""


class ToolTimeoutPlugin(Plugin):
    """超时护栏：订阅 `tools/execute`（around），超时即抛 `ToolTimeout`。

    与 legacy `CallFunc.call_with_timeout` 的**有意差别**（用户裁定）：超时不再伪装成
    一个 `ok` 的字符串结果，而是抛出异常，让权威结果明确是 `error`，下游可按 status 区分
    「超时」与「成功」。默认超时沿用 `CallFunc.DEFAULT_TOOL_TIMEOUT`；工具可用 `timeoutMs` 覆盖。
    """

    inject = ("tools",)

    def __init__(self, default_ms: int = CallFunc.DEFAULT_TOOL_TIMEOUT * 1000) -> None:
        self.default_ms = default_ms

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/execute", self._wrap)

    def _wrap(self, payload: dict, next_: Callable[[], Any]) -> Any:
        timeout_ms = payload.get("timeoutMs") or self.default_ms
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(next_)
            return future.result(timeout=timeout_ms / 1000)
        except _FutureTimeout:
            raise ToolTimeout(
                f"工具 {payload['call'].get('name')} 执行超时（{timeout_ms}ms 未返回）"
            ) from None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


# ═══════════════ 输出校验：Structure → tools/post-execute ═══════════════
class ValidationPlugin(Plugin):
    """输出校验策略：订阅 `tools/post-execute`，复用 `Structure` 的注入检测与 schema 校验。

    校验不通过则把权威结果改写为结构化失败（而非抛异常）。
    """

    inject = ("tools",)

    def __init__(self, schemas: dict[str, dict] | None = None) -> None:
        self.schemas = dict(schemas or {})

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/post-execute", self._post)

    def _post(self, payload: dict, next_: Callable[[], Any]) -> dict:
        result = next_()
        if result.get("status") != "ok":
            return result
        name = result["name"]
        try:
            value = Structure.sanitize_output(result.get("value"))
            schema = self.schemas.get(name)
            if schema is not None:
                value = Structure.validate_output(value, schema)
        except ValueError as exc:
            return {"status": "error", "name": name, "error": f"输出校验失败: {exc}"}
        return {**result, "value": value}


# ═══════════════ 终结行为：FinalOutput → agent/post-tool ═══════════════
class FinalOutputPlugin(Plugin):
    """声明式终结工具：某工具执行成功即可作为本轮最终答复（Loop 零改动）。

    订阅 `agent/post-tool`：刚执行的工具名在 `terminal_tools` 内且权威结果为 `ok` 时，
    以它的 `content` 短路本轮（不再采样）；否则交给后继监听者（`next_()`）。
    """

    inject = ("tools",)

    def __init__(self, terminal_tools: Iterable[str] = ("final_output",)) -> None:
        self.terminal_tools = set(terminal_tools)

    def apply(self, ctx: Context) -> None:
        ctx.on("agent/post-tool", self._post)

    def _post(self, payload: dict, next_: Callable[[], Any]) -> dict:
        result = payload["result"]
        if result.get("status") == "ok" and result.get("name") in self.terminal_tools:
            return {"continue": False, "answer": result.get("content", "")}
        return next_()


# ═══════════════ 日志消费者：Persistence / AgentTrace ═══════════════
class PersistenceConsumer(Plugin):
    """把 Session 日志喂给 `Persistence.PersistenceManager`（复用 `save_session` 完整接口）。

    沿用 legacy 的频率语义：只在模型可见内容变化的事件后落盘；`summary` 与
    `active_skills` 经 `ctx.get("compaction")` / `ctx.get("skills")` 现取，缺失则用空值。
    """

    inject = ("session",)
    SAVE_ON = ("assistant/message", "tool/result", "tool/denied", "context/compacted")

    def __init__(self, manager: Persistence.PersistenceManager | None = None,
                 session_id: str | None = None) -> None:
        self.manager = manager or Persistence.PersistenceManager()
        self.session_id = session_id or self.manager.new_session_id()
        self.saves = 0
        self._ctx: Context | None = None
        self._session: Session | None = None
        self._last_input = ""

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        self._session = ctx.get("session")
        ctx.effect(self._session.subscribe(self._on_event))

    def _on_event(self, event: dict) -> None:
        if event["type"] == "user/message":
            self._last_input = event.get("content", "")
            return
        if event["type"] not in self.SAVE_ON or self._session is None:
            return
        compaction = self._ctx.get("compaction") if self._ctx else None
        skills = self._ctx.get("skills") if self._ctx else None
        trace = self._ctx.get("trace") if self._ctx else None
        self.manager.save_session(
            self.session_id,
            self._session.derive_messages(),
            compaction.summary if compaction is not None else "",
            trace.tracer.to_dicts() if trace is not None else [],
            skills.active_names() if skills is not None else [],
            self._last_input,
        )
        self.saves += 1


class TraceConsumer(Plugin):
    """把 Session 日志喂给 `AgentTrace.AgentTracer`（复用 start_run / log_llm_call / log_tool_call）。

    日志里没有真实耗时（不在本阶段范围），span 的 duration 记 0。
    """

    inject = ("session",)

    def __init__(self, tracer: AgentTrace.AgentTracer | None = None,
                 run_id: str | None = None) -> None:
        self.tracer = tracer or AgentTrace.AgentTracer()
        self.run_id = run_id or "run_0"
        self.run: AgentTrace.Span | None = None

    def apply(self, ctx: Context) -> None:
        self._session: Session = ctx.get("session")
        ctx.provide("trace", self)          # 供 PersistenceConsumer 落盘真实 span
        ctx.effect(self._session.subscribe(self._on_event))

    def _on_event(self, event: dict) -> None:
        kind = event["type"]
        if kind == "turn/start":
            self.run = self.tracer.start_run(self.run_id, event.get("input", ""))
        elif kind == "assistant/message" and event.get("tool_calls"):
            # 只传纯数据：不再伪造 provider 响应对象，追踪器也不认识任何 SDK 类型
            messages = self._session.derive_messages()[:-1]   # 去掉刚追加的这条回复
            self._span(self.tracer.log_llm_call(
                messages,
                content=event.get("content", ""),
                tool_calls=[
                    {"name": call.get("name", ""),
                     "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)}
                    for call in event["tool_calls"]
                ],
            ))
        elif kind == "tool/result":
            self._span(self.tracer.log_tool_call(
                event.get("name", ""), event.get("args") or {},
                str(event.get("content", "")), 0.0))

    def _span(self, span: AgentTrace.Span) -> None:
        if self.run is not None:
            self.run.children.append(span)


# ═══════════════ 技能装载：SkillManager → 可逆注册 ═══════════════
class SkillRegistry(Plugin):
    """技能的可逆注册（原语 5）：装载即把技能工具挂进 ToolRuntime，卸载即撤销。

    复用 `SkillManager` 的技能目录/激活记录（`get_active_prompt` / `get_active_tools`
    语义不变），并暴露 legacy 的 `load_skill` / `unload_skill` 两个 meta 工具。
    """

    inject = ("tools",)

    def __init__(self, manager: SkillManager.SkillManager | None = None) -> None:
        self.manager = manager or SkillManager.SkillManager()
        self._skills: dict[str, SkillManager.Skill] = {}   # SkillManager 不提供按名查询
        self._active: dict[str, Callable[[], None]] = {}

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        self._tools: ToolRuntime = ctx.get("tools")
        ctx.provide("skills", self)
        ctx.effect(self.unload_all)          # 插件卸载时撤销全部已装载技能（可逆注册）
        # meta 工具随插件卸载一并撤销
        self._tools.register(ToolDefinition(
            name="load_skill", description="加载一个技能模块，其工具立即可用",
            parameters={"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"]},
            execute=lambda args: self.load_skill(args.get("name", "")),
        ))
        self._tools.register(ToolDefinition(
            name="unload_skill", description="卸载一个技能模块，其工具立即撤销",
            parameters={"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"]},
            execute=lambda args: self.unload_skill(args.get("name", "")),
        ))

    def register(self, skill: SkillManager.Skill) -> None:
        self._skills[skill.name] = skill
        self.manager.register(skill)

    def load(self, name: str) -> Callable[[], None]:
        """装载技能：工具经 ToolRuntime 可逆注册；返回卸载 disposer（幂等）。"""
        existing = self._active.get(name)
        if existing is not None:
            return existing
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"技能 '{name}' 不存在。可用: {', '.join(self._skills)}")

        self.manager.load(name)                            # 复用 legacy 的激活记录
        disposers = []
        for tool_def in skill.tools:
            function = tool_def.get("function", {})
            fn = skill.tool_map.get(function.get("name", ""))
            if fn is None:
                continue
            disposers.append(self._tools.register(ToolDefinition(
                name=function.get("name", ""),
                description=function.get("description", ""),
                parameters=function.get("parameters", {}),
                execute=_bind_legacy(fn),
            )))

        def unload() -> None:
            for dispose in reversed(disposers):
                dispose()
            disposers.clear()
            self._active.pop(name, None)
            self.manager.unload(name)

        self._active[name] = unload
        return self._ctx.effect(unload)

    def unload(self, name: str) -> bool:
        disposer = self._active.get(name)
        if disposer is None:
            return False
        disposer()
        return True

    def unload_all(self) -> None:
        """撤销全部已装载技能（插件卸载时逆序 unwind 的入口）。"""
        for name in list(self._active):
            self.unload(name)

    # ── legacy meta 工具形状：返回给模型的提示字符串 ──
    def load_skill(self, name: str) -> str:
        try:
            self.load(name)
        except KeyError as exc:
            return str(exc.args[0])
        return f"✅ 已加载技能 '{name}'（{self._skills[name].description}）"

    def unload_skill(self, name: str) -> str:
        if not self.unload(name):
            return f"技能 '{name}' 当前未加载。"
        return f"✅ 已卸载技能 '{name}'"

    def get_active_tools(self) -> list[dict]:
        return self.manager.get_active_tools()

    def active_names(self) -> list[str]:
        """已激活技能名（供持久化恢复/保存会话时记录）。"""
        return sorted(self._active)

    def get_active_prompt(self) -> str:
        return self.manager.get_active_prompt()


class SystemPromptPlugin(Plugin):
    """system prompt 同步策略：技能装载后，其领域提示立刻进入模型可见历史。

    legacy 每步重算 system prompt 并覆写 `messages[0]`；Session 是 append-only 日志，
    这里改为「组合结果与日志里最后一条 `system/message` 不同时追加一条」。
    技能的装载/卸载都走工具调用，故订阅 `tools/result` 就能在**同一轮内**、下次采样前刷新；
    再订阅 `agent/pre-step` 覆盖每轮开始；`apply` 时的首次同步对应 legacy 的 `messages[0]`。
    """

    inject = ("session", "skills")

    def __init__(self, base_prompt: str = "") -> None:
        self.base_prompt = base_prompt

    def apply(self, ctx: Context) -> None:
        self._session: Session = ctx.get("session")
        self._skills = ctx.get("skills")
        self.sync()
        ctx.on("tools/result", self._on_event)
        ctx.on("agent/pre-step", self._pre)

    def compose(self) -> str:
        """base + 已激活技能的领域提示（与 legacy `build_system_prompt` 同形）。"""
        parts = [self.base_prompt]
        skill_prompt = self._skills.get_active_prompt()
        if skill_prompt:
            parts.append(f"\n\n--- 当前激活的技能 ---\n{skill_prompt}")
        return "\n".join(parts)

    def sync(self) -> None:
        """组合结果变化时追加一条 system/message；未变化则不动。"""
        prompt = self.compose()
        if not prompt:
            return
        for event in reversed(self._session.events):
            if event["type"] == "system/message":
                if event["content"] == prompt:
                    return
                break
        self._session.append("system/message", content=prompt)

    def _on_event(self, payload: dict) -> None:
        self.sync()

    def _pre(self, payload: dict, next_: Callable[[], Any]) -> dict:
        self.sync()
        return next_()


def _bind_legacy(fn: Callable[..., Any]) -> Callable[[dict], Any]:
    """legacy 工具是 `fn(**args)`，ToolDefinition.execute 收一个 args dict。"""
    def execute(args: dict) -> Any:
        return fn(**args)
    return execute


# ═══════════════ 装配示例（smoke run：python miniharness_plugins.py） ═══════════════
def _demo() -> None:
    """装配完整迁移栈：压缩 / 重试 / 超时 / 校验 / 持久化 / 追踪 / 技能，Loop 不变。"""
    import tempfile

    from miniharness import MockLLM

    ctx = Context()
    ctx.provide("session", Session())
    ctx.load(Loop())
    ctx.load(ToolRuntime())
    ctx.load(ToolTimeoutPlugin(default_ms=500))
    ctx.load(RetryPlugin(max_retries=1, base_delay=0.0))

    summarize_calls: list[str] = []
    ctx.load(CompactionPlugin(
        Compaction.CompactionConfig(max_tokens=120, keep_last_n=2),
        summarizer=lambda existing, new_text: summarize_calls.append(new_text) or "（摘要）",
    ))

    workdir = tempfile.mkdtemp(prefix="miniharness_demo_")
    persistence = PersistenceConsumer(
        Persistence.PersistenceManager(Persistence.Store(workdir)), session_id="demo")
    tracer = TraceConsumer()
    ctx.load(persistence)
    ctx.load(tracer)

    skills = SkillRegistry()
    skills.register(SkillManager.Skill(
        name="math", description="算数技能",
        tools=[{"type": "function", "function": {
            "name": "double", "description": "翻倍",
            "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}}],
        tool_map={"double": lambda n: n * 2},
    ))
    ctx.load(skills)

    llm = MockLLM()
    llm.then_tool_call("load_skill", {"name": "math"})
    llm.then_tool_call("double", {"n": 21})
    llm.then_text("42")
    ctx.load(llm)

    session: Session = ctx.get("session")
    # 预置一段超阈值的历史：压缩策略在第一次 turn 的 pre-step 触发 surface 替换
    session.append("system/message", content="你是助手")
    for i in range(3):
        session.append("user/message", content=f"第{i}问：" + "很长的历史内容。" * 20)
        session.append("assistant/message", content=f"第{i}答：" + "很长的历史内容。" * 20)

    print("turn ->", ctx.get("loop").turn("把 21 翻倍"))
    print("log   ->", [(e["seq"], e["type"]) for e in session.events])
    print("compact->", [(e["replaced_seqs"], e["summary"]) for e in session.events
                        if e["type"] == "context/compacted"],
          f"（摘要输入 {len(summarize_calls)} 次）")
    print("traces->", [(s.type, s.output if s.type == "tool_call" else "…")
                       for s in (tracer.run.children if tracer.run else [])])
    print("saved ->", persistence.saves, "次；",
          Persistence.PersistenceManager(Persistence.Store(workdir)).load_messages("demo"))


if __name__ == "__main__":
    _demo()
