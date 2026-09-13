# capabilities —— 能力族（策略：契约 / 实现 / 消费方三分）

这里是骨架之上的**策略**。每个能力一个目录，内部按角色与变化速率拆包：

- **Definition（契约）**：`<能力>/definition/` —— 稳定、低频的类型与语义；
- **Provider（实现）**：`<能力>/provider/` —— 具体策略，变动频繁；
- **Consumer（消费方）**：只经契约消费该能力的包。

契约与实现不同包，是为了让高频变动的策略不牵连稳定接口。若某条能力的契约**就是骨架上的
事件 seam**（例如权限决策、终结收尾协议），契约留在 `miniharness/`，本族只放实现——
不为了凑角色而建空壳包。

**已知偏差**：本轮的 `definition/` 由 legacy 语义模块整体搬移而来，**同时含类型与实现**，
所以上面这条「按变化速率分离」目前只是层级分离、不是内容分离，调阈值 / 改提示词 / 换存储
仍要动 `definition/`。为什么先这样、以及真正的二分为何是独立工作，见
[`docs/packaging.md`](../docs/packaging.md) §9「已知偏差」。

本文件是该族的**权威包地图**。落位、命名、依赖方向与测试放置的规范见
[`docs/packaging.md`](../docs/packaging.md)。

## 包地图：每个能力的三个角色落在哪个包

| 能力 | Definition（契约包） | Provider（实现包） | Consumer（消费方包） |
| --- | --- | --- | --- |
| `compaction` 压缩 | `capabilities.compaction.definition`（`CompactionConfig` / `ContextManager` / `to_text`） | `capabilities.compaction.provider`（`CompactionPlugin`、`stub_summarizer`） | `capabilities.persistence.provider`（落盘时读当前摘要）、`app.assembly`（恢复摘要） |
| `persistence` 持久化 | `capabilities.persistence.definition`（`Store` / `PersistenceManager` / 事件日志的格式版本与迁移链） | `capabilities.persistence.provider`（`PersistenceConsumer`，订阅 Session 日志落盘，`flush()` 是崩溃承诺的屏障） | `app.assembly`、`app.cli`（重放日志恢复会话、列出历史） |
| `retry` 重试 | `capabilities.retry.definition`（`is_retryable` / `with_retry`） | `capabilities.retry.provider`（`RetryPlugin`） | `miniharness.tools.runtime`（消费被包装后的调用结果） |
| `timeout` 超时 | `capabilities.timeout.definition`（`DEFAULT_TOOL_TIMEOUT` 与超时调用语义） | `capabilities.timeout.provider`（`ToolTimeout` / `ToolTimeoutPlugin`） | `miniharness.tools.runtime`（把超时收敛为结构化 error） |
| `validation` 输出校验 | `capabilities.validation.definition`（`sanitize_output` / `validate_output` 的注入检测与 schema 校验） | `capabilities.validation.provider`（`ValidationPlugin`） | `miniharness.tools.runtime`（消费被改写后的权威结果） |
| `tracing` 追踪 | `capabilities.tracing.definition`（`Span` / `AgentTracer`） | `capabilities.tracing.provider`（`TraceConsumer`） | `capabilities.persistence.provider`（把 span 一并落盘） |
| `skills` 技能 | `capabilities.skills.definition`（`Skill` / `SkillManager`） | `capabilities.skills.provider`（`SkillRegistry`） | `capabilities.skills.consumer`（`SystemPromptPlugin`）、`app.assembly`（恢复已激活技能） |
| `permission` 审批 | `miniharness.tools.runtime` 的 `tools/pre-execute` 决策词汇（`allow` / `ask` / `deny`） | `capabilities.permission.provider`（`PermissionPlugin`） | `miniharness.tools.runtime`（按决策决定是否执行工具体） |
| `final_output` 终结 | `miniharness.loop` 的 `agent/post-tool` 收尾协议 | `capabilities.final_output.provider`（`FinalOutputPlugin`） | `miniharness.loop`（按收尾协议结束本轮） |

每条能力内部的职责说明见各自的 `capabilities/<能力>/__init__.py`。

## 本族的测试

包内测试与实现同层、独立文件：`capabilities/<能力>/definition/test_*.py` 与
`capabilities/<能力>/provider/test_*.py`。它们只装「实现 + 一个工具流水线」，
并逐字对照契约包的语义（例如
`capabilities/retry/provider/test_retry.py` 同时跑 `with_retry` 与 `RetryPlugin`；
`capabilities/persistence/definition/test_log_format.py` 逐字读回日志文件与迁移链）。
跨包集成（装配整个 Loop）集中在 [`tests/`](../tests/README.md)。
