"""compaction —— 上下文压缩能力。

- 契约：`definition`（`CompactionConfig` / `ContextManager` / `to_text` / `compaction_summaries`）；
- 实现：`provider`（`CompactionPlugin`，订阅 `agent/pre-step`，把被遮蔽范围 + 替换内容 +
  摘要写成一条 `context/compacted` 事件）；
- 消费方：`miniharness.session`（把压缩事件投影成 surface 替换）与 `app.assembly`
  （重放压缩事件恢复增量摘要链）。
"""
