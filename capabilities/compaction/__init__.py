"""compaction —— 上下文压缩能力。

- 契约：`definition`（`CompactionConfig` / `ContextManager` / `to_text`）；
- 实现：`provider`（`CompactionPlugin`，订阅 `agent/pre-step` 做 surface 替换）；
- 消费方：`capabilities.persistence.provider`（落盘时读取当前摘要）与 `app.assembly`（恢复摘要）。
"""