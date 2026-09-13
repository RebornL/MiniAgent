"""persistence.definition —— 会话持久化的契约与落盘实现。

会话历史以**事件日志**落盘，而不是模型可见历史的数组副本：

- `events.v<N>.jsonl`：权威事件日志。首行是 header（`format` / `version` / `created`），
  其后逐行一条事件（`seq` 单调递增 + `type`）；写入只发布**当代版本**；
- `meta.json`：展示用的派生索引——`message_count` 这类**计数**与创建 / 更新时刻。
  它不存模型可见内容（列表要展示的「末条输入」按需由日志重算），也不存恢复所需的
  状态：压缩摘要与技能装载都是日志里的事件，恢复靠重放；
- `traces.json`：执行追踪留档（span 的输入 / 输出），供展示与诊断，不参与恢复；
- `messages.json`：v0 的历史格式（消息数组），只在没有当代日志时作迁移源，只读。

模型可见内容与由它派生的状态（压缩摘要、技能装载）一律由日志重放重建
（`Session.replay` + 各能力自己的重放入口），因此磁盘上不存在并行的模型可见历史。
格式演进走**相邻版本迁移链**（`translate`）：旧代际记录逐级翻译成当代事件日志；
旧代际写下的旁路状态（`meta.json` 的 `active_skills` / `summary`）在**翻译期**由
`_fold_legacy_meta` 折进日志——旁路就此废弃，此后运行时不读它（ADR-0001）。

（本包同时含契约与落盘实现，与 `docs/packaging.md` §9「已知偏差」一致。）
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from capabilities.skills.definition import LOADED_EVENT, UNLOADED_EVENT
from miniharness.session import COMPACTED_EVENT, Session, compaction_replacement

__all__ = [
    "LOG_FORMAT",
    "LOG_VERSION",
    "LEGACY_LOG_VERSION",
    "log_filename",
    "translate",
    "read_log",
    "Store",
    "PersistenceManager",
]


# ═══════════════════════════════════════════════════════════════
# 事件日志的物理形态与格式版本
# ═══════════════════════════════════════════════════════════════
LOG_FORMAT = "miniharness-session-log"
LOG_VERSION = 2                 # 当代版本：写入只发布它
LEGACY_LOG_VERSION = 0          # v0 = messages.json：消息数组，无 header、无 seq

META_FILE = "meta.json"
TRACES_FILE = "traces.json"
LEGACY_MESSAGES_FILE = "messages.json"
MIGRATED_SUFFIX = ".migrated"


def log_filename(version: int = LOG_VERSION) -> str:
    """日志文件按格式代际命名：写入只发布当代版本，旧代际只作只读迁移源。"""
    return f"events.v{version}.jsonl"


def _v0_to_v1(records: list[dict]) -> list[dict]:
    """v0（消息数组）→ v1（事件日志）：逆投影成带单调序号的事件。"""
    return Session.session_from_messages(records).events


def _v1_to_v2(records: list[dict]) -> list[dict]:
    """v1 → v2：压缩事件补上「被遮蔽的事件范围」与「替换内容」。

    v1 的压缩事件只有 `summary` + `replaced_seqs`，替换内容是投影时现渲染的；
    v2 把范围（`shadowed_seqs` / `shadowed_range`）与替换内容（`replacement`）都记进事件，
    投影因此不再需要重新渲染——翻译时按 v1 的渲染规则把它固化成替换内容。
    """
    for record in records:
        if record.get("type") != COMPACTED_EVENT:
            continue
        seqs = sorted(set(record.pop("replaced_seqs", None) or []))
        record["shadowed_seqs"] = seqs
        record["shadowed_range"] = {"start": seqs[0], "end": seqs[-1]} if seqs else None
        record["replacement"] = compaction_replacement(record.get("summary", ""))
    return records


_MIGRATIONS: dict[int, Callable[[list[dict]], list[dict]]] = {
    LEGACY_LOG_VERSION: _v0_to_v1,
    1: _v1_to_v2,
}


def translate(records: list[dict], version: int) -> list[dict]:
    """相邻版本迁移链：把 `version` 代的记录逐级翻译成当代事件日志。

    高于当代的版本一律拒绝——宁可报错，也不按旧语义误读新记录。
    """
    if version > LOG_VERSION:
        raise ValueError(f"日志版本 v{version} 高于本实现支持的 v{LOG_VERSION}，拒绝误读")
    while version < LOG_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:
            raise ValueError(f"缺少 v{version} → v{version + 1} 的迁移步骤")
        records, version = step(records), version + 1
    return records


#: 技能状态事件：`active_skills` 旁路要折成的事件名（词汇表在 `capabilities.skills.definition`）。
_SKILL_EVENTS = (LOADED_EVENT, UNLOADED_EVENT)


def _fold_legacy_meta(meta: dict, records: list[dict]) -> list[dict]:
    """把旧代际的旁路状态（`meta.json` 的 `active_skills` / `summary`）折进翻译出的日志。

    补丁前的会话不写 `skill/*` 事件，装载了哪些技能只记在 meta 的旁路里；v0 的增量摘要
    也没有 `context/compacted` 事件可落。旁路因此成了第二份状态源——旧会话一旦迁移，
    技能工具没注册、摘要链从空重启。这里在**翻译期**把它折成对应事件，折完旁路即弃：
    此后日志是唯一权威源，运行时不读旁路（ADR-0001）。

    只在日志**无法表达**该状态时才折：日志里已有 `skill/*` 事件（技能状态自明）就不再
    折技能，已有 `context/compacted` 事件（摘要链自明）就不再折摘要——否则等于拿过时的
    旁路盖在日志上。合成的事件接着日志的最后一个 `seq` 追加。
    """
    events = list(records)
    seq = max((event.get("seq", 0) for event in events), default=0)

    def append(kind: str, **data: Any) -> None:
        nonlocal seq
        seq += 1
        events.append({"seq": seq, "type": kind, **data})

    if "active_skills" in meta and not any(e.get("type") in _SKILL_EVENTS for e in events):
        for name in meta.get("active_skills") or []:
            append(LOADED_EVENT, name=name)
    if "summary" in meta and not any(e.get("type") == COMPACTED_EVENT for e in events):
        # 仅用于保留的压缩事件：遮蔽范围与替换内容都为空，投影完全忽略它（不遮蔽任何
        # 事件、不注入替换内容），但 `compaction_summaries` 会把它读回摘要链。
        append(COMPACTED_EVENT, summary=meta.get("summary", ""),
               shadowed_seqs=[], shadowed_range=None, replacement=[])
    return events


def read_log(path: Path) -> tuple[int, list[dict]]:
    """读一份日志，返回 `(格式版本, 事件记录)`；版本取自 header（权威），不翻译、不改写。

    首行必须是 header；完整记录的行必然以 `\\n` 结尾，所以**缺结尾换行的最后一行**是崩溃
    写残的（它不是一条已落盘的完整事件），整行丢弃——这与写前的 `_drop_torn_tail` 是同一
    判定。两者若不一致，冷启动读出的水位就会高于盘上的真实形状（那条事件将再也补不回来）。
    缺结尾换行之外的坏行是真损坏，直接报错。
    """
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):        # 与 `_drop_torn_tail` 同一判定：写残的尾行
        data = data[:data.rfind(b"\n") + 1]
    lines = [line for line in data.decode("utf-8").splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"日志 {path.name} 缺少完整 header（空文件或写残的 header）")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"日志 {path.name} 的 header 无法解析：{exc}") from exc
    if header.get("format") != LOG_FORMAT:
        raise ValueError(f"日志 {path.name} 不是 {LOG_FORMAT} 格式")
    version = header.get("version")
    if not isinstance(version, int):
        raise ValueError(f"日志 {path.name} 的 header 缺少整数 version")

    records: list[dict] = []
    for number, line in enumerate(lines[1:], start=2):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            # 写残的尾行已在上面整行丢弃，能走到这里的都是真损坏
            raise ValueError(f"日志 {path.name} 第 {number} 行损坏：{exc}") from None
    return version, records


# ═══════════════════════════════════════════════════════════════
# 第三部分：持久化后端
# ═══════════════════════════════════════════════════════════════
class Store:
    """会话存储 —— 目录布局 + 事件日志的追加式落盘（可替换为 SQLite / Redis / S3）。

    「已落盘的最后一个 seq」是水位：只把高于它的事件追加进当代日志，重复调用幂等。
    """

    def __init__(self, dir_path: str = "./agent_sessions"):
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._written: dict[str, int] = {}      # 每会话已落盘的最后一个 seq

    def _session_dir(self, session_id: str, create: bool = False) -> Path:
        session_dir = self.dir / session_id
        if create:
            session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def _path(self, session_id: str, filename: str) -> Path:
        return self._session_dir(session_id, create=True) / filename

    # ── 元数据读写（沿用 legacy 的原子 JSON 落盘） ──
    def save(self, session_id: str, filename: str, data: Any) -> None:
        path = self._path(session_id, filename)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)  # 原子写入，防止写一半崩溃

    def load(self, session_id: str, filename: str) -> Any:
        path = self._session_dir(session_id) / filename
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ── 事件日志 ──
    def log_path(self, session_id: str, version: int = LOG_VERSION,
                 create: bool = False) -> Path:
        return self._session_dir(session_id, create=create) / log_filename(version)

    def load_log(self, session_id: str) -> list[dict]:
        """读会话的事件日志（已翻译成当代版本）；没有日志则为空。

        当代日志优先；没有它时依次尝试更早代际的日志文件与 v0 的 `messages.json`，
        都经迁移链翻译——旧会话因此仍可恢复。当代文件若没有完整的 header（首个 flush
        落盘到一半就崩了），按「整份都没落盘过」处理：既不当作日志读，也不挡住后备迁移源。
        旧代际的旁路状态（`meta.json` 的 `active_skills` / `summary`）在翻译期折进
        翻译结果（`_fold_legacy_meta`）：恢复只认日志，运行时与装配层都不再读旁路。
        """
        path = self.log_path(session_id)
        if _has_complete_header(path):
            version, records = self._read_generation(path)
            return translate(records, version)
        for older in self._older_logs(session_id):
            version, records = self._read_generation(older)
            return self._fold_legacy_meta(session_id, translate(records, version))
        legacy = self._session_dir(session_id) / LEGACY_MESSAGES_FILE
        if legacy.exists():
            records = translate(json.loads(legacy.read_text(encoding="utf-8")),
                                LEGACY_LOG_VERSION)
            return self._fold_legacy_meta(session_id, records)
        return []

    def _fold_legacy_meta(self, session_id: str, records: list[dict]) -> list[dict]:
        """旧代际日志的迁移收尾：把 meta 里的旁路状态折进翻译出的记录。"""
        return _fold_legacy_meta(self.load(session_id, META_FILE) or {}, records)

    def _read_generation(self, path: Path) -> tuple[int, list[dict]]:
        """读一份代际日志：文件名与 header 声明的版本必须一致，否则拒绝误读。"""
        version, records = read_log(path)
        if _version_of(path) != version:
            raise ValueError(
                f"日志 {path.name} 的 header 版本 v{version} 与文件名不一致，拒绝误读")
        return version, records

    def append_log(self, session_id: str, events: list[dict],
                   created: str | None = None) -> int:
        """把尚未落盘的事件追加进当代日志，返回写入条数。

        写前先看盘上有什么：盘上有更高代际的日志就拒绝写入（宁可报错，也不改名降级、
        覆盖更高版本的历史）；当代日志若没有完整可解析的 header（首个 flush 落盘到一半
        就崩了），整份视同没落过盘并重写 header。首次写入补 header，并把本会话的旧格式
        记录标记为已迁移（改名不删档）；写入后 fsync：返回时事件确在盘上。写残的尾行在
        写前修掉，不留半条记录。写失败时先修掉写残的尾行、再作废水位缓存：盘上可能只落了
        半批，下一次写入重读盘上的水位，只补真正缺的部分（不重复追加、也不丢未落盘项）。
        """
        self._refuse_newer_generation(session_id)
        watermark = self._watermark(session_id)
        pending = [event for event in events if event.get("seq", 0) > watermark]
        path = self.log_path(session_id, create=True)
        fresh = not _has_complete_header(path)
        if fresh:
            mode = "w"                 # 残 header / 0 字节：整份重写，不留半份 header
        else:
            mode = "a"
            _drop_torn_tail(path)      # 写残的尾行不是已落盘的事件，写前修掉

        try:
            with open(path, mode, encoding="utf-8") as f:
                if fresh:
                    header = {"format": LOG_FORMAT, "version": LOG_VERSION,
                              "session_id": session_id,
                              "created": created or time.strftime("%Y-%m-%d %H:%M:%S")}
                    f.write(json.dumps(header, ensure_ascii=False) + "\n")
                for event in pending:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            # 半批落盘 / 中断：先修掉写残的尾行再作废水位缓存——残行不算已落盘的事件，
            # 留在盘上会让重读出的水位高过盘上的真实记录，那条事件就再也补不回来了。
            _drop_torn_tail(path)
            self._written.pop(session_id, None)
            raise

        if pending:
            self._written[session_id] = max(pending[-1]["seq"], watermark)
        if fresh:
            self._mark_migrated(session_id)
        return len(pending)

    def _watermark(self, session_id: str) -> int:
        """已落盘的最后一个 seq（当代日志的最高 seq；没有当代日志则为 0）。"""
        if session_id not in self._written:
            path = self.log_path(session_id)
            # 没有完整 header 的文件整份都没落过盘：不能拿它当水位，也读不了它
            records = read_log(path)[1] if _has_complete_header(path) else []
            self._written[session_id] = max((r.get("seq", 0) for r in records), default=0)
        return self._written[session_id]

    def _refuse_newer_generation(self, session_id: str) -> None:
        """写前校验既有代际：盘上有比当代更高的版本就拒绝写入。

        更高版本只有未来的实现能解释：既不能改名降级（那会静默丢掉它的历史），也不能在它
        旁边另写一份旧代际（两份日志谁权威就说不清了）。
        """
        for path in self._older_logs(session_id):
            version = _version_of(path)
            if version > LOG_VERSION:
                raise ValueError(
                    f"日志 {path.name} 的版本 v{version} 高于本实现支持的 v{LOG_VERSION}，"
                    f"拒绝写入以免覆盖更高版本的历史")

    def _older_logs(self, session_id: str) -> list[Path]:
        """更早代际的日志文件（按版本降序）：只在没有当代日志时作迁移源。"""
        current = log_filename()
        return sorted((p for p in self._session_dir(session_id).glob("events.v*.jsonl")
                       if p.name != current),
                      key=_version_of, reverse=True)

    def _mark_migrated(self, session_id: str) -> None:
        """迁移落定：旧格式记录改名 `*.migrated`，此后只有当代日志是权威源。

        只改名不删除——旧记录留档可查，但不再被读，也不再是「并行的模型可见历史」。
        """
        session_dir = self._session_dir(session_id)
        stale = self._older_logs(session_id) + [session_dir / LEGACY_MESSAGES_FILE]
        for path in stale:
            if path.exists():
                path.replace(path.with_name(path.name + MIGRATED_SUFFIX))

    # ── 会话管理 ──
    def list_sessions(self) -> list[dict]:
        sessions = []
        for d in sorted(self.dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta = self.load(d.name, META_FILE) or {}
            sessions.append({
                "id":            d.name,
                "created":       meta.get("created", ""),
                "updated":       meta.get("updated", ""),
                "last_message":  self._last_user_message(d.name),
                "message_count": meta.get("message_count", 0),
            })
        return sessions

    def _last_user_message(self, session_id: str) -> str:
        """列表展示用的「末条用户输入」：按需从日志重算，而不是在盘上另存一份内容。"""
        contents = [event.get("content", "") for event in self.load_log(session_id)
                    if event.get("type") == "user/message"]
        return contents[-1][:100] if contents else ""

    def delete_session(self, session_id: str) -> bool:
        self._written.pop(session_id, None)
        path = self.dir / session_id
        if path.exists():
            shutil.rmtree(path)
            return True
        return False


def _has_complete_header(path: Path) -> bool:
    """文件是否已经落下一份完整可解析的 header（读与写都据此判「整份落过盘没有」）。

    `open(path, "a")` 会先创建文件，所以「文件存在」不等于「落过盘」：首个 flush 在写出
    header 之前崩溃，盘上留下的就是 0 字节或半行 header。写入按顺序落盘，因此没有完整
    header 的文件里也不会有完整的事件——按「整份都没落盘过」处理是安全的。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            first_line = f.readline()
    except OSError:
        return False
    if not first_line.endswith("\n"):       # 半行 header：还没写完，不算数
        return False
    try:
        header = json.loads(first_line)
    except json.JSONDecodeError:
        return False
    return (isinstance(header, dict) and header.get("format") == LOG_FORMAT
            and isinstance(header.get("version"), int))


def _version_of(path: Path) -> int:
    """从代际文件名 `events.v<N>.jsonl` 取出版本号。"""
    return int(path.name[len("events.v"):-len(".jsonl")])


def _drop_torn_tail(path: Path) -> None:
    """写前修掉写残的尾行：完整记录必然以 `\\n` 结尾，没结尾的那段不是已落盘的事件。

    文件不存在（还没落过盘，或失败发生在建文件之前）就无事可做。
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return
    data = path.read_bytes()
    path.write_bytes(data[:data.rfind(b"\n") + 1])   # 0 表示整份文件只有一条残行：截到开头


# ═══════════════════════════════════════════════════════════════
# 第四部分：持久化管理器
# ═══════════════════════════════════════════════════════════════
class PersistenceManager:
    """会话持久化：事件日志是权威源，模型可见内容与由此派生的状态只能由它重放重建。

    - `save_session` / `append_events` 只把尚未落盘的事件追加进当代日志（幂等）；
    - `load_session` / `load_events` 读日志，必要时把旧格式翻译成当代版本。
    `meta.json` 只存展示用的派生索引（消息条数这类计数与时刻），压缩摘要与技能状态
    不在这里——它们是日志里的事件，由 `CompactionPlugin.restore` / `SkillRegistry.restore`
    从同一份日志重放；列表要展示的「末条输入」也按需由日志重算，盘上没有第二份内容。
    """

    def __init__(self, store: Store | None = None):
        self.store = store or Store()

    # ── 会话 ID ──
    def new_session_id(self) -> str:
        return time.strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(4).hex()

    # ── 保存 ──
    def save_session(
        self, session_id: str,
        events: list[dict],
        runs: list[dict],
    ) -> int:
        """落盘一次：事件进当代日志，随后刷新 meta / traces。返回本次写入的事件条数。

        meta 只刷新派生索引（计数与时刻）——模型可见内容不落 meta，列表要展示的
        「末条输入」由 `Store.list_sessions` 按需从日志重算。
        """
        old_meta = self._load_meta(session_id)
        written = self.store.append_log(session_id, events, created=old_meta.get("created"))
        self.store.save(session_id, TRACES_FILE, runs)
        self.store.save(session_id, META_FILE, {
            "created":        old_meta.get("created") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated":        time.strftime("%Y-%m-%d %H:%M:%S"),
            # 展示用的派生索引：由日志投影算出，可随时重建
            "message_count":  len(Session(events=list(events)).derive_messages()),
        })
        return written

    def append_events(self, session_id: str, events: list[dict]) -> int:
        """只把事件追加进当代日志 —— 高频操作，轻量（meta / traces 不动）"""
        return self.store.append_log(session_id, events)

    # ── 加载 ──
    def load_session(self, session_id: str) -> dict:
        """重放源：事件日志 + 派生追踪。模型可见状态不在这里，只能由日志重放算出。"""
        return {
            "events": self.load_events(session_id),
            "runs": self.store.load(session_id, TRACES_FILE) or [],
        }

    def load_events(self, session_id: str) -> list[dict]:
        """重放源：会话的完整事件日志（旧格式已翻译成当代版本）。"""
        return self.store.load_log(session_id)

    def _load_meta(self, session_id: str) -> dict:
        return self.store.load(session_id, META_FILE) or {}

    # ── 会话管理 ──
    def list_sessions(self) -> list[dict]:
        return self.store.list_sessions()

    def delete_session(self, session_id: str) -> bool:
        return self.store.delete_session(session_id)


# ═══════════════════════════════════════════════════════════════
# 第八部分：辅助接口
# ═══════════════════════════════════════════════════════════════

def list_sessions(store_dir: str = "./agent_sessions") -> list[dict]:
    """列出所有历史会话"""
    return PersistenceManager(Store(store_dir)).list_sessions()


def delete_session(session_id: str, store_dir: str = "./agent_sessions") -> bool:
    """删除指定会话"""
    return Store(store_dir).delete_session(session_id)
