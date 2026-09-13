"""app.stack_demo —— 全栈 smoke run：`python -m app.stack_demo`（离线，不联网）。

装配压缩 / 重试 / 超时 / 持久化 / 追踪 / 技能六个能力 + mock provider，跑通一次 turn，
顺带打印日志、压缩事件、trace span 与落盘结果。
"""
from __future__ import annotations

from capabilities.compaction.definition import CompactionConfig
from capabilities.compaction.provider import CompactionPlugin
from capabilities.persistence.provider import PersistenceConsumer, PersistenceManager, Store
from capabilities.retry.provider import RetryPlugin
from capabilities.skills.definition import Skill
from capabilities.skills.provider import SkillRegistry
from capabilities.timeout.provider import ToolTimeoutPlugin
from capabilities.tracing.provider import TraceConsumer
from miniharness.core import Context
from miniharness.loop import Loop
from miniharness.session import Session
from miniharness.tools.runtime import ToolRuntime
from providers.mock import MockLLM


# ═══════════════ 装配示例（smoke run：python -m app.stack_demo） ═══════════════
def _demo() -> None:
    """装配完整迁移栈：压缩 / 重试 / 超时 / 校验 / 持久化 / 追踪 / 技能，Loop 不变。"""
    import tempfile

    from providers.mock import MockLLM

    ctx = Context()
    ctx.provide("session", Session())
    ctx.load(Loop())
    ctx.load(ToolRuntime())
    ctx.load(ToolTimeoutPlugin(default_ms=500))
    ctx.load(RetryPlugin(max_retries=1, base_delay=0.0))

    summarize_calls: list[str] = []
    ctx.load(CompactionPlugin(
        CompactionConfig(max_tokens=120, keep_last_n=2),
        summarizer=lambda existing, new_text: summarize_calls.append(new_text) or "（摘要）",
    ))

    workdir = tempfile.mkdtemp(prefix="miniharness_demo_")
    persistence = PersistenceConsumer(
        PersistenceManager(Store(workdir)), session_id="demo")
    tracer = TraceConsumer()
    ctx.load(persistence)
    ctx.load(tracer)

    skills = SkillRegistry()
    skills.register(Skill(
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
    print("compact->", [(e["shadowed_range"], e["summary"]) for e in session.events
                        if e["type"] == "context/compacted"],
          f"（摘要输入 {len(summarize_calls)} 次）")
    print("traces->", [(s.type, s.output if s.type == "tool_call" else "…")
                       for s in (tracer.run.children if tracer.run else [])])
    print("saved ->", persistence.saves, "次；",
          "日志", len(PersistenceManager(Store(workdir)).load_events("demo")), "条事件")


if __name__ == "__main__":
    _demo()
