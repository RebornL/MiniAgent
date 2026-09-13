"""compaction.definition —— 压缩的契约（低频）：压缩状态的恢复语义。

压缩事件 `context/compacted` 的词汇在 `miniharness.session`：它携带「被遮蔽的事件范围 +
替换内容 + 摘要」，是一次压缩的完整记录。本包提供从事件日志投影压缩状态的纯函数：

- `compaction_summaries`：投影出每次压缩的摘要本体，按发生顺序（纯函数、可重放）。
  摘要链恢复规则：末项是当前增量摘要，条数是累计压缩次数。

压缩阈值、摘要提示词与切分策略是实现，见 `capabilities.compaction.provider`。
"""
from __future__ import annotations

from typing import Iterable

from miniharness.session import COMPACTED_EVENT

__all__ = ["compaction_summaries"]


def compaction_summaries(events: Iterable[dict]) -> list[str]:
    """从事件日志投影压缩状态：每次压缩的摘要本体，按发生顺序（纯函数、可重放）。

    压缩事件的 `replacement` 是模型可见的替换内容，`summary` 是摘要本体；
    后者是下一次增量压缩的输入，故恢复时必须从日志重建它。
    """
    return [event.get("summary", "") for event in events
            if event.get("type") == COMPACTED_EVENT]
