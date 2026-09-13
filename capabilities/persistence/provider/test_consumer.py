"""包内测试：持久化消费者的 flush 屏障。

只依赖本包与更下层（`miniharness.core` / `miniharness.session`）：屏障返回才构成崩溃承诺；
屏障之前的事件只活在内存里（不落盘），屏障幂等（没有新事件就不写记录）。
"""
from __future__ import annotations

from capabilities.persistence.definition import PersistenceManager, Store
from capabilities.persistence.provider import PersistenceConsumer
from miniharness.core import Context
from miniharness.session import Session


def _harness(tmp_path, session_id: str = "s1") -> tuple[Context, Session, PersistenceManager]:
    manager = PersistenceManager(Store(str(tmp_path)))
    ctx = Context()
    session = Session()
    ctx.provide("session", session)
    ctx.load(PersistenceConsumer(manager, session_id=session_id))
    return ctx, session, manager


def test_flush_is_the_only_crash_promise(tmp_path):
    ctx, session, manager = _harness(tmp_path)
    log = manager.store.log_path("s1")

    session.append("user/message", content="在吗")
    assert not log.exists()                                # 未过屏障：事件还只在内存里

    ctx.get("persistence").flush()
    assert manager.load_events("s1") == session.events

    session.append("system/message", content="你拥有计算能力")   # 技能装载后的提示刷新
    assert manager.load_events("s1") == session.events[:-1]      # 屏障后的新事件尚未承诺

    ctx.get("persistence").flush()
    assert manager.load_events("s1") == session.events           # 屏障返回即已落盘


def test_model_visible_changes_and_the_turn_boundary_flush(tmp_path):
    """订阅语义不变：模型可见内容变化的事件与回合结束各冲刷一次。"""
    ctx, session, manager = _harness(tmp_path)

    session.append("turn/start", input="在吗")
    session.append("user/message", content="在吗")
    session.append("assistant/message", content="在的")
    session.append("turn/end", status="done")

    assert ctx.get("persistence").saves == 2               # assistant/message + turn/end
    assert manager.load_events("s1") == session.events
