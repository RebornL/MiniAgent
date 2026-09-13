# miniharness —— 骨架族（低频的契约与运行时）

骨架只装「不随策略变动」的东西：事件与插件原语、会话日志、能力契约、以及零策略的循环。
策略与后端都在别的族：`capabilities/`（策略）、`providers/`（seam 后端）、`app/`（装配）。

本文件是该族的**权威包地图**。落位、命名、依赖方向与测试放置的规范见
[`docs/packaging.md`](../docs/packaging.md)。

## 本族的包

| 包 | 角色 | 职责 | 依赖 |
| --- | --- | --- | --- |
| `miniharness.core` | 事件与插件原语 | `Context`（服务注册表 + 事件总线 `emit`/`waterfall`/`serial` + 可逆效应栈）、`Plugin`（`inject` 声明依赖，按需激活） | 无 |
| `miniharness.session` | 会话事件的契约 | `Session`：append-only 事件日志、`derive_messages()` 投影、`replay()` 用落盘日志重放恢复、`session_from_messages()` 把 v0 的消息数组翻译成事件、`compact()` surface 替换 | 无 |
| `miniharness.tools` | 中间包：工具能力角色包的**归属层** | 说明这层能力：契约（`contract`）与运行时（`runtime`）为什么分居两个包；不放实现 | 无 |
| `miniharness.tools.contract` | 工具能力的 **Definition（契约）** | `ToolDefinition`：工具作者只声明字段，不碰执行；`AbortOutcome` 与四个中止结局码（`timed_out` / `cancelled` / `denied` / `failed`） | 无 |
| `miniharness.tools.runtime` | 工具能力的 **Provider（运行时）** | `ToolRuntime`：注册表 + 固定顺序流水线（pre-execute → guard → execute → post-execute → finalize → result）；把成功与中止规范化成同构的结构化结果 | `core`、`tools.contract` |
| `miniharness.llm` | 中间包：LLM 能力角色包的**归属层** | 说明这层能力：契约在骨架、实现在 `providers/`；不放实现 | 无 |
| `miniharness.llm.contract` | LLM 能力的 **Definition（契约）** | `LLM.complete(messages) -> {text, tool_calls?}` | `core` |
| `miniharness.process` | 中间包：受管范围 seam 角色包的**归属层** | 说明这层 seam：契约在骨架、平台后端在 `providers.process`；不放实现 | 无 |
| `miniharness.process.contract` | 受管范围 seam 的 **Definition（契约）** | `ManagedRange`（`poll` / `wait_for_exit` / `terminate` / `release`）与 `ProcessSeam.spawn`：以整棵进程树为单位，终止幂等 | `core` |
| `miniharness.sandbox` | 中间包：沙箱 seam 角色包的**归属层** | 说明这层 seam：契约在骨架、后端在 `providers.sandbox`；不放实现 | 无 |
| `miniharness.sandbox.contract` | 沙箱 seam 的 **Definition（契约）** | `SandboxSeam.wrap(调用意图, 策略) -> 可执行的 argv + 完整性要求`；不可用即 `SandboxUnavailableError`（fail-closed）；**没有任何终止动词** | `core` |
| `miniharness.loop` | 三个契约的 **Consumer（消费方）** | `Loop`：取输入 → `agent/pre-step` → llm seam → tools 管线 → 落日志；在每步开始前 / 模型请求前 / 顶层工具派发前派发 `agent/checkpoint`（`CHECKPOINT_*`，订阅者抛错即 fail-closed）；**零策略** | `core`、`session`、`tools.runtime`、`llm.contract` |

**为什么 `tools.contract` 与 `tools.runtime` 是两个包**：契约低频、流水线可变。按变化速率拆包，
频繁的运行时改动不会牵动工具作者依赖的那个接口。LLM 同理：契约在本族，实现在 `providers/`。

**为什么受管范围另立一个包**：它是工具体脚下的进程 seam，与工具流水线（`tools.runtime`）的
变化节奏不同；终止机制只随平台变（POSIX 信号组 / Windows Job Object），不随策略变。
「何时终止」是策略，归 `capabilities/`（超时 / 取消的接线），本 seam 只认识 argv。
契约留在本族、平台后端放 `providers/`（§3：骨架 seam 的后端在 `providers/`）：消费方一律
经 `ctx.get("process")` 拿契约，装配由 `app/` 负责，所以依赖方向仍然是
`capabilities → miniharness`。

**为什么沙箱与受管范围是两个 seam、不能合成一个**：它们回答两个不同的问题——沙箱回答
「拿什么 argv、在什么环境下跑」（隔离），受管范围回答「这棵进程树怎么等、怎么终止」（生命周期）。
变化速率也不同：隔离手段随平台与策略变，终止机制只随平台变。所以沙箱契约里**没有**任何
终止动词，受管范围契约里**没有**任何隔离策略；`run_command` 依次经过两者，各自只做自己的事。

## 本族的测试（与实现同层、独立文件）

| 文件 | seam | 覆盖 |
| --- | --- | --- |
| `miniharness/session/test_projection.py` | S5（纯函数） | 投影确定幂等、只含 model-visible 事件、压缩是 surface 替换且原日志可重放、逆投影往返等价、订阅者观察日志不改投影 |
| `miniharness/tools/runtime/test_pipeline.py` | S3（工具管线契约） | 四种中止结局各有稳定码且与成功同构、同走 `tools/result`；deny 不执行工具体；单调 guard 不可反向放行；ask 无审批者默认拒绝；并提供 `_pipeline()` 供能力测试复用 |

跨包集成测试集中在 [`tests/`](../tests/README.md)——那里是装配多个包之后才成立的断言。

## 核心不变量

**Model-visible means logged**：凡进入模型的内容，都必须能从会话日志重建。
