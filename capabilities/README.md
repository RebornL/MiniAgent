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
| `compaction` 压缩 | `capabilities.compaction.definition`（`CompactionConfig` / `ContextManager` / `to_text` / `compaction_summaries`） | `capabilities.compaction.provider`（`CompactionPlugin`、`stub_summarizer`） | `miniharness.session`（把压缩事件投影成 surface 替换）、`app.assembly`（重放日志恢复摘要链） |
| `persistence` 持久化 | `capabilities.persistence.definition`（`Store` / `PersistenceManager` / 事件日志的格式版本与迁移链） | `capabilities.persistence.provider`（`PersistenceConsumer`：有界写后缓冲 + `flush()` 屏障，订阅 `agent/checkpoint` 在三个语义点 fail-closed 落盘） | `app.assembly`、`app.cli`（重放日志恢复会话、列出历史）；`miniharness.loop` 只**派发 `agent/checkpoint` seam 事件**（每步开始前 / 模型请求前 / 工具派发前），不 import 本能力的任何契约 |
| `retry` 重试 | `capabilities.retry.definition`（`is_retryable` / `is_retryable_outcome` / `with_retry`） | `capabilities.retry.provider`（`RetryPlugin`） | `miniharness.tools.runtime`（消费被包装后的调用结果） |
| `timeout` 超时 | `capabilities.timeout.definition`（`DEFAULT_TOOL_TIMEOUT` 与超时调用语义） | `capabilities.timeout.provider`（`ToolTimeoutPlugin`） | `miniharness.tools.runtime`（把超时结局规范化成结构化结果）、`capabilities.retry.provider`（按结局码决定是否重试） |
| `validation` 输出校验 | `capabilities.validation.definition`（`sanitize_output` / `validate_output` 的注入检测与 schema 校验） | `capabilities.validation.provider`（`ValidationPlugin`） | `miniharness.tools.runtime`（消费被改写后的权威结果） |
| `tracing` 追踪 | `capabilities.tracing.definition`（`Span` / `AgentTracer`） | `capabilities.tracing.provider`（`TraceConsumer`） | `capabilities.persistence.provider`（把 span 一并落盘） |
| `skills` 技能 | `capabilities.skills.definition`（`Skill` / `SkillManager` / `active_skills` + `skill/*` 事件词汇） | `capabilities.skills.provider`（`SkillRegistry`） | `capabilities.skills.consumer`（`SystemPromptPlugin`）、`app.assembly`（重放 `skill/*` 事件恢复技能状态） |
| `permission` 审批 | `miniharness.tools.runtime` 的 `tools/pre-execute` 决策词汇（`allow` / `ask` / `deny`） | `capabilities.permission.provider`（`PermissionPlugin`：拒绝名单 + 审批名单） | `miniharness.tools.runtime`（按决策决定是否执行工具体）、`app.assembly`（对 `run_command` 装 `ask`：无审批者默认拒绝） |
| `sandbox` 沙箱 | `miniharness.sandbox.contract`（`SandboxSeam.wrap(调用意图, 策略) -> 可执行的 argv + 完整性要求`；不可用即 `SandboxUnavailableError`） | `providers.sandbox`（`EnvSandbox`：环境收敛 + argv 解析；强制不了的要求拒绝服务；**不负责终止**） | `capabilities.shell.provider`（`run_command` 的调用点：沙箱缺失或拒绝即失败，不回退到无约束执行） |
| `shell` 命令执行 | `capabilities.shell.definition`（argv-only 的调用语义、结果形状 `exit_code` / `stdout` / `stderr`、`DEFAULT_OUTPUT_LIMIT` 与截断标记） | `capabilities.shell.provider`（`ShellTool`：沙箱 seam + 受管范围的第一个真实消费者，不设超时、不终止） | `app.assembly`（注册 `shell` 技能、装审批策略、装配两个进程边界 seam）；工具是 `app.tools` 同类技能的 `run_command` |
| `final_output` 终结 | `miniharness.loop` 的 `agent/post-tool` 收尾协议 | `capabilities.final_output.provider`（`FinalOutputPlugin`） | `miniharness.loop`（按收尾协议结束本轮） |

每条能力内部的职责说明见各自的 `capabilities/<能力>/__init__.py`。

## 本族的测试

包内测试与实现同层、独立文件：`capabilities/<能力>/definition/test_*.py` 与
`capabilities/<能力>/provider/test_*.py`。它们只装「实现 + 一个工具流水线」，
并逐字对照契约包的语义（例如
`capabilities/retry/provider/test_retry.py` 同时跑 `with_retry` 与 `RetryPlugin`；
`capabilities/persistence/definition/test_log_format.py` 逐字读回日志文件与迁移链）。
跨包集成（装配整个 Loop）集中在 [`tests/`](../tests/README.md)。
