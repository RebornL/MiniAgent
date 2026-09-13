"""persistence —— 会话持久化能力。

- 契约：`definition`（`Store` / `PersistenceManager`：事件日志的物理形态、格式版本与迁移链）；
- 实现：`provider`（`PersistenceConsumer`，订阅 Session 日志落盘，`flush()` 是崩溃承诺的屏障）；
- 消费方：`app.assembly` / `app.cli`（重放日志恢复会话、列出历史）。

会话历史以事件日志（`events.v<N>.jsonl`）为权威源；模型可见内容与由它派生的状态
（压缩摘要、技能装载）都由日志重放重建。`meta.json` 只存可丢弃的展示用**计数**索引
（如消息条数），列表要看的「末条输入」由日志按需重算——盘上没有第二份模型可见内容。
旧代际写下的旁路状态（`active_skills` / `summary`）在迁移翻译期折进日志，此即其归宿。
"""