"""persistence.definition —— 会话持久化的契约。

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
格式演进走**相邻版本迁移链**：旧代际记录逐级翻译成当代事件日志；旧代际写下的
旁路状态（`meta.json` 的 `active_skills` / `summary`）在**翻译期**折进日志——
旁路就此废弃，此后运行时不读它（ADR-0001）。

落盘实现（迁移链、日志读写与 `Store` / `PersistenceManager`）见 `capabilities.persistence.provider`。
"""
from __future__ import annotations


__all__ = [
    "LOG_FORMAT",
    "LOG_VERSION",
    "LEGACY_LOG_VERSION",
    "log_filename",
]


# ═══════════════════════════════════════════════════════════════
# 事件日志的物理形态与格式版本
# ═══════════════════════════════════════════════════════════════
LOG_FORMAT = "miniharness-session-log"
LOG_VERSION = 2                 # 当代版本：写入只发布它
LEGACY_LOG_VERSION = 0          # v0 = messages.json：消息数组，无 header、无 seq


def log_filename(version: int = LOG_VERSION) -> str:
    """日志文件按格式代际命名：写入只发布当代版本，旧代际只作只读迁移源。"""
    return f"events.v{version}.jsonl"
