"""跨包集成：能力之间的协作与契约等价性（压缩 / 终结 / 日志消费者）。

这些用例把多个能力装到同一个 Loop 上，断言「只多装一个插件」不改变既有结局，
并与契约包逐字对齐。
"""
from __future__ import annotations

import json

from capabilities.compaction.provider import CompactionConfig, CompactionPlugin
from capabilities.final_output.provider import FinalOutputPlugin
from miniharness.core import Context
from miniharness.session import Session
from miniharness.session.test_projection import _event_types
from miniharness.tools.contract import ToolDefinition
from providers.mock import MockLLM
from tests.support import CALC_PARAMS, _assemble, _scripted

LONG = "这是第一段很长的历史对话内容，用来把 token 数推过阈值。" * 6


def test_compaction_plugin_threshold_split_and_incremental_summary():
    """Story 15：超阈值才压缩；切分与增量摘要语义固定（原以 legacy ContextManager 为差分 oracle，legacy 路径已随 #15 删除，改为显式断言）。"""
    config = CompactionConfig(max_tokens=300, keep_last_n=2)
    summary_text = "（旧对话摘要）"

    seen: list[tuple[str, str]] = []

    def summarizer(existing: str, new_text: str) -> str:
        seen.append((existing, new_text))
        return summary_text

    llm = _scripted("double", {"n": 1}, "答")
    plugin = CompactionPlugin(config=config, summarizer=summarizer)
    _, session, loop, _ = _assemble(
        llm, plugins=[plugin], tools=[ToolDefinition("double", "", {}, lambda a: 1)])

    for role, content in (("system", "你是助手"), ("user", LONG), ("assistant", LONG),
                          ("user", LONG), ("assistant", LONG)):
        session.append(f"{role}/message", content=content)

    # 未超阈值时不压缩
    below = Session(events=list(session.events))
    below_plugin = CompactionPlugin(config=CompactionConfig(max_tokens=10**6),
                                    summarizer=summarizer)
    below_plugin.apply(_ctx_with(below))
    assert below_plugin.compact_if_needed() is None

    loop.turn("新问题")

    compacted = [e for e in session.events if e["type"] == "context/compacted"]
    assert len(compacted) == 1
    assert compacted[0]["summary"] == summary_text
    assert plugin.context.total_compactions == 1
    # 摘要函数首次调用：旧摘要为空；新文本正是被替换区间里的旧对话（含 system 行）
    assert seen[0][0] == ""
    assert LONG in seen[0][1] and "你是助手" in seen[0][1]
    # 切分：摘要注入之后保留的尾部正是最近两条消息（keep_last_n=2）
    projected = session.derive_messages()
    assert projected[1]["content"] == f"[上下文已压缩] {summary_text}"
    assert projected[2:4] == [{"role": "user", "content": LONG},
                              {"role": "assistant", "content": LONG}]
    # 本轮新输入在 pre-step 之后才入日志，不在被替换区间内
    assert "新问题" in [m["content"] for m in projected]

    # ── 第二次压缩：增量摘要合并（旧摘要进下一次合并） ──
    for role in ("user", "assistant", "user", "assistant"):
        session.append(f"{role}/message", content=LONG)
    loop.turn("再问")

    assert plugin.context.total_compactions == 2
    assert seen[1][0] == summary_text
    assert "再问" in [m["content"] for m in session.derive_messages()]


def _ctx_with(session: Session) -> Context:
    ctx = Context()
    ctx.provide("session", session)
    return ctx


def test_final_output_plugin_ends_turn_at_terminal_tool():
    """声明式终结工具：本轮以它的 content 结束，且不再采样（Loop 零改动）。"""
    llm = (MockLLM()
           .then_tool_call("final_output", {"result": {"answer": 42}, "summary": "42"})
           .then_text("不应走到这一步"))
    _, session, loop, _ = _assemble(
        llm,
        plugins=[FinalOutputPlugin(terminal_tools={"final_output"})],
        tools=[ToolDefinition("final_output", "输出最终答案", {},
                              lambda args: json.dumps(args, ensure_ascii=False))],
    )

    answer = loop.turn("输出结构化答案")

    assert json.loads(answer) == {"result": {"answer": 42}, "summary": "42"}
    assert len(llm.calls) == 1                             # 终结后不再采样
    assert _event_types(session)[-2:] == ["tool/result", "turn/end"]

    # 非终结工具不会被误判为终结：同一插件下普通工具照旧继续采样
    llm2 = MockLLM().then_tool_call("calculate", {"expression": "1 + 1"}).then_text("1 + 1 = 2")
    _, _, loop2, _ = _assemble(
        llm2,
        plugins=[FinalOutputPlugin(terminal_tools={"final_output"})],
        tools=[ToolDefinition("calculate", "算数", CALC_PARAMS, lambda args: "2")],
    )

    assert loop2.turn("算 1+1") == "1 + 1 = 2"
    assert len(llm2.calls) == 2
