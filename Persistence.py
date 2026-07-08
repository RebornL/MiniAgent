import json
import os
import shutil
import time
# from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════
# 第三部分：持久化后端
# ═══════════════════════════════════════════════════════════════
class Store:
    """JSON 文件存储 —— 可替换为 SQLite / Redis / S3"""

    def __init__(self, dir_path: str = "./agent_sessions"):
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str, filename: str) -> Path:
        session_dir = self.dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir / filename

    # ── 读写 ──
    def save(self, session_id: str, filename: str, data: Any) -> None:
        path = self._path(session_id, filename)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)  # 原子写入，防止写一半崩溃

    def load(self, session_id: str, filename: str) -> Any:
        path = self._path(session_id, filename)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ── 会话管理 ──
    def list_sessions(self) -> list[dict]:
        sessions = []
        for d in sorted(self.dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta = self.load(d.name, "meta.json") or {}
            sessions.append({
                "id":            d.name,
                "created":       meta.get("created", ""),
                "updated":       meta.get("updated", ""),
                "last_message":  meta.get("last_message", ""),
                "message_count": meta.get("message_count", 0),
            })
        return sessions

    def delete_session(self, session_id: str) -> bool:
        path = self.dir / session_id
        if path.exists():
            shutil.rmtree(path)
            return True
        return False


# ═══════════════════════════════════════════════════════════════
# 第四部分：持久化管理器
# ═══════════════════════════════════════════════════════════════

class PersistenceManager:

    def __init__(self, store: Store | None = None):
        self.store = store or Store()

    # ── 会话 ID ──
    def new_session_id(self) -> str:
        return time.strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(4).hex()

    # ── 保存 ──
    def save_session(
        self, session_id: str,
        messages: list[dict],
        summary: str,
        runs: list[dict],
        last_user_input: str = "",
    ) -> None:
        """一次性保存全部状态"""
        old_meta = self._load_meta(session_id)
        self.store.save(session_id, "messages.json", messages)
        self.store.save(session_id, "traces.json", runs)
        self.store.save(session_id, "meta.json", {
            "created":       old_meta.get("created") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated":       time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_message":  last_user_input[:100],
            "message_count": len(messages),
            "summary":       summary,
        })

    def save_messages(self, session_id: str, messages: list[dict]) -> None:
        """仅保存 messages —— 高频操作，轻量"""
        self.store.save(session_id, "messages.json", messages)

    # ── 加载 ──
    def load_session(self, session_id: str) -> dict:
        return {
            "messages": self.store.load(session_id, "messages.json") or [],
            "summary":  self._load_meta(session_id).get("summary", ""),
            "runs":     self.store.load(session_id, "traces.json") or [],
        }

    def load_messages(self, session_id: str) -> list[dict]:
        return self.store.load(session_id, "messages.json") or []

    def _load_meta(self, session_id: str) -> dict:
        return self.store.load(session_id, "meta.json") or {}

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


