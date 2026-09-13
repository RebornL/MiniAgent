"""包内测试：事件日志的读取、追加与格式版本迁移。

只依赖本包与更下层（`miniharness.session`）：日志逐行可读、首行 header 带格式版本、
写入只发布当代版本；v0 的 `messages.json` 经相邻版本迁移链翻译成当代事件日志；
写路径校验既有代际（更高版本拒绝写入）、也修复首个 flush 崩溃留下的半份日志。
"""
from __future__ import annotations

import json

import pytest

from capabilities.persistence.definition import (
    LEGACY_MESSAGES_FILE,
    LOG_FORMAT,
    LOG_VERSION,
    PersistenceManager,
    Store,
)
from miniharness.session import Session


def _turn_events() -> list[dict]:
    session = Session()
    session.append("turn/start", input="在吗")
    session.append("user/message", content="在吗")
    session.append("assistant/message", content="在的")
    session.append("turn/end", status="done")
    return session.events


def test_log_is_one_readable_event_per_line_under_a_version_header(tmp_path):
    events = _turn_events()
    store = Store(str(tmp_path))
    pm = PersistenceManager(store)

    assert pm.append_events("s1", events) == len(events)

    header, *lines = store.log_path("s1").read_text(encoding="utf-8").splitlines()
    assert json.loads(header)["format"] == LOG_FORMAT
    assert json.loads(header)["version"] == LOG_VERSION

    records = [json.loads(line) for line in lines]
    assert records == events                               # 扁平的单条 JSON，且逐字相同
    assert [r["seq"] for r in records] == [1, 2, 3, 4]     # 单调递增的序号
    assert [r["type"] for r in records] == [
        "turn/start", "user/message", "assistant/message", "turn/end"]
    assert all("role" not in record for record in records)  # 落盘的不是消息数组
    assert pm.load_events("s1") == events


def test_appending_only_writes_the_events_that_are_not_on_disk_yet(tmp_path):
    """水位语义：已落盘的事件不会重复追加（重复屏障幂等）。"""
    session = Session()
    session.append("user/message", content="在吗")
    pm = PersistenceManager(Store(str(tmp_path)))

    assert pm.append_events("s1", session.events) == 1
    assert pm.append_events("s1", session.events) == 0

    session.append("assistant/message", content="在的")
    assert pm.append_events("s1", session.events) == 1     # 只追加新的那条
    assert pm.append_events("s1", session.events) == 0

    assert pm.load_events("s1") == session.events


def test_a_torn_last_line_is_not_an_event_and_is_repaired_before_appending(tmp_path):
    """崩溃写残的尾行不是已落盘的完整事件：读时丢弃，写前修掉，不留半条记录。"""
    session = Session()
    session.append("user/message", content="在吗")
    pm = PersistenceManager(Store(str(tmp_path)))
    pm.append_events("s1", session.events)

    path = Store(str(tmp_path)).log_path("s1")
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"seq": 2, "type": "assistant/mess')      # 写到一半崩溃

    assert pm.load_events("s1") == session.events          # 残行不算事件

    session.append("assistant/message", content="在的")
    assert pm.append_events("s1", session.events) == 1
    assert pm.load_events("s1") == session.events
    # 残行被截掉，日志里只剩那条完整的 assistant/message
    assert path.read_text(encoding="utf-8").count("assistant/mess") == 1


def test_a_flush_that_died_before_the_header_is_treated_as_never_written(tmp_path):
    """首个 flush 在写出 header 之前崩溃：半份日志整份视同没落过盘，重写 header 后照常读写。

    没有 header 的日志既不是权威源（读不出东西），也不挡住后续 flush——否则这个会话
    就永久卡在「读不了也写不了」。
    """
    events = _turn_events()
    store = Store(str(tmp_path))
    pm = PersistenceManager(store)
    (tmp_path / "s1").mkdir()
    (tmp_path / "s2").mkdir()
    empty = store.log_path("s1")
    empty.touch()                                          # open(path, "a") 建了文件，flush 前崩溃
    torn = store.log_path("s2")
    torn.write_text('{"format": "miniharness-sess', encoding="utf-8")

    for session_id, path in (("s1", empty), ("s2", torn)):
        assert pm.load_events(session_id) == []            # 没有完整 header → 什么都没落盘
        assert pm.append_events(session_id, events) == len(events)
        assert pm.load_events(session_id) == events        # 重写 header 后读得回来
        assert pm.append_events(session_id, events) == 0   # 水位从重写后的日志算起
        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert header["format"] == LOG_FORMAT
        assert header["version"] == LOG_VERSION


def test_appending_refuses_to_write_below_a_newer_generation(tmp_path):
    """盘上有更高代际的日志：拒绝写入，不改名降级，也不在旁边另写一份当代日志。"""
    store = Store(str(tmp_path))
    pm = PersistenceManager(store)
    (tmp_path / "s1").mkdir()
    newer = store.log_path("s1", version=LOG_VERSION + 1)
    original = json.dumps({"format": LOG_FORMAT, "version": LOG_VERSION + 1}) + "\n"
    newer.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="拒绝写入"):
        pm.append_events("s1", _turn_events())

    assert newer.read_text(encoding="utf-8") == original   # 更高版本的历史原样留着
    assert not store.log_path("s1").exists()               # 也没有偷偷写一份旧代际


def test_legacy_messages_json_is_translated_and_later_writes_publish_the_current_version(tmp_path):
    """v0（消息数组）→ v1（事件日志）：旧会话仍可恢复，其后的写入只发布当代版本。"""
    messages = [
        {"role": "user", "content": "16 * 2 是多少"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "calculate", "args": {"expression": "16 * 2"}}]},
        {"role": "tool", "content": "32", "tool_call_id": "c1"},
        {"role": "assistant", "content": "16 * 2 = 32"},
    ]
    session_dir = tmp_path / "old"
    session_dir.mkdir()
    legacy = session_dir / LEGACY_MESSAGES_FILE
    legacy.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")

    store = Store(str(tmp_path))
    pm = PersistenceManager(store)
    events = pm.load_events("old")

    # 翻译等价：模型可见历史一字不差，且补上了单调序号
    assert Session(events=events).derive_messages() == messages
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert legacy.exists()                                 # 纯读不改盘

    events.append({"seq": 5, "type": "turn/end", "status": "done"})
    assert pm.append_events("old", events) == 5            # 首次写入把整份日志落成当代版本

    # 此后当代日志就是权威源：逐字往返，模型可见历史仍与原消息数组等价
    assert pm.load_events("old") == events
    assert Session(events=pm.load_events("old")).derive_messages() == messages


def test_a_log_this_build_cannot_read_is_refused_instead_of_misread(tmp_path):
    """更高版本，或文件名与 header 声明的版本不符：宁可报错，也不按旧语义误读。"""
    store = Store(str(tmp_path))
    (tmp_path / "s1").mkdir()
    store.log_path("s1", version=LOG_VERSION + 1).write_text(
        json.dumps({"format": LOG_FORMAT, "version": LOG_VERSION + 1}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="拒绝误读"):       # 更高版本：没有迁移步骤
        store.load_log("s1")

    (tmp_path / "s2").mkdir()
    store.log_path("s2").write_text(
        json.dumps({"format": LOG_FORMAT, "version": LOG_VERSION + 1}) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="拒绝误读"):       # 代际文件名与 header 不符
        store.load_log("s2")
