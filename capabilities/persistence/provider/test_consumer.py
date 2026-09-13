"""包内测试：持久化消费者的有界写后缓冲、flush 屏障与写失败重试。

只依赖本包与更下层（`miniharness.core` / `miniharness.session`）：事件先入内存日志、
再经有界写后缓冲批量落盘；屏障返回才构成崩溃承诺；写失败时未落盘项留在队列里可重试，
日志不出现半条记录。
"""
from __future__ import annotations

import json
import os

import pytest

from capabilities.persistence.definition import PersistenceManager, Store
from capabilities.persistence.provider import PersistenceConsumer
from miniharness.core import Context
from miniharness.session import Session


def _harness(tmp_path, session_id: str = "s1",
             capacity: int | None = None) -> tuple[Context, Session, PersistenceManager]:
    manager = PersistenceManager(Store(str(tmp_path)))
    ctx = Context()
    session = Session()
    ctx.provide("session", session)
    kwargs = {} if capacity is None else {"buffer_capacity": capacity}
    ctx.load(PersistenceConsumer(manager, session_id=session_id, **kwargs))
    return ctx, session, manager


def test_flush_is_the_only_crash_promise(tmp_path):
    ctx, session, manager = _harness(tmp_path)
    log = manager.store.log_path("s1")

    session.append("user/message", content="在吗")
    assert not log.exists()                                # 未过屏障：事件还只在内存里

    assert ctx.get("persistence").flush() == 1             # 屏障返回：这一条确在盘上
    assert manager.load_events("s1") == session.events

    session.append("system/message", content="你拥有计算能力")   # 技能装载后的提示刷新
    assert manager.load_events("s1") == session.events[:-1]      # 屏障后的新事件尚未承诺

    assert ctx.get("persistence").flush() == 1
    assert manager.load_events("s1") == session.events           # 屏障返回即已落盘


def test_the_write_behind_buffer_is_bounded_and_lands_in_batches(tmp_path):
    """有界写后缓冲：未满不落盘；满容即批量落盘——一次写盘落一批，不是逐条写。"""
    ctx, session, manager = _harness(tmp_path, capacity=3)
    persistence = ctx.get("persistence")

    session.append("user/message", content="一")
    session.append("user/message", content="二")
    assert not manager.store.log_path("s1").exists()       # 缓冲未满：都还在内存里

    session.append("user/message", content="三")           # 满容 → 一次批量落盘
    assert persistence.saves == 1                          # 一次写盘落三条
    assert manager.load_events("s1") == session.events      # 前两条随这批一起落盘

    session.append("user/message", content="四")
    assert manager.load_events("s1") == session.events[:-1]  # 缓冲未满：第 4 条还没落盘
    assert persistence.flush() == 1                        # 屏障待落盘的正是这一条
    assert manager.load_events("s1") == session.events


def test_the_turn_boundary_is_the_barrier_for_model_visible_events(tmp_path):
    """模型可见事件不再逐条同步写：它们攒在缓冲里，到回合边界才过屏障。"""
    ctx, session, manager = _harness(tmp_path)
    persistence = ctx.get("persistence")

    session.append("turn/start", input="在吗")
    session.append("user/message", content="在吗")
    session.append("assistant/message", content="在的")
    assert not manager.store.log_path("s1").exists()       # 三条都还在缓冲里

    session.append("turn/end", status="done")              # 回合边界：屏障返回即已落盘
    assert persistence.saves == 1
    assert manager.load_events("s1") == session.events


def test_the_barrier_is_idempotent(tmp_path):
    ctx, session, manager = _harness(tmp_path)
    persistence = ctx.get("persistence")
    session.append("user/message", content="在吗")

    assert persistence.flush() == 1
    assert persistence.flush() == 0                        # 没有待落盘项：不触碰盘
    assert persistence.saves == 1
    lines = manager.store.log_path("s1").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines[1:]] == [1]   # 也没重复追加


def test_a_failed_write_keeps_unlanded_items_queued_for_retry(tmp_path, monkeypatch):
    """写失败不丢未落盘项：留在队列里，原地重试一次落全；不重复、不留半条。"""
    ctx, session, manager = _harness(tmp_path)
    persistence = ctx.get("persistence")
    session.append("user/message", content="一")
    session.append("user/message", content="二")

    real_save = manager.save_session

    def failing(*args, **kwargs):
        raise OSError("磁盘写失败")

    monkeypatch.setattr(manager, "save_session", failing)
    with pytest.raises(OSError):
        persistence.flush()

    assert persistence.saves == 0                          # 失败不计作一次落盘
    assert not manager.store.log_path("s1").exists()

    monkeypatch.setattr(manager, "save_session", real_save)
    assert persistence.flush() == 2                        # 两条都留在队列里：一次补齐
    assert persistence.flush() == 0                        # 队列已空：没有残留待落盘项
    assert manager.load_events("s1") == session.events
    assert len(manager.load_events("s1")) == 2             # 没有重复追加


def _failing_fsync(fd: int) -> None:
    raise OSError("磁盘写失败")


def test_a_retry_after_a_failed_write_never_duplicates_records(tmp_path, monkeypatch):
    """写失败后重试：水位从盘上重读——盘上已有的不重复追加（无撕裂的常见情形）。"""
    ctx, session, manager = _harness(tmp_path)
    persistence = ctx.get("persistence")
    session.append("user/message", content="一")
    session.append("user/message", content="二")

    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", _failing_fsync)
    with pytest.raises(OSError):
        persistence.flush()
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert persistence.flush() == 0                        # 两条都在盘上：没有要补的
    assert manager.load_events("s1") == session.events
    lines = manager.store.log_path("s1").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines[1:]] == [1, 2]   # 没有重复追加


def test_a_crash_torn_tail_is_repaired_on_the_next_barrier(tmp_path, monkeypatch):
    """崩溃模拟：写残的尾行不算已落盘的完整事件；下一次屏障修掉它并补齐，日志仍可解析、可重放。"""
    ctx, session, manager = _harness(tmp_path)
    persistence = ctx.get("persistence")
    session.append("turn/start", input="在吗")
    session.append("user/message", content="在吗")
    session.append("assistant/message", content="在的")

    real_fsync = os.fsync

    monkeypatch.setattr(os, "fsync", _failing_fsync)
    with pytest.raises(OSError):
        persistence.flush()
    monkeypatch.setattr(os, "fsync", real_fsync)

    log = manager.store.log_path("s1")
    log.write_text(log.read_text(encoding="utf-8")[:-10], encoding="utf-8")   # 崩溃撕裂尾行
    # 崩溃后的日志仍可解析、可重放：撕裂的尾行不算已落盘的事件，前面的照常读出
    assert manager.load_events("s1") == session.events[:-1]

    assert persistence.flush() == 1                        # 只补被撕裂的那条（水位从盘上重读）
    assert persistence.flush() == 0                        # 队列已空：没有残留待落盘项
    assert manager.load_events("s1") == session.events
    text = log.read_text(encoding="utf-8")
    assert text.endswith("\n")                             # 没有写残的半条记录
    assert [json.loads(line) for line in text.splitlines()[1:]] == session.events
    assert (Session(events=manager.load_events("s1")).derive_messages()
            == session.derive_messages())                  # 崩溃后仍可重放


def test_a_failed_write_never_loses_the_record_whose_line_was_torn(tmp_path, monkeypatch):
    """F1 回归：写残的尾行（完整 JSON、只丢结尾换行）遇上写入失败，重试后不丢也不重。

    残行**解析得动**，但不算已落盘的事件：失败收尾必须先把它从盘上修掉、再作废水位缓存。
    否则重读出的水位会高过盘上的真实记录，那条事件既进不了待落盘集合（返回 0），又会
    在下次写入前被 `_drop_torn_tail` 删掉——永久丢失，而屏障照常返回。
    """
    ctx, session, manager = _harness(tmp_path)
    persistence = ctx.get("persistence")
    session.append("user/message", content="一")
    assert persistence.flush() == 1                        # 第 1 条已落盘（水位 1）
    session.append("user/message", content="二")

    log = manager.store.log_path("s1")
    real_fsync = os.fsync

    def torn_fsync(fd: int) -> None:
        with open(log, "r+b") as f:                        # 崩溃现场：尾行只差结尾换行
            f.truncate(log.stat().st_size - 1)
        raise OSError("磁盘写失败")

    monkeypatch.setattr(os, "fsync", torn_fsync)
    with pytest.raises(OSError):
        persistence.flush()
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert manager.load_events("s1") == session.events[:-1]   # 残行不是已落盘的事件
    assert persistence.flush() == 1                           # 重试只补缺的那条
    assert manager.load_events("s1") == session.events        # 不丢
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines[1:]] == [1, 2]   # 不重
