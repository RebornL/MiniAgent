"""persistence —— 会话持久化能力。

- 契约：`definition`（`Store` / `PersistenceManager`：事件日志的物理形态、格式版本与迁移链）；
- 实现：`provider`（`PersistenceConsumer`，订阅 Session 日志落盘，`flush()` 是崩溃承诺的屏障）；
- 消费方：`app.assembly` / `app.cli`（重放日志恢复会话、列出历史）。

会话历史以事件日志（`events.v<N>.jsonl`）为权威源，模型可见内容由它重放重建。
"""