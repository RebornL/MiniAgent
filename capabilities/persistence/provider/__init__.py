"""persistence.provider —— 会话持久化的实现（Provider）。

`PersistenceConsumer` 订阅 Session 日志，把**尚未落盘的事件**经**有界写后缓冲**批量追加进
权威日志文件；落盘的语义与格式版本在契约包 `capabilities.persistence.definition`。

- 事件先入内存日志（`Session.events`），再进写后缓冲；缓冲未满不落盘，满容即批量落盘，
  待落盘条数因此不超过 `buffer_capacity`——写盘次数不随 append 次数增长。
- `flush()` 是显式屏障：**返回才构成崩溃承诺**；没有待落盘项时不触碰盘（重复屏障幂等）。
- 写失败时未落盘项留在缓冲里，原地重试即可；日志不出现半条记录（写残的尾行在写前与写
  失败时都修掉，水位随盘上真实形状重读，不丢也不重）。
- 检查点策略订阅 Loop 的 `agent/checkpoint`（每步开始前 / 向模型发起请求前 / 顶层工具
  派发前）落盘，并且 **fail-closed**：屏障抛错就让下游的副作用不发生。
"""
from __future__ import annotations

from capabilities.persistence.definition import PersistenceManager
from miniharness.core import Context, Plugin
from miniharness.session import Session

__all__ = ["PersistenceConsumer", "DEFAULT_BUFFER_CAPACITY"]

#: 有界写后缓冲的默认容量（条）：够大以摊薄写盘，够小以不让待落盘项无限堆积。
DEFAULT_BUFFER_CAPACITY = 64

#: 一进缓冲就触发落盘的**事件类型**（本模块的实现细节，不作为包的公开面导出）。
#: 回合边界：回合的收尾事件过屏障——一次 turn 结束即已落盘的既有承诺。
#: 其余写盘都由缓冲满容与语义检查点触发，不随每条模型可见事件同步写。
FLUSH_TRIGGER_EVENTS = ("turn/end",)


# ═══════════════ 日志消费者：Persistence ═══════════════
class PersistenceConsumer(Plugin):
    """把 Session 日志喂给 `PersistenceManager`（日志本身落盘，投影不落盘）。

    只订阅 `session` 与 `agent/checkpoint`：摘要与技能状态都是日志里的事件，
    随日志一起落盘，因此这里不再从 `ctx.get("compaction")` / `ctx.get("skills")` 现取任何
    旁路状态。
    """

    inject = ("session",)

    def __init__(self, manager: PersistenceManager | None = None,
                 session_id: str | None = None,
                 buffer_capacity: int = DEFAULT_BUFFER_CAPACITY) -> None:
        self.manager = manager or PersistenceManager()
        self.session_id = session_id or self.manager.new_session_id()
        self.buffer_capacity = max(1, buffer_capacity)
        self.saves = 0
        self._pending: list[dict] = []       # 有界写后缓冲：已入内存日志、尚未过屏障的事件
        self._ctx: Context | None = None
        self._session: Session | None = None

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        self._session = ctx.get("session")
        ctx.provide("persistence", self)     # 屏障可被装配层与策略按名字取用
        ctx.effect(self._session.subscribe(self._on_event))
        ctx.on("agent/checkpoint", self._on_checkpoint)

    def flush(self) -> int:
        """显式 flush 屏障：把缓冲批量落盘并返回写入条数；返回即构成崩溃承诺。

        未过屏障的事件只活在内存里——崩溃丢的是屏障之后的事件。屏障幂等：没有待落盘项时
        直接返回 0，不触碰盘。
        """
        if not self._pending:
            return 0
        return self._drain()

    def _on_event(self, event: dict) -> None:
        if self._session is None:
            return
        self._pending.append(event)
        if event["type"] in FLUSH_TRIGGER_EVENTS or len(self._pending) >= self.buffer_capacity:
            self._drain()

    def _on_checkpoint(self, payload: dict) -> None:
        """语义检查点：fail-closed 地落盘——屏障抛错即中止下游（副作用不发生）。"""
        self.flush()

    def _drain(self) -> int:
        """把缓冲批量落盘，返回写入条数。

        落盘成功才出队：写失败时未落盘项留在队列里，可原地重试。传给管理器的仍是整份内存
        日志（`meta.json` 的派生计数由它算），水位去重保证只追加真正缺的那些。
        """
        count = len(self._pending)
        trace = self._ctx.get("trace") if self._ctx else None
        written = self.manager.save_session(
            self.session_id,
            self._session.events,
            trace.tracer.to_dicts() if trace is not None else [],
        )
        del self._pending[:count]
        self.saves += 1
        return written
