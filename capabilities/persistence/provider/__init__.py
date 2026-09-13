"""persistence.provider —— 会话持久化的实现（Provider）。

`PersistenceConsumer` 订阅 Session 日志，把**尚未落盘的事件**追加进权威日志文件；
落盘的语义与格式版本在契约包 `capabilities.persistence.definition`。

写盘有显式屏障：`flush()` 返回才构成崩溃承诺。屏障之前的事件只活在内存里——
这是刻意的语义，检查点策略（在哪些语义点强制屏障）是 T4 的工作。
"""
from __future__ import annotations

from capabilities.persistence.definition import PersistenceManager
from miniharness.core import Context, Plugin
from miniharness.session import Session

__all__ = ["PersistenceConsumer"]




# ═══════════════ 日志消费者：Persistence ═══════════════
class PersistenceConsumer(Plugin):
    """把 Session 日志喂给 `PersistenceManager`（日志本身落盘，投影不落盘）。

    沿用 legacy 的频率语义：模型可见内容变化的事件与回合结束各冲刷一次；
    `summary` 与 `active_skills` 经 `ctx.get("compaction")` / `ctx.get("skills")` 现取，缺失则用空值。
    """

    inject = ("session",)
    FLUSH_ON = ("assistant/message", "tool/result", "tool/denied",
                "context/compacted", "turn/end")

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
        ctx.provide("persistence", self)     # 屏障可被装配层与策略按名字取用
        ctx.effect(self._session.subscribe(self._on_event))

    def flush(self) -> int:
        """显式 flush 屏障：把当前日志落盘并返回写入条数；返回即构成崩溃承诺。

        未屏障的事件只活在内存里——崩溃即丢。屏障是幂等的：没有新事件时不追加事件记录。
        """
        compaction = self._ctx.get("compaction") if self._ctx else None
        skills = self._ctx.get("skills") if self._ctx else None
        trace = self._ctx.get("trace") if self._ctx else None
        written = self.manager.save_session(
            self.session_id,
            self._session.events,
            compaction.summary if compaction is not None else "",
            trace.tracer.to_dicts() if trace is not None else [],
            skills.active_names() if skills is not None else [],
            self._last_input,
        )
        self.saves += 1
        return written

    def _on_event(self, event: dict) -> None:
        if event["type"] == "user/message":
            self._last_input = event.get("content", "")
            return
        if event["type"] not in self.FLUSH_ON or self._session is None:
            return
        self.flush()
