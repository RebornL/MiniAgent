# miniharness 规格设计

> 状态：待实现。来源：三方 harness 调研（`MiniAgent-vs-Codex-Harness.md`、`MiniAgent-Harness-Design.md`）+ MiniAgent 现状重构。发布标签：`ready-for-agent`（待 tracker 配置后应用）。

## Problem Statement

MiniAgent 现在的核心循环（`run_agent_with_trace`）把「驱动」和「策略」焊在同一段代码里：重试、工具超时、上下文压缩、权限判断全部在循环体内 if-else 内联。由此产生三个问题：

1. **加一种策略就要改循环**——每新增审批、结果改写、脱敏这类能力，都要动循环代码，职责发散、回归风险高。
2. **会话是不可逆的可变数组**——压缩旧消息时直接替换，历史丢失无法重放、无法审计。
3. **能力不可替换**——LLM 是模块级单例，工具是硬编码字典，无法不改循环地换后端。

对照已调研的三个生产级 harness 的实际做法——Codex（进程/协议边界隔离策略）与 deepseek-harness（一切皆插件 + 事件瀑布线，策略靠事件订阅注入而「不改循环」）——问题本质是：**MiniAgent 缺一个 harness 骨架**。本规格要为它补上这个骨架的最小形态。

## Solution

引入一个**最简化 harness（miniharness）**，落地 5 个原语，把现有 MiniAgent 的核心循环迁移上去：

1. **循环驱动、事件治理**——循环只负责「驱动 + 派发事件」，重试/超时/压缩/权限全部是事件订阅者。
2. **会话 = append-only 事件日志**——模型历史是日志的派生投影，持久化/压缩/遥测共用同一真相源。
3. **能力 seam = Definition + Provider + Consumer**——能力是契约+实现+消费三分，可替换。
4. **依赖注入、按需激活**——插件声明依赖，服务就绪才激活，加载顺序由依赖决定。
5. **注册可逆**——每个注册返回 disposer，卸载逆序 unwind。

迁移完成后，加「审批」这类新策略 = 新增一个订阅 `tools/pre-execute` 的插件，循环零改动。

## User Stories

1. 作为一个 Agent 使用者，我想通过单一入口提交一行输入并得到一个回答，以便我不需要理解循环内部细节。
2. 作为一个工具作者，我想只声明工具的 name/description/parameters/execute 就能注册一个工具，以便我不需要知道循环如何派发调用。
3. 作为一个工具作者，我想我的工具执行失败时得到结构化的错误结果而非抛异常崩掉整轮，以便错误能被模型观察和后续纠正。
4. 作为一个策略作者，我想订阅 `tools/pre-execute` 事件来允许/拒绝一次工具调用，以便我能加审批而不用改循环。
5. 作为一个策略作者，我想订阅 `tools/execute` 事件来包裹超时或重试，以便我能加耐用性护栏而不用改工具本身。
6. 作为一个策略作者，我想订阅 `tools/post-execute` 事件来观察或改写工具结果，以便我能注入提醒或规范化输出。
7. 作为一个策略作者，我想订阅循环的事件来在每步之前做上下文压缩，以便上下文治理也成为一种可插拔策略。
8. 作为一个会话消费者，我想 `Session.append` 只增不改地记录事件，以便历史能够被重放和审计。
9. 作为一个会话消费者，我想用 `derive_messages` 投影出模型可见的历史，以便持久化、压缩、追踪共用同一个真相源。
10. 作为一个能力扩展者，我想通过 `provide('llm', 实现)` 替换模型后端，以便换 provider 而不动循环和工具。
11. 作为一个能力扩展者，我想用插件的 `inject=[...]` 声明依赖，以便加载顺序由依赖关系而非手写启动序列决定。
12. 作为一个运行时维护者，我想卸载一个插件时其所有注册被逆序撤销，以便热卸载不留悬空引用。
13. 作为一个普通用户，我想危险工具在未获批准时被拒绝且工具体不执行，以便系统保持安全。
14. 作为一个调试者，我想任意一轮 turn 都能从会话日志重建出模型所见内容，以便事后排查可靠。
15. 作为一个既有 MiniAgent 用户，我想现有的计算/文件读写/搜索能力在迁移后行为不变，以便迁移不破坏已工作的功能。
16. 作为一个扩展作者，我想在依赖服务缺失时插件保持挂起而非报错崩溃，以便组合顺序可以自由调整。

## Implementation Decisions

- **引入 Context 作为唯一注册表与事件总线**：服务按键提供/获取（`provide`/`get`），事件按名派发。这是所有扩展点汇聚的地方。
- **事件派发三语义**：`emit`（观察，无返回）、`waterfall`（洋葱模型，可 short-circuit，后继通过 `next` 委托）、`serial`（按序，返回假则停止）。这足以表达 hooks/权限/超时/重试，无需更复杂机制。
- **插件模型**：插件是一个带可选 `inject`（依赖列表）与 `apply(ctx)` 的对象；依赖未就绪时保持挂起，就绪后激活。
- **Session 为 append-only 事件日志**：`append(type, data)` 只增不改；`derive_messages()` 是 surface 投影。模型可见内容必须能从日志重建（「Model-visible means logged」）。
- **工具执行流水线固定顺序**（源自调研原型，作为决策契约内联）：

  ```
  tools/pre-execute (allow | deny | ask)   ← 权限/审批/沙箱
      → 单调 guard（不可反向放行）
      → tools/execute（around 包装）        ← 超时/重试/计量
      → tools/post-execute（接受/改写）      ← 结果加工/提醒注入
      → finalizeContent（最后的 content 不变量）
      → tools/result（不可变权威结果）
  ```

- **ToolDefinition 契约**（源自原型，作为类型形状内联）：`{ name, description, parameters, execute(args) -> value, timeoutMs?, finalizeContent? }`。工具返回 canonical 值，错误走结构化失败结果而非抛异常。
- **Loop 是普通可替换服务**：只做「取输入 → 派发 pre-step → 经 llm seam 调模型 → 经 tools 管线执行工具 → 落日志 → 派发 turn-stopping」，不含任何具体策略。
- **LLM 走能力 seam**：provider 抽象暴露 `complete(messages) -> {text, tool_calls?}`，循环只依赖契约，不认识具体 provider。
- **现有模块迁移而不重写行为**：`Compaction` 变为 `agent/pre-step` 的策略插件；`RetryFunc`/`CallFunc` 变为 `tools/execute` 的 around 插件；`Persistence`/`AgentTrace` 变为 `Session` 日志的消费者；`SkillManager` 的 Skill 注册改为返回 disposer 的可逆注册；`Structure` 的校验下沉到 `ToolDefinition.execute` 的返回校验。行为语义保持不变。
- **并存过渡**：miniharness 与现有 `MiniAgent.py` 并存，先在 harness 上跑通计算/文件读写两类工具，再逐步把压缩/重试/超时从「循环内联」迁移到「事件插件」，最后移除旧的循环路径。

## Testing Decisions

- **好测试只测外部可观察行为**：断言会话日志中的事件内容与顺序（工具是否执行、`tool/result` 是否入日志、最终答案是什么），不断言内部字段名或中间状态。一个测试若在某条策略换实现后仍通过、却在「该策略被移除」后失败，才算测对了意图。
- **主 seam（集成）：turn 边界**。通过一个 mock LLM provider 注入「返回 tool-call」的回复，驱动一次 `turn(input)`，断言三个可观察事实：工具体被执行、`tool/result` 事件按序入日志、最终答案正确。这是最高层集成 seam，覆盖循环+日志+工具管线，也是**唯一能证明「策略注入不改循环」的 seam**。危险工具被拒（user story 13）也在这一层验证：装配 `PermissionPlugin` 后 turn 返回拒绝且 `tool/result` 未入日志。
- **辅助 seam（契约）：工具管线**。对 `ToolRuntime` 单测：注册一个 `tools/pre-execute deny` 策略后，断言工具 execute body 不被调用且返回结构化拒绝。这是核心契约的最小单位测试，为 deny 路径提供快速、定位准确的反馈。
- **辅助 seam（纯函数）：会话投影**。对 `Session.derive_messages()` 直接断言：给定一段 `append` 序列，投影出的 `messages` 确定且不含非 model-visible 事件；压缩做 surface 替换后，原始日志仍能重建出同样的模型可见历史。这是「Model-visible means logged」的最小确定性验证。
- **不为这些写 seam**：进程边界（Out of Scope），以及事件总线原语（`waterfall`/`serial`/`emit`）与插件装载/disposer——它们是框架原语，测它们等于测框架而非 agent 行为。
- **Prior art**：MiniAgent 当前无自动化测试。参照调研中 dsh 的 `test-support` 套件（`session-snapshot` / `llm-replay` / `agent-loop-testkit`）作为 mock provider 与日志断言的先例；本规格落地时以「最小、行为导向」为准，不照搬其规模。

## Out of Scope

- 进程边界与沙箱（独立执行进程、平台级沙箱）——后续阶段。
- 真实多 provider 路由与凭据管理——本阶段仅 mock provider，不迁 `config.json` 的真实 DeepSeek 链路。
- 多 agent / subagent / 委派外部 harness（Codex/Claude Code/ACP）。
- UI / SDK / 协议解耦的远程 client。
- 权限的持久化策略（本阶段只做内存态的 allow/deny/ask，不落盘审计）。
- 性能优化（token 精确计量、prompt 缓存）。

## Further Notes

- 设计依据见同目录 `MiniAgent-Harness-Design.md` 第 5 节（~200 行骨架）与第 6 节（迁移对照表）；Codex 深挖见 `MiniAgent-vs-Codex-Harness.md`。
- 本规格的「不写文件路径」约束下，模块均以概念名（Context / Session / ToolRuntime / Loop / LLM seam / 各策略插件）指代；落地时映射到实际文件。
- 5 个原语是硬约束：若实现中把任何一个策略重新写回循环体，即视为偏离本规格。