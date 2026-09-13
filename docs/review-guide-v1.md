# review-guide-v1 —— miniharness 迁移总结与审视指南

> 对应提交 `813d3bb`。本文用途：① 说清这轮改了哪些东西；② 说清迁移是怎么做的；③ 给出一条能独立复核整个工程的路径（读什么、跑什么、怀疑什么）。
> **注意：本文记的是 v1 平铺布局（`miniharness.py` / `miniharness_plugins.py` / `MiniAgent.py` / `test_miniharness*.py`）。v2-T1 已按能力族包化，模块路径与入口命令见 [`docs/packaging.md`](packaging.md) 与各族 `README.md`；代码本体未变。**
> 相关文档：[`docs/miniharness.md`](miniharness.md)（架构）、[`miniharness-spec.md`](../miniharness-spec.md)（规格）、[`MiniAgent-Harness-Design.md`](../MiniAgent-Harness-Design.md)（三方对比与设计）、[`MiniAgent-vs-Codex-Harness.md`](../MiniAgent-vs-Codex-Harness.md)（Codex 深挖）。

## 1. 做了什么

5 个提交，相对迁移前（`480fbee`）共 **18 个文件、+3465 / −727 行**。

| 提交 | 内容 |
| --- | --- |
| `757ffad` | agent skills 配置（issue tracker、triage 标签、domain 文档布局） |
| `1139e85` | miniharness 骨架：5 原语 + 迁移插件 + 三个 seam 的测试 |
| `127df22` | 移除旧循环：真实 provider、入口切到 harness、文档与 README |
| `24dfac5` | 超时鲁棒性：工具体改跑 daemon 线程 |
| `813d3bb` | 文档：记录超时边界 |

| 类别 | 文件 | 说明 |
| --- | --- | --- |
| **骨架（新）** | `miniharness.py`（679 行） | 5 原语 + `ToolRuntime` + 零策略 `Loop` + `MockLLM` + `PermissionPlugin` |
| **迁移层（新）** | `miniharness_plugins.py`（573 行） | 9 个策略插件 / 日志消费者 |
| **真实后端（新）** | `miniharness_deepseek.py`（191 行） | `DeepSeekProvider`，只实现 `complete(messages)` |
| **测试（新）** | `test_miniharness.py`(343) / `test_miniharness_plugins.py`(422) / `test_miniharness_agent.py`(125) | 26 条，全落在三个 seam |
| **入口（重写）** | `MiniAgent.py`（**922 → 453 行**） | 删内联循环，改为 `build_harness()` + `chat_loop()` |
| **解耦（改）** | `AgentTrace.py` | `log_llm_call` 改收纯数据，不再吃 SDK 响应对象 |
| **既有模块（零改动）** | `CallFunc` `RetryFunc` `SkillManager` `Persistence` `Compaction` `Structure` | 逐字未动 |
| **文档 / 工程** | `docs/miniharness.md`、`README.md`、`requirements.txt`、`AGENTS.md`、`docs/agents/*` | |
| **调研 / 规格** | `MiniAgent-vs-Codex-Harness.md`(158)、`MiniAgent-Harness-Design.md`(336)、`miniharness-spec.md` | 三方对比 → 原语提炼 → 规格 |

## 2. 怎么迁移的

### 迁移前：策略焊在循环里

`run_agent_with_trace` 的循环体内联了全部策略：

```python
content, tool_calls = with_retry(_call)             # 重试
future = executor.submit(call_with_timeout, ...)    # 超时
messages = ctx.maybe_compact(messages, client)      # 压缩
if name in OUTPUT_TOOL_NAMES: return result         # 终结
```

### 迁移后：循环只驱动与派发，策略全是订阅者

```
Loop.turn
  ├─ agent/pre-step ──▶ CompactionPlugin / SystemPromptPlugin
  ├─ llm.complete(messages)              ← 能力 seam，只认契约，不认识具体 provider
  └─ 每个 tool_call: ToolRuntime.run
       tools/pre-execute ──▶ PermissionPlugin (allow | deny | ask)
       tools/guard        ──▶ 单调收紧，不可反向放行
       tools/execute      ──▶ RetryPlugin / ToolTimeoutPlugin（around 包装）
       tools/post-execute ──▶ ValidationPlugin（接受 / 改写）
       finalizeContent → tools/result      ← 不可变权威结果
       agent/post-tool    ──▶ FinalOutputPlugin（Loop 不认识 final_output 这个名字）
```

### 迁移对照

| legacy（写在循环里） | 现在（挂在 seam 上） |
| --- | --- |
| `with_retry(...)` | `RetryPlugin` → `tools/execute` |
| `call_with_timeout(...)` | `ToolTimeoutPlugin` → `tools/execute` |
| `ctx.maybe_compact(...)` | `CompactionPlugin` → `agent/pre-step`（surface 替换） |
| `Structure` 校验 / sanitize | `ValidationPlugin` → `tools/post-execute` |
| `build_system_prompt` 每步覆写 | `SystemPromptPlugin` → `tools/result` / `agent/pre-step` |
| `OUTPUT_TOOL_NAMES` 硬编码终结 | `FinalOutputPlugin` → `agent/post-tool` |
| `pm.save_messages(...)` | `PersistenceConsumer` → Session 日志订阅 |
| `tracer.*(...)` | `TraceConsumer` → Session 日志订阅 |
| `skills.load` + 工具注册 | `SkillRegistry` → `ToolRuntime` 可逆注册 |

### 核心不变量

- **Model-visible means logged**：凡进模型的内容都必须能从 `Session` 日志重建。`Session` 是 append-only 事件日志，`derive_messages()` 是它的投影，`session_from_messages()` 是逆投影。
- **Loop 零策略**：`Loop.turn` 里没有具体工具名、没有重试/超时/压缩/权限分支。这可以用 `grep` 自证（见 §3）。
- **注册皆可逆**：`Context.effect` 返回 disposer，插件卸载逆序 unwind。

## 3. 怎么审视

### 建议阅读顺序

| 顺序 | 看什么 | 目的 |
| --- | --- | --- |
| 1 | `docs/miniharness.md` | 架构总览：5 原语、流水线、迁移对照、已知边界（约 15 分钟） |
| 2 | `miniharness.py` 的 5 个类：`Context` / `Session` / `ToolRuntime` / `Loop` / `LLM` | 骨架本体。**重点是 `Loop.turn` 有多空** |
| 3 | `miniharness_plugins.py` | 策略怎么挂上去：每个类只有 `apply`（订阅）+ 一个 handler |
| 4 | `MiniAgent.py` 的 `build_harness()` + `chat_loop()` | 应用侧装配；对照 git 历史看 922 行循环塌缩成 453 行 |
| 5 | 三个测试文件 | 契约。每条测试都对应一个真实会被违反的行为 |
| 6 | `miniharness-spec.md` → `MiniAgent-Harness-Design.md` → `MiniAgent-vs-Codex-Harness.md` | 为什么长这样：三方对比 → 原语提炼 → 规格 |

### 命令

```bash
python -m pytest -q                    # 26 passed
python miniharness.py                  # 骨架 smoke：依赖驱动激活顺序 + deny 分支
python miniharness_plugins.py          # 全栈 smoke：压缩 / trace / 持久化 / 技能
python MiniAgent.py                    # 真实入口（需根目录 config.json）

# 自证「Loop 零策略」：Loop 类体内不该出现任何工具名（只应命中 _demo）
grep -n "final_output\|search_web\|calculate\|read_file\|write_file" miniharness.py

# 自证「旧循环已移除」
grep -n "run_agent_with_trace\|stream_llm_call\|SYSTEM_PROMPT\|TOOL_MAP" *.py
```

### 逐项核对清单

- [ ] `python -m pytest -q` → 26 passed。
- [ ] `Loop.turn` 内无工具名、无重试/超时/压缩/权限分支。
- [ ] `run_agent` / `run_agent_with_trace` / `stream_llm_call` 已无任何引用。
- [ ] 6 个既有模块（`CallFunc`/`RetryFunc`/`SkillManager`/`Persistence`/`Compaction`/`Structure`）无改动；`AgentTrace.py` 的改动只是 `log_llm_call` 改签名。
- [ ] 删掉根目录 `config.json` 后，`python -m pytest -q` 仍全绿（导入期不读配置）。
- [ ] 用 mock provider 装配一次 turn 并落日志（`test_miniharness_agent.py` 覆盖）。
- [ ] `docs/miniharness.md` §4 的「有意偏离」「超时边界」与代码一致。

### 值得重点怀疑的 7 处（有意取舍，不是遗漏）

1. **`Loop.turn` 整批跑完才收尾** —— 终结工具不中断同批其它 `tool_calls`。原因：中途 `return` 会留下「`tool_calls` 未逐条配对」的断链，下一轮上行必被兼容接口 400。有两条测试守着（终结路径 + deny 路径）。
2. **投影只保留最新一条 `system/message`，且恒置最前** —— system prompt 视为「状态」而非「历史」。原因：中位 system 消息会被部分 OpenAI 兼容接口拒绝；legacy 也恒为「开头单条 system」。
3. **`SystemPromptPlugin` 订阅 `tools/result`** —— 为了让技能装载后在**同一轮内**、下次采样前刷新提示（legacy 是每步重算 `build_system_prompt`）。
4. **超时 = 停止等待，不是取消** —— daemon 线程；被放弃的工具体仍跑到底、副作用不回滚。真隔离需要进程级边界或沙箱（Out of Scope）。
5. **超时报 `error` 而非 legacy 的字符串 `ok`** —— 这是唯一一处**有意违背** legacy 行为的地方。
6. **工具懒注册** —— 初始只有 `load_skill` / `unload_skill`，其余随技能装载才可见（与 legacy `get_active_tools()` 同语义）。因此 `unload_skill` 真的会撤销工具。
7. **`build_harness()` 不装 `PermissionPlugin`** —— legacy 没有审批概念，装了就是凭空加行为。审批能力本身在 `miniharness.py` 里，需要时自行装配（见 `docs/miniharness.md` §5）。

### 一个已知的设计张力（下一步最值得做的改进）

持久化落的是**投影**（`derive_messages()` + `summary` / `active_skills` / `runs` 旁路），不是**事件日志**。因此日志里那些非 model-visible 的事件（如技能装载/卸载、`context/compacted`）不落盘，恢复会话是「messages + meta」拼回来的，而不是重放日志。这与「`Session` 是唯一真相源」的宣言存在张力——更彻底的形态是持久化事件、恢复即重放日志。本轮没做，因为它需要改动 `Persistence` 的公开 API（在非目标清单里）。

## 4. 验收证据

| 项 | 证据 |
| --- | --- |
| 单元/集成测试 | `python -m pytest -q` → **26 passed** |
| 真实入口离线跑通 | 假 client 驱动 `chat_loop`：4 次采样、`/history`、`/new` 正常；落盘 `[system, user, assistant, tool, assistant, tool, assistant]`、`active_skills=['calculator']`、traces 非空 |
| 全新 clone 可运行 | `test_import_works_on_a_fresh_clone_without_config_json`：无 `config.json` 也能 `import MiniAgent` |
| 超时不拖垮进程 | `test_timeout_does_not_block_process_exit`：修复前子进程挂满超时，修复后秒回 |
| 行为不变性 | `test_retry_plugin_matches_with_retry_semantics`、`test_compaction_plugin_matches_legacy_threshold_split_and_summary`、`test_validation_plugin_reuses_structure_semantics` 均与 legacy 模块**并排跑**比对 |
