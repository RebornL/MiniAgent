"""包内测试：压缩状态的日志重放（`compaction.provider`）。

只依赖本能力与更下层：压缩事件就是「被遮蔽的事件范围 + 替换内容 + 摘要」的完整记录，
`restore()` 折叠日志里的压缩事件重建增量摘要链——重启不靠任何旁路元数据。
"""
from __future__ import annotations

from capabilities.compaction.definition import CompactionConfig, compaction_summaries
from capabilities.compaction.provider import CompactionPlugin
from miniharness.core import Context
from miniharness.session import Session

LONG = "这是一段很长的历史对话内容，用来把 token 数推过阈值。" * 6


def _plugin(session: Session, seen: list[tuple[str, str]]) -> CompactionPlugin:
    ctx = Context()
    ctx.provide("session", session)
    plugin = CompactionPlugin(
        CompactionConfig(max_tokens=300, keep_last_n=2),
        summarizer=lambda existing, new_text: seen.append((existing, new_text)) or "（摘要）",
    )
    ctx.load(plugin)
    return plugin


def test_compaction_state_is_rebuilt_from_the_log_alone():
    session = Session()
    ask = session.append("user/message", content="第一问")
    answer = session.append("assistant/message", content="第一答")
    session.compact("第一份摘要", [ask["seq"], answer["seq"]])

    seen: list[tuple[str, str]] = []
    plugin = _plugin(session, seen)
    plugin.restore(session.events)

    # 摘要链只由日志重建：末项是当前增量摘要，条数是累计压缩次数
    assert compaction_summaries(session.events) == ["第一份摘要"]
    assert plugin.context.summary == "第一份摘要"
    assert plugin.context.total_compactions == 1

    # 重启后接着压缩：`existing` 正是重放出来的那份摘要（增量合并不丢）
    for _ in range(4):
        session.append("user/message", content=LONG)
    assert plugin.compact_if_needed() is not None
    assert seen[0][0] == "第一份摘要"
