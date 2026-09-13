"""persistence —— 会话持久化能力。

- 契约：`definition`（`Store` / `PersistenceManager`：事件日志的物理形态、格式版本与迁移链）；
- 实现：`provider`（`PersistenceConsumer`：有界写后缓冲 + `flush()` 屏障 + 三个语义检查点，
  事件成批落盘，`flush()` 返回才构成崩溃承诺）；
- 消费方：`app.assembly` / `app.cli`（重放日志恢复会话、列出历史）；
- 派发方：`miniharness.loop`——在「每步开始前 / 模型请求前 / 工具派发前」**派发
  `agent/checkpoint` seam 事件**，不 import 本能力的任何契约（检查点由本能力订阅）。

会话历史以事件日志（`events.v<N>.jsonl`）为权威源；模型可见内容与由它派生的状态
（压缩摘要、技能装载）都由日志重放重建。`meta.json` 只存可丢弃的展示用**计数**索引
（如消息条数），列表要看的「末条输入」由日志按需重算——盘上没有第二份模型可见内容。
旧代际写下的旁路状态（`active_skills` / `summary`）在迁移翻译期折进日志，此即其归宿。

写盘不随每次 append 同步发生：事件先入内存日志，再进有界写后缓冲，满容或回合边界批量
落盘；Loop 在「每步开始前 / 向模型发起请求前 / 顶层工具派发前」派发 `agent/checkpoint`，
本能力订阅它 fail-closed 地过屏障——屏障失败就不让下游的副作用发生。
"""