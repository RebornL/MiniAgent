"""跨包集成：能力之间的协作与契约等价性（压缩 / 终结 / 日志消费者）。

这些用例把多个能力装到同一个 Loop 上，断言「只多装一个插件」不改变既有结局，
并与契约包（legacy 语义）逐字对齐。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from capabilities.compaction.provider import CompactionConfig, CompactionPlugin, ContextManager
from capabilities.final_output.provider import FinalOutputPlugin
from miniharness.core import Context
from miniharness.session import Session
from miniharness.session.test_projection import _event_types
from miniharness.tools.contract import ToolDefinition
from providers.mock import MockLLM
from tests.support import CALC_PARAMS, _assemble, _scripted

LONG = "这是第一段很长的历史对话内容，用来把 token 数推过阈值。" * 6


class _FakeClient:
    """假 LLM client：只为 Compaction._generate_summary 提供确定性响应，并记录 prompt。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model: str, messages: list[dict], temperature: int) -> Any:
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


def test_compaction_plugin_matches_legacy_threshold_split_and_summary():
    """Story 15：达同样阈值才压缩，切分与增量摘要语义与 legacy 一致。"""
    config = CompactionConfig(max_tokens=300, keep_last_n=2)
    summary_text = "（旧对话摘要）"

    # ── legacy 侧：直接跑 ContextManager 的原路径 ──
    legacy = ContextManager(config)
    client = _FakeClient(summary_text)

    def legacy_history() -> list[dict]:
        return [{"role": "system", "content": "你是助手"}] + [
            {"role": role, "content": LONG} for role in ("user", "assistant", "user", "assistant")
        ]

    # ── harness 侧：同一个 Loop，只多装一个 CompactionPlugin ──
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

    # 未超阈值时不压缩（同样的 messages 交给 legacy 也是原样返回）
    below = Session(events=list(session.events))
    below_plugin = CompactionPlugin(config=CompactionConfig(max_tokens=10**6),
                                    summarizer=summarizer)
    below_plugin.apply(_ctx_with(below))
    assert below_plugin.compact_if_needed() is None
    assert ContextManager(CompactionConfig(max_tokens=10**6)) \
        .maybe_compact(below.derive_messages(), client) == below.derive_messages()

    messages = session.derive_messages()
    legacy_out = legacy.maybe_compact(messages, client)
    loop.turn("新问题")

    compacted = [e for e in session.events if e["type"] == "context/compacted"]
    assert len(compacted) == 1
    assert compacted[0]["summary"] == legacy.summary == summary_text
    assert plugin.context.total_compactions == legacy.total_compactions == 1
    # 摘要函数拿到的文本，正是 legacy 送去摘要的那段旧对话
    assert seen[0][0] == "" and seen[0][1] in client.prompts[0]
    # 切分一致：保留的尾部 == legacy 压缩结果里去掉 system 与摘要注入后的部分
    projected = session.derive_messages()
    assert projected[1]["content"] == f"[上下文已压缩] {summary_text}"
    assert projected[2:4] == legacy_out[2:]
    # 本轮新输入在 pre-step 之后才入日志，不在被替换区间内
    assert "新问题" in [m["content"] for m in projected]

    # ── 第二次压缩：增量摘要合并（旧摘要进下一次合并）与 legacy 一致 ──
    for role in ("user", "assistant", "user", "assistant"):
        session.append(f"{role}/message", content=LONG)
    messages2 = session.derive_messages()
    legacy_out2 = legacy.maybe_compact(messages2, client)
    loop.turn("再问")

    assert legacy.total_compactions == plugin.context.total_compactions == 2
    assert seen[1][0] == summary_text and summary_text in client.prompts[1]
    assert "再问" in [m["content"] for m in session.derive_messages()]
    assert summary_text in legacy_out2[1]["content"]


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
