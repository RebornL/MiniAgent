"""compaction —— 上下文压缩能力。

- 契约：`definition`（压缩状态的恢复语义：`compaction_summaries`——从事件日志投影摘要链，
  末项是当前增量摘要、条数是累计压缩次数）；
- 实现：`provider`（`CompactionConfig` / `ContextManager`：阈值 / 提示词 / 切分策略；
  `CompactionPlugin` 订阅 `agent/pre-step`，把被遮蔽范围 + 替换内容 + 摘要写成一条
  `context/compacted` 事件，摘要函数可注入，默认 `stub_summarizer`）；
- 消费方：`miniharness.session`（把压缩事件投影成 surface 替换）与 `app.assembly`
  （重放压缩事件恢复增量摘要链）。
"""
