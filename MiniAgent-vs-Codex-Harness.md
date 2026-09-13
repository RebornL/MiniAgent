# MiniAgent vs Codex Harness 对比

> 结论先行：**MiniAgent 是「单进程 Python 手搓 Agent」，Codex 是「多进程 Rust 单仓的工业级 Agent harness」。**
> 两者核心循环（LLM ⇄ 工具）本质相同，但 Codex 在**进程隔离、协议解耦、权限沙箱、扩展生态、多 Provider** 五条战线上的深度是 MiniAgent 完全不具备的。
>
> 本文记录于 2026-09，基于 `D:/Project/Python/MiniAgent`（实现 `queryPath - 普通人怎么去构建自己的Agent.md` 的落地代码）与 `D:/Project/Ai/codex`（OpenAI Codex CLI 源码）两处一手源码逐条对照。
> 引用约定：`codex-rs/<crate>/src/<file>::<symbol>` 为 Codex 源码符号；`MiniAgent/<file>.py::<symbol>` 为 MiniAgent 符号。

---

## 1. 规模与定位

| | MiniAgent | Codex Harness |
|---|---|---|
| 语言 | Python 3.13，单进程，同步 | Rust 单仓 ~150 crate + Node CLI 壳 + TS/Python SDK |
| 代码量 | ~9 个 `.py`，合计 ~60KB | 单 crate 如 `core-plugins/src/manager.rs` 即 146KB，总规模百万级 |
| 入口 | `python MiniAgent.py` → `chat_loop` 读 stdin | `codex` CLI → TUI / `codex exec` / app-server / SDK |
| 定位 | 学习/验证 Agent 原语（ReAct+压缩+持久化+Skill） | 生产级 coding agent harness，可被 CLI/IDE/云端/app 多重嵌入 |

对比基准的 MiniAgent 组件清单：`MiniAgent/MiniAgent.py`（主循环+工具+流式+并行执行）、`SkillManager.py`（动态加载工具）、`Compaction.py`（上下文压缩）、`Persistence.py`（JSON 会话持久化）、`AgentTrace.py`（Span 追踪）、`Structure.py`（结构化输出+注入检测）、`RetryFunc.py`（指数退避重试）、`CallFunc.py`（工具超时）。

---

## 2. Codex Harness 是怎么做的

### 2.1 核心引擎：双层循环

Codex 的 Agent 循环不是 MiniAgent 的一个 `while step < max_steps`，而是**会话 → 任务 → Turn → 采样请求**的嵌套结构：

- 一个「会话线程」由 `ThreadManager` 创建 `CodexThread`（`codex-rs/core/src/thread_manager.rs::ThreadManager`、`core/src/codex_thread.rs:180`）。
- 会话内 `Session` 跑 `submission_loop` 消费 SQ(Submission) 队列，把用户输入 `Op::TurnInput` 转换成 `RegularTask`（`core/src/tasks/regular.rs` 内是 `loop { run_turn(...) }`）。
- `run_turn` 内层再套 `run_sampling_request → try_run_sampling_request`：流式解析 `ResponseEvent` → 派发工具 → 把工具结果回填历史 → 再采样（`core/src/session/turn.rs`）。

即 **「外层循环管 turn，内层循环管一次采样」**，比 MiniAgent 的单一 `for step` 更细：一次模型调用如果还要求工具，内层就能再次采样而不必触发新一轮 turn 记账。

### 2.2 协议解耦：client 与 agent 是两个编译单元

Codex 把「和人交互的里层(CLI/TUI/app-server)」与「Agent 引擎」用协议队列彻底拆开：

- client 只通过 `protocol` 定义的 **SQ(Submission)/EQ(Event) 通道** 与 agent 通信，两者**编译器层面无直接依赖**（`codex-rs/protocol/src/protocol.rs`；语义见 `codex-rs/docs/protocol_v1.md`，实体为 Model/Codex/Session/Task/Turn）。
- 工具执行再被甩到第三方进程（下节），所以「谁在跑哪个进程」是运行时决定，不是编译期写死。

对比 MiniAgent：`client`、工具、循环、UI(print) 全在一个 `.py` 文件里，LLM client 是模块级单例 `client = OpenAI(...)`（`MiniAgent/MiniAgent.py::client`）。

### 2.3 执行架构：exec → app-server → exec-server 三层

Codex 里 **`exec/` 并不执行任何命令**，它是 `codex exec` 的非交互 CLI 前端：

- `exec/Cargo.toml` 只依赖 `codex-app-server-client`，**不依赖** `codex-exec-server`（`exec/Cargo.toml:19-47`）。
- 真正起子进程/做文件 IO 的是 **`exec-server`**——一个 JSON-RPC 执行守护进程，用 PTY 产子进程、`LocalFileSystem` 做文件操作、沙箱化操作再 fork 隐藏 helper 进程（`exec-server/src/local_process.rs::LocalProcess::start_process`、`exec-server/src/fs_sandbox.rs::FileSystemSandboxRunner`）。
- 三层边界：`exec`(CLI) → `app-server`(编排) → `exec-server`(执行)，物理隔离在独立进程/服务里。

对比 MiniAgent：工具就是「同进程内的 Python 函数」，`read_file`/`write_file` 直接 `open()`（`MiniAgent/MiniAgent.py::read_file`/`::write_file`），`calculate` 甚至直接 `eval()`（`MiniAgent/MiniAgent.py::calculate`）。

### 2.4 工具系统：feature-flag 注册 + MCP 命名空间

- 内置工具由 `build_tool_router` 按 feature 开关注册，handler 集中在 `core/src/tools/handlers/mod.rs`（`core/src/tools/spec_plan.rs::build_tool_router`）。
- 可观察到的工具名：`local_shell`、`apply_patch`、`web_search`、`Todo`、协作工具 `CollabTool::{SpawnAgent, SendInput, Wait}`、以及 MCP 工具 `mcp__server__tool`（三段前缀，`codex-rs/codex-mcp/src/mcp/mod.rs::qualified_mcp_tool_name_prefix`）。
- MCP 分两层：`codex-rmcp-client` 管 transport/OAuth/认证，`codex-mcp` 管连接管理/工具目录缓存（`codex-rs/codex-mcp/src/lib.rs`）。

### 2.5 权限与沙箱

- **审批**：`execpolicy` 用 **Starlark 前缀规则 DSL**，逐命令输出 `Allow / Prompt / Forbidden` 三态，`prompt` 即「请求用户批准」（`execpolicy/src/decision.rs::Decision`、`execpolicy/src/parser.rs::parse_policy`）。
- **沙箱**（跨平台四套，`sandboxing/src/manager.rs::SandboxType`）：Linux `bubblewrap`(默认)+seccomp+landlock、macOS `Seatbelt`、Windows `RestrictedToken/AppContainer/WFP`、Windows `MXC native`；Linux 默认就是沙箱（`linux-sandbox/README.md`）。
- **提权**：`shell-escalation` 给 zsh 打补丁拦截 `execve`，把命令从沙箱内提升到沙箱外（`shell-escalation/README.md`、`shell-escalation/src/unix/escalate_server.rs`）。
- **文件编辑**：`apply-patch` 把补丁解析成 hunk → 读原文 → **严格定位 old_lines**，任何一步对不上就**整体拒绝**，绝不猜测落盘（`apply-patch/src/file_update.rs::compute_replacements`）。
- **加固**：`process-hardening` 在 main 前关 core dump、禁 ptrace、清 `LD_*` 环境变量（`process-hardening/src/lib.rs:9-24`）。

对比 MiniAgent：**没有任何权限/沙箱概念**，唯一的安全措施是 `calculate` 里的字符白名单和 `Structure.py` 的注入检测，属于「信任 LLM 不乱来」的原型级。

### 2.6 上下文管理与压缩

- 真正的模型对话历史在 `ContextManager`（append / for_prompt / compaction，`core/src/context_manager/history.rs`）。
- 压缩是一个专门的 `CompactTask`：**远端 V2 与本地压缩二选一分派**（`core/src/tasks/compact.rs`）；本地压缩是「摘要 prompt + 重建压缩历史」（`core/src/compact.rs`）。
- `context-fragments` 提供**带标记的上下文片段注入**机制（`context-fragments/src/fragment.rs::ContextualUserFragment`），skills/memories/web-search 都通过它向模型上下文注入，而非硬拼字符串。
- system prompt 不在源码硬编码，运行时从 `models.json` 每条模型的 `model_messages.instructions_template` 读取（`models-manager/models.json`）；`core/` 下那些 `gpt_5_*_prompt.md` 只是人类可读参考，无代码引用。

对比 MiniAgent：压缩是「tiktoken 计数 + 超 2000 token 就把旧消息交给小模型摘要」（`MiniAgent/Compaction.py::ContextManager`，`CompactionConfig.max_tokens=2000`），system prompt 是 `SYSTEM_PROMPT` 字符串 + `build_system_prompt` 拼接 skill 文本（`MiniAgent/MiniAgent.py::build_system_prompt`）。

### 2.7 Skills / 扩展 / Hooks

- Skill 由 **`SKILL.md`(正文) + `agents/openai.yaml`(interface 元数据)** 定义，被扫描发现（深度 ≤6、并发 ≤8，`ext/skills/src/loader/mod.rs`）；以 `PromptFragment` 注入（developer 目录 + user 角色 skill 体，`ext/skills/src/fragments.rs`）。
- 目录采用**渐进式披露**，元数据预算 8000 字符（`ext/skills/src/render.rs`）；另有**检索式触发**动态选择器（RRF 融合等，`ext/skills/src/dynamic_skill_selector.rs`）。
- 扩展统一走 `extension-api` 的 contributor trait（Context/Tool/McpServer/ThreadLifecycle…，`ext/extension-api/notes.md`）。
- 插件包模型 `PluginManifest`（skills/mcp_servers/apps/hooks 路径，`plugin/src/manifest.rs`）+ `core-plugins` 管存储/市场/同步。
- `hooks` 提供 **12 类 ClaudeCode 风格事件 hook**（PreToolUse/PostToolUse/PreCompact/SessionStart/…，`hooks/src/lib.rs::HOOK_EVENT_NAMES`）。

对比 MiniAgent：Skill 是「Python 对象 `Skill{name,description,tools,tool_map,system_prompt}` + 内存字典 + `load_skill`/`unload_skill` 元工具」（`MiniAgent/SkillManager.py::Skill`/`::SkillManager`）。**没有** SKILL.md 文件约定、没有插件包、没有 hooks。

### 2.8 多 Agent / 长期记忆 / 可观测 / 多 Provider / SDK

- **子 Agent**：`AgentRunner` 调 `ThreadManager::spawn_subagent` fork 线程，父子拓扑持久化进 `agent-graph-store`（SQLite，`ext/agent/src/lib.rs::AgentRunner`、`agent-graph-store/src/local.rs`）。
- **协作模式**：`collaboration-mode-templates/plan.md` + `default.md` 两套预设 developer_instructions。
- **长期记忆**：memories 两阶段流水线——Phase1 从 rollout 抽取结构化记忆、Phase2 全局整合并维护 `~/.codex/memories/.git` 基线（`memories/README.md`）。
- **可观测**：生产走 `otel`（OTLP traces/logs/metrics + W3C 传播）；本地诊断走 `rollout-trace`（opt-in 事件 bundle + 离线 reducer 生成语义图，**不上传**）。
- **多 Provider**：`ModelProvider` trait + `ModelsManager`（bundled `models.json` 或远端 `/models`）；本地还支持 `ollama`/`lmstudio`（`--oss`）。
- **SDK**：TS SDK 包装 CLI（spawn + JSONL over stdio）；Python SDK 走 app-server JSON-RPC（`sdk/typescript/README.md`、`sdk/python/README.md`）。

---

## 3. 逐维度对比表

| 维度 | MiniAgent | Codex Harness | 差距 |
|---|---|---|---|
| 核心循环 | 单 `for step` ReAct（`MiniAgent.py::run_agent_with_trace`） | Session→Task→Turn→Sampling 双层循环（`core/src/session/turn.rs`） | Codex 更细粒度 |
| 进程模型 | 单进程，工具=同进程函数 | 多进程：client/agent/exec-server 物理隔离 | ⭐️ 根本性差异 |
| client↔agent 耦合 | 模块级单例 `client`，紧耦合 | SQ/EQ 协议队列，编译期解耦 | ⭐️ |
| 工具注册 | 手写 JSON Schema 列表 `TOOLS`/`TOOL_MAP` | feature-flag 注册 + MCP 命名空间 | Codex 生态化 |
| 工具执行 | 直接 `eval()`/`open()` | exec-server 沙箱 + execpolicy 审批 | ⭐️ 安全 |
| 权限/审批 | 无 | Starlark 规则 Allow/Prompt/Forbidden | ⭐️ |
| 沙箱 | 无 | 4 平台沙箱，Linux 默认 bwrap | ⭐️ |
| 上下文管理 | list + tiktoken + 摘要 | ContextManager + CompactTask(远端/本地) + fragments | Codex 更结构化 |
| 压缩触发 | token>2000 摘要旧消息 | 专用 Task，远端 V2/本地分派 | 量级不同 |
| 会话持久化 | JSON 文件 + 原子写（`Persistence.py::Store`） | thread-store + state 多 store migrations | Codex 多存储后端 |
| Skill | Python 对象 + 内存字典 | SKILL.md 文件 + fragment 注入 + 检索式选择 | Codex 文件化/可分发 |
| 结构化输出 | `final_output` 工具 + jsonschema (`Structure.py`) | 工具调用本身结构化 + code-mode 协议 | 思路相近 |
| 重试/超时 | `RetryFunc` + `CallFunc`（手写） | 内建于 async runtime/clients | 相近 |
| 流式 | `stream_llm_call` 逐 chunk 打印 | TUI 差分渲染 + streaming 模块 | Codex 更完整 |
| 多 Agent | 无 | spawn_subagent + graph-store + collab 模板 | ⭐️ |
| 长期记忆 | 无（压缩摘要算近似的记忆） | memories Phase1/2 流水线 + git 基线 | ⭐️ |
| 可观测 | `AgentTrace` 手写 Span | otel(OTLP) + rollout-trace 语义图 | ⭐️ |
| 多 Provider | 单 OpenAI 兼容端点（config.json） | ModelProvider trait + ollama/lmstudio | Codex 抽象完备 |
| Plugins/Hooks | 无 | plugin Manifest + 12 类 hooks | ⭐️ |
| UI | `print`/`input`（`chat_loop`） | 全功能 TUI + app-server + IDE | ⭐️ |
| SDK | 无 | TS + Python 双 SDK | ⭐️ |

---

## 4. 结论与借鉴清单

### 根本性差异（按重要性）

1. **进程隔离**：Codex 把「会执行命令的代码」扔进 `exec-server` 独立进程，MiniAgent 把 `eval()` 和 `open()` 写进主循环。这是「玩具」和「工具」的分水岭。
2. **权限模型**：Codex 有 Starlark 规则 + 四平台沙箱 + 审批流；MiniAgent 完全没有，只靠字符白名单。
3. **协议解耦**：Codex 的 client 和 agent 是编译期独立的，才能长出 TUI/IDE/app-server/SDK 多个前端；MiniAgent 全耦合在一个文件里。
4. **扩展生态**：Codex 的 SKILL.md 文件约定 + plugin 包 + hooks，让能力可分发、可共享；MiniAgent 的 skill 是写死在代码里的 Python 对象。

### 可以落到 MiniAgent 的廉价手法（性价比排序）

1. **把工具执行与主循环拆分**：参照 codex 的 exec-server 思路，MiniAgent 至少可以把 `eval`/文件读写放到独立线程（当前 `CallFunc.py` 已用 `ThreadPoolExecutor` 做了超时隔离，方向正确，但边界不彻底）。
2. **把 system prompt 数据化**：从 `config.json`/单独文件读，而非硬编码（对照 codex 的 `models.json::instructions_template`）。
3. **工具注册表数据化**：工具定义用 JSON Schema 文件而非代码内联，MCP 的三段命名 `mcp__server__tool` 是低成本可借鉴约定。
4. **文件编辑走校验收敛**：`apply-patch` 的「严格定位 + 失败整体拒绝」可直接启发 MiniAgent 的 `write_file` 增加校验。

### 不建议 MiniAgent 照搬的

- Rust 单仓 + Bazel 构建、OTLP 全链路遥测、跨平台内核沙箱、SQLite 拓扑图存储——这些是「工业 harness」的应有之义，但与本项目「理解 Agent 原语」的定位不符，照搬即过度设计。

---

## 附：源码引用速查

- Codex 核心循环：`codex-rs/core/src/{thread_manager.rs,codex_thread.rs,tasks/regular.rs,session/turn.rs}`
- Codex 协议：`codex-rs/protocol/src/protocol.rs`、`codex-rs/docs/protocol_v1.md`
- Codex 执行/沙箱：`codex-rs/{exec,exec-server,exec-server-protocol,execpolicy,sandboxing,linux-sandbox,windows-sandbox-rs,apply-patch,shell-escalation}/`
- Codex 能力扩展：`codex-rs/{skills,ext/skills,codex-mcp,codex-rmcp-client,ext/agent,agent-graph-store,memories,model-provider,models-manager,otel,rollout-trace,plugin,core-plugins,hooks,tui}/`
- Codex SDK：`sdk/typescript/`、`sdk/python/`
- MiniAgent 全量：`MiniAgent/MiniAgent.py` 及 `SkillManager.py`/`Compaction.py`/`Persistence.py`/`AgentTrace.py`/`Structure.py`/`RetryFunc.py`/`CallFunc.py`