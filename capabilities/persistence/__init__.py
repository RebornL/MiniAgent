"""persistence —— 会话持久化能力。

- 契约：`definition`（`Store` / `PersistenceManager`）；
- 实现：`provider`（`PersistenceConsumer`，订阅 Session 日志落盘）；
- 消费方：`app.assembly` / `app.cli`（恢复会话、列出历史）。
"""