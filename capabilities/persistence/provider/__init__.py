"""persistence.provider —— 会话持久化的实现（Provider）。

`PersistenceConsumer` 订阅 Session 日志：只在模型可见内容变化的事件后落盘；
summary / active_skills / trace 经 `ctx.get(...)` 现取。落盘与恢复的语义在契约包
`capabilities.persistence.definition`。
"""
from __future__ import annotations

from capabilities.persistence.definition import PersistenceManager
from miniharness.core import Context, Plugin
from miniharness.session import Session

__all__ = ["PersistenceConsumer"]




# ═══════════════ 日志消费者：Persistence / AgentTrace ═══════════════
class PersistenceConsumer(Plugin):
    """把 Session 日志喂给 `PersistenceManager`（复用 `save_session` 完整接口）。

    沿用 legacy 的频率语义：只在模型可见内容变化的事件后落盘；`summary` 与
    `active_skills` 经 `ctx.get("compaction")` / `ctx.get("skills")` 现取，缺失则用空值。
    """

    inject = ("session",)
    SAVE_ON = ("assistant/message", "tool/result", "tool/denied", "context/compacted")

    def __init__(self, manager: PersistenceManager | None = None,
                 session_id: str | None = None) -> None:
        self.manager = manager or PersistenceManager()
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
