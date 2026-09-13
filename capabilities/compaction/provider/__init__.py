"""compaction.provider —— 压缩策略的实现（Provider）。

`CompactionPlugin` 订阅 `agent/pre-step`：超阈值时把「被遮蔽的事件范围 + 替换内容 + 摘要」
写成一条 `context/compacted` 事件（surface 替换）；阈值、切分点与增量摘要语义全部复用契约包
`capabilities.compaction.definition`。摘要函数可注入，默认 `stub_summarizer`（确定性，不调 LLM）。

压缩状态本身（增量摘要链）只由日志重建：`restore(events)` 折叠日志里的压缩事件，
不读任何旁路元数据——重启后接着压缩时，`existing` 就是日志里最后一次压缩的摘要。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from capabilities.compaction.definition import (
    CompactionConfig,
    ContextManager,
    compaction_summaries,
    to_text,
)
from miniharness.core import Context, Plugin
from miniharness.session import Session

__all__ = ["CompactionPlugin", "stub_summarizer"]




# ═══════════════ 上下文治理：Compaction → agent/pre-step ═══════════════
def stub_summarizer(existing: str, new_text: str) -> str:
    """确定性摘要 stub（不调 LLM）：增量合并已有摘要 + 新对话首几行。

    形状与 `ContextManager._generate_summary` 一致：
    输入 `(已有摘要, 新增对话文本)`，输出合并后的完整摘要。
    """
    lines = [line.strip() for line in new_text.splitlines() if line.strip()]
    parts = ([existing] if existing else []) + lines[:3]
    if len(lines) > 3:
        parts.append(f"…（共 {len(lines)} 行）")
    return " | ".join(parts)


class CompactionPlugin(Plugin):
    """上下文治理策略：订阅 `agent/pre-step`，超阈值时对 Session 做 surface 替换。

    阈值、切分点、keep_last_n、增量摘要语义全部复用 `ContextManager`
    （`count_tokens` / `config` / `summary` / `total_compactions` / `to_text`）。
    摘要函数可注入，默认 `stub_summarizer`（确定性，不调真实 LLM）。
    """

    inject = ("session",)

    def __init__(
        self,
        config: CompactionConfig | None = None,
        summarizer: Callable[[str, str], str] | None = None,
        context_manager: ContextManager | None = None,
    ) -> None:
        self.context = context_manager or ContextManager(config)
        self.summarizer = summarizer or stub_summarizer

    def apply(self, ctx: Context) -> None:
        self._session: Session = ctx.get("session")
        ctx.provide("compaction", self)      # 装配层按名字取用它做重放（restore）
        ctx.on("agent/pre-step", self._pre)

    def restore(self, events: Iterable[dict]) -> None:
        """从事件日志重放压缩状态：摘要链只由日志里的压缩事件重建，不读旁路元数据。"""
        self.context.restore(compaction_summaries(events))

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
            text for message in messages[:split] if (text := to_text(message))
        )
        summary = self.summarizer(self.context.summary, old_text)
        self.context.summary = summary                      # 增量合并语义：旧摘要进下一次合并
        self.context.total_compactions += 1
        return self._session.compact(summary, replaced)
