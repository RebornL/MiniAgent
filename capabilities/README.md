# capabilities —— 能力族（策略：契约 / 实现 / 消费方三分）

这里是骨架之上的**策略**。每个能力一个目录，内部按角色与变化速率拆包：

- **Definition（契约）**：`<能力>/definition/` —— 稳定、低频的类型与语义；
- **Provider（实现）**：`<能力>/provider/` —— 具体策略，变动频繁；
- **Consumer（消费方）**：只经契约消费该能力的包。

契约与实现不同包，是为了让高频变动的策略不牵连稳定接口。若某条能力的契约**就是骨架上的
事件 seam**（例如权限决策、终结收尾协议），契约留在 `miniharness/`，本族只放实现；若某条
能力**没有稳定契约符号**（如 `validation` / `tracing` / `timeout`：无事件词汇、无形状、
无投影，消费方要么缺位要么鸭子类型），则不设 `definition/`，语义写进能力的 `__init__.py`
——同样不为了凑角色而建空壳包。

T1 曾把这条规则只做到一半（legacy 语义模块整体搬进 `definition/`，类型与实现没分家）。
该偏差已于 T12（issue #14）全部消化：`persistence` / `compaction` / `skills` / `retry`
拆成契约 + 实现两包，`validation` / `tracing` / `timeout` 整体并入 provider 撤销空壳；
消化路径与行数见 [`docs/packaging.md`](../docs/packaging.md) §9。

本文件是该族的**权威包地图**。落位、命名、依赖方向与测试放置的规范见
[`docs/packaging.md`](../docs/packaging.md)。

## 包地图：每个能力的三个角色落在哪个包

| 能力 | Definition（契约包） | Provider（实现包） | Consumer（消费方包） |
| --- | --- | --- | --- |
| `compaction` 压缩 | `capabilities.compaction.definition`（压缩状态的恢复语义：`compaction_summaries`——从事件日志投影摘要链，末项是当前增量摘要、条数是累计压缩次数） | `capabilities.compaction.provider`（`CompactionConfig` / `ContextManager`：token 计数与压缩状态；`CompactionPlugin` + `stub_summarizer`：订阅 `agent/pre-step` 做 surface 替换） | `miniharness.session`（把压缩事件投影成 surface 替换）；`app.assembly` 派发 `session/replayed`，`CompactionPlugin` 订阅折叠摘要链 |
| `persistence` 持久化 | `capabilities.persistence.definition`（事件日志的格式版本词汇：`LOG_FORMAT` / `LOG_VERSION` / `LEGACY_LOG_VERSION` / `log_filename`） | `capabilities.persistence.provider`（`Store` / `PersistenceManager`：目录布局、`events.v<N>.jsonl` 追加写与迁移链；`PersistenceConsumer`：有界写后缓冲 + `flush()` 屏障，订阅 `agent/checkpoint` 在三个语义点 fail-closed 落盘） | `app.assembly`、`app.cli`（重放日志恢复会话、列出历史）；`miniharness.loop` 只**派发 `agent/checkpoint` seam 事件**（每步开始前 / 模型请求前 / 工具派发前），不 import 本能力的任何契约 |
| `retry` 重试 | `capabilities.retry.definition`（`RETRYABLE_OUTCOMES` / `is_retryable_outcome`） | `capabilities.retry.provider`（`with_retry` / `is_retryable`：退避循环与瞬时故障判定；`RetryPlugin`，订阅 `tools/execute` 的 around） | `miniharness.tools.runtime`（消费被包装后的调用结果） |
| `timeout` 超时 / 取消 | —（无独立契约包：结局码词汇在骨架 `miniharness.tools.contract`，超时/取消语义见 `capabilities/timeout/__init__.py`） | `capabilities.timeout.provider`（`ToolTimeoutPlugin`：**本次工具调用期间把 `process` 服务包成登记册**，超时 / 取消即终止本次起的受管范围、确认静止后返回 `timed_out` / `cancelled`；`cancel()` 以 `ctx.get("abort")` 暴露，同时登记**本轮取消**；`TurnCancelPlugin`：消费本轮取消——`tools/guard` 拒绝新工具调用（在审批之前）、`agent/post-tool` 以取消收尾、`agent/turn-stopping` 把取消后的 Loop 自主收尾（纯文本答复 / 工具全被拒 / 步数用尽）改判为取消。取消源（信号装配）仍由装配层接：`app.cli.InterruptSource` 把回合执行期间的 Ctrl-C 换成一次 `cancel()`） | `miniharness.tools.runtime`（把中止结局规范化成结构化结果）、`capabilities.retry.provider`（按结局码决定是否重试）、`app.cli.InterruptSource`（信号装配）、`miniharness.process.contract`（终止动词） |
| `validation` 输出校验 | —（无独立契约包：本能力无稳定契约符号，三层防线语义见 `capabilities/validation/__init__.py`） | `capabilities.validation.provider`（三层防线：`validate_schema` / `validate_output` / `sanitize_*` + `ValidationPlugin`） | `miniharness.tools.runtime`（消费被改写后的权威结果）、`app.tools`（直接用 `sanitize_output` / `sanitize_string`）、`app.assembly`（装配 `ValidationPlugin`） |
| `tracing` 追踪 | —（无独立契约包：本能力无跨包 import 的契约符号，span 语义见 `capabilities/tracing/__init__.py`） | `capabilities.tracing.provider`（`AgentTracer`（span 累积与摘要）+ `TraceConsumer`（订阅 Session 日志）） | `capabilities.persistence.provider`（把 span 一并落盘） |
| `skills` 技能 | `capabilities.skills.definition`（技能装载的恢复语义：`skill/*` 事件词汇、`active_skills` 投影——从事件日志折叠出已激活技能名；`Skill` 形状） | `capabilities.skills.provider`（`SkillManager`：技能目录的注册 / 装载 / 卸载与激活视图；`SkillRegistry`：把技能工具经 ToolRuntime 可逆注册并记事件） | `capabilities.skills.consumer`（`SystemPromptPlugin`）；`app.assembly` 派发 `session/replayed`，`SkillRegistry` 订阅折叠 `skill/*` 事件 |
| `permission` 审批 | `miniharness.tools.runtime` 的 `tools/pre-execute` 决策词汇（`allow` / `ask` / `deny`） | `capabilities.permission.provider`（`PermissionPlugin`：拒绝名单 + 审批名单） | `miniharness.tools.runtime`（按决策决定是否执行工具体）、`app.assembly`（对 `run_command` 装 `ask`：无审批者默认拒绝） |
| `sandbox` 沙箱 | `miniharness.sandbox.contract`（`SandboxSeam.wrap(调用意图, 策略) -> 可执行的 argv + 完整性要求`；不可用即 `SandboxUnavailableError`） | `providers.sandbox`（`EnvSandbox`：环境收敛 + argv 解析；强制不了的要求拒绝服务；**不负责终止**） | `capabilities.shell.provider`（`run_command` 的调用点：沙箱缺失或拒绝即失败，不回退到无约束执行） |
| `shell` 命令执行 | `capabilities.shell.definition`（argv-only 的调用语义、结果形状 `exit_code` / `stdout` / `stderr`、`DEFAULT_OUTPUT_LIMIT` 与截断标记） | `capabilities.shell.provider`（`ShellTool`：沙箱 seam + 受管范围的第一个真实消费者，不设超时、不终止） | `app.assembly`（注册 `shell` 技能、装审批策略、装配两个进程边界 seam）；工具是 `app.tools` 同类技能的 `run_command` |
| `final_output` 终结 | `miniharness.loop` 的 `agent/post-tool` 收尾协议 | `capabilities.final_output.provider`（`FinalOutputPlugin`） | `miniharness.loop`（按收尾协议结束本轮） |

每条能力内部的职责说明见各自的 `capabilities/<能力>/__init__.py`。

## 本族的测试

包内测试与实现同层、独立文件：`capabilities/<能力>/provider/test_*.py`。它们只装
「实现 + 一个工具流水线」，并逐字对照契约语义（例如
`capabilities/retry/provider/test_retry.py` 同时跑 `with_retry` 与 `RetryPlugin`；
`capabilities/persistence/provider/test_log_format.py` 逐字读回日志文件与迁移链）。
跨包集成（装配整个 Loop）集中在 [`tests/`](../tests/README.md)。
