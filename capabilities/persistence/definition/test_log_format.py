"""包内测试：事件日志的读取、追加与格式版本迁移。

只依赖本包与更下层（`miniharness.session`）：日志逐行可读、首行 header 带格式版本、
写入只发布当代版本；v0 的 `messages.json` 经相邻版本迁移链翻译成当代事件日志；
旧代际写在 `meta.json` 里的旁路状态在翻译期折进日志（日志已能自明时不再折）；
写路径校验既有代际（更高版本拒绝写入）、也修复首个 flush 崩溃留下的半份日志。
"""
from __future__ import annotations

import json

import pytest

from capabilities.persistence.definition import (
    LEGACY_MESSAGES_FILE,
    LOG_FORMAT,
    LOG_VERSION,
    META_FILE,
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


def test_legacy_bypass_state_is_folded_into_the_translated_log(tmp_path):
    """旧代际的旁路（`meta.json` 的 `active_skills` / `summary`）在**翻译期**折进日志。

    旧日志里既没有 `skill/*` 事件、也没有 `context/compacted` 事件，装载状态与摘要链
    只在旁路里；折进来后它们才是可重放的——运行时不读旁路（日志是唯一权威源，ADR-0001）。
    """
    _write_v1_log(tmp_path, "old", [
        {"seq": 1, "type": "user/message", "content": "第一问"},
        {"seq": 2, "type": "assistant/message", "content": "第一答"},
    ])
    store = Store(str(tmp_path))
    store.save("old", META_FILE, {"active_skills": ["math"], "summary": "第一份摘要"})

    events = store.load_log("old")

    assert events[2:] == [                                   # 折进来的事件接着日志的 seq
        {"seq": 3, "type": "skill/loaded", "name": "math"},
        {"seq": 4, "type": "context/compacted", "summary": "第一份摘要",
         "shadowed_seqs": [], "shadowed_range": None, "replacement": []},
    ]
    # 「仅用于保留」的压缩事件不遮蔽任何事件、也不注入替换内容：投影逐字不变
    assert Session(events=events).derive_messages() == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]


def test_a_log_that_speaks_for_itself_is_never_overridden_by_the_legacy_meta(tmp_path):
    """日志已能表达状态时不再折叠旁路：过时的 meta 不许盖在日志上（那又是两份真相源）。"""
    _write_v1_log(tmp_path, "old", [
        {"seq": 1, "type": "user/message", "content": "第一问"},
        {"seq": 2, "type": "skill/loaded", "name": "math"},
        {"seq": 3, "type": "context/compacted", "summary": "日志里的摘要", "replaced_seqs": [1]},
    ])
    Store(str(tmp_path)).save("old", META_FILE, {
        "active_skills": ["math", "calc"], "summary": "旁路里的摘要"})

    events = Store(str(tmp_path)).load_log("old")

    assert events == [                                       # 只有 v1 → v2 的迁移，旁路一个字没进来
        {"seq": 1, "type": "user/message", "content": "第一问"},
        {"seq": 2, "type": "skill/loaded", "name": "math"},
        {"seq": 3, "type": "context/compacted", "summary": "日志里的摘要",
         "shadowed_seqs": [1], "shadowed_range": {"start": 1, "end": 1},
         "replacement": [{"role": "user", "content": "[上下文已压缩] 日志里的摘要"}]},
    ]


def _write_v1_log(tmp_path, session_id: str, records: list[dict]) -> None:
    """落一份 v1 代际日志（旧日志没有 `skill/*` / `context/compacted` 时的旁路才需要折叠）。"""
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "events.v1.jsonl").write_text(
        json.dumps({"format": LOG_FORMAT, "version": 1, "session_id": session_id}) + "\n"
        + "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8")


def test_v1_compaction_events_are_translated_to_the_current_shape(tmp_path):
    """v1 的压缩事件（`summary` + `replaced_seqs`）翻译成当代形态：遮蔽与替换语义不变。"""
    _write_v1_log(tmp_path, "old", [
        {"seq": 1, "type": "user/message", "content": "第一问"},
        {"seq": 2, "type": "assistant/message", "content": "第一答"},
        {"seq": 3, "type": "user/message", "content": "第二问"},
        {"seq": 4, "type": "context/compacted", "summary": "第一份摘要",
         "replaced_seqs": [1, 2]},
    ])

    events = PersistenceManager(Store(str(tmp_path))).load_events("old")

    compacted = events[-1]
    assert compacted["shadowed_seqs"] == [1, 2]             # 被遮蔽的事件范围
    assert compacted["shadowed_range"] == {"start": 1, "end": 2}
    assert compacted["replacement"] == [                    # 替换内容：按 v1 的渲染规则固化
        {"role": "user", "content": "[上下文已压缩] 第一份摘要"}]
    assert "replaced_seqs" not in compacted                 # 旧字段名不再出现
    # 翻译后的投影与 v1 语义一致：被遮蔽事件留档，投影里只剩替换内容与未遮蔽事件
    assert Session(events=events).derive_messages() == [
        {"role": "user", "content": "[上下文已压缩] 第一份摘要"},
        {"role": "user", "content": "第二问"},
    ]


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
