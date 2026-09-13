# miniharness —— MiniAgent 的 harness 骨架

`miniharness` 把 Agent 的「循环」与「策略」分开：**循环只负责驱动和派发事件**；重试、超时、压缩、输出校验、终结、持久化、追踪、system prompt 同步，全部是挂在事件 seam 上的插件。

一句话：**改策略，不改循环。**

对照调研见 [`MiniAgent-vs-Codex-Harness.md`](../MiniAgent-vs-Codex-Harness.md)（Codex）与 [`MiniAgent-Harness-Design.md`](../MiniAgent-Harness-Design.md)（三方对比 + 原语提炼）；规格见 [`miniharness-spec.md`](../miniharness-spec.md)。

## 1. 五个原语

| 原语 | 落点 | 说明 |
| --- | --- | --- |
| 循环驱动、事件治理 | `miniharness.py::Context` `Loop` | 事件三种派发语义：`emit`（观察）/ `waterfall`（洋葱，可 short-circuit）/ `serial`（按序，返回假则停） |
| 会话 = append-only 事件日志 | `miniharness.py::Session` | `append` 只增不改；`derive_messages()` 是投影；`session_from_messages()` 是其逆 |
| 能力 seam | `miniharness.py::LLM`、`ToolRuntime` | 契约 + Provider + 消费方三分，换后端不动循环 |
| 依赖注入、按需激活 | `Plugin.inject`、`Context.load` | 依赖未就绪则挂起，就绪后自动激活 |
| 注册可逆 | `Context.effect`、`Context.unload` | 每个注册返回 disposer，卸载逆序 unwind |

**核心不变量：Model-visible means logged** —— 凡进入模型的内容，都必须能从会话日志重建。

## 2. 工具执行流水线

```
tools/pre-execute (allow | deny | ask)   权限 / 审批
  → tools/guard（单调：只能收紧）          不可反向放行
  → tools/execute（around 包装）           超时 / 重试 / 计量
  → tools/post-execute（接受 / 改写）      输出校验 / 提醒注入
  → finalizeContent                       收敛出恒为 str 的 content
  → tools/result                          不可变权威结果（仅 ok / error）
```

`deny` 不产生权威结果：落 `tool/denied`、不发 `tools/result`、工具体不执行。

## 3. 文件地图

| 文件 | 职责 |
| --- | --- |
| `miniharness.py` | 5 原语 + `ToolRuntime` + 零策略 `Loop` + `MockLLM` + `PermissionPlugin` |
| `miniharness_plugins.py` | 迁移层：把 legacy 的内联策略变成事件订阅者 |
| `miniharness_deepseek.py` | 真实 provider（DeepSeek / OpenAI 兼容），只实现 `complete(messages)` |
| `MiniAgent.py` | 应用装配：工具、技能、`build_harness()`、`chat_loop()` |
| `test_miniharness*.py` | 三个 seam 上的测试 |

## 4. 迁移对照

| legacy（写在循环里） | 现在（挂在 seam 上） |
| --- | --- |
| `ctx.maybe_compact(...)` | `CompactionPlugin` → `agent/pre-step`（surface 替换） |
| `with_retry(...)` | `RetryPlugin` → `tools/execute`（around） |
| `call_with_timeout(...)` | `ToolTimeoutPlugin` → `tools/execute`（around） |
| `Structure` 校验 / sanitize | `ValidationPlugin` → `tools/post-execute` |
| `pm.save_session(...)` | `PersistenceConsumer` → Session 日志订阅 |
| `tracer.*(...)` | `TraceConsumer` → Session 日志订阅 |
| `skills.load` + 工具注册 | `SkillRegistry` → `ToolRuntime` 可逆注册 |
| `build_system_prompt` 每步覆写 | `SystemPromptPlugin` → `tools/result` / `agent/pre-step` |
| `OUTPUT_TOOL_NAMES` 硬编码终结 | `FinalOutputPlugin` → `agent/post-tool`（Loop 不认识工具名） |

### 两处有意偏离 legacy

1. **超时报 `error` 而非 `ok`**：legacy 的 `CallFunc.call_with_timeout` 把超时也返回成字符串，管道只好把它当成功。现在 `ToolTimeoutPlugin` 抛 `ToolTimeout`，权威结果明确是 `error`，下游可按 `status` 区分「超时」与「成功」。
2. **`AgentTrace.log_llm_call` 改收纯数据**：原签名吃 OpenAI SDK 的响应对象，逼得日志消费者伪造一个假响应。现在只收已归一化的 `messages` / `content` / `tool_calls`，SDK 形状的耦合留在 provider 一层。

### 一处未装

`build_harness()` **不装 `PermissionPlugin`**——legacy 没有审批概念，装了会改变行为。审批能力本身在 `miniharness.py` 里，需要时按 §5 自行装配。

## 5. 怎么加一个策略（不碰 Loop）

```python
class AuditPlugin(Plugin):
    inject = ("tools",)

    def apply(self, ctx):
        ctx.on("tools/pre-execute", self._pre)

    def _pre(self, payload, next_):
        log(payload["call"])          # 观察
        return next_()                # 放行；返回 {"kind": "deny", ...} 即短路
```

`Loop` 一行都不用改——这正是本骨架存在的理由。

## 6. 测试 seam

| seam | 层 | 覆盖 |
| --- | --- | --- |
| **S2** | `Loop.turn`（集成） | 工具体执行、`tool/result` 按序入日志、最终答案正确；deny 时工具体不执行；终结工具收尾本轮；**只换装配的插件就改变结局，而 Loop 零改动** |
| **S3** | `ToolRuntime.run`（契约） | deny → 工具体不执行；单调 guard 不可反向放行；ask 无审批者默认拒绝；工具异常收敛为结构化 error |
| **S5** | `Session` 投影（纯函数） | 确定且幂等；只含 model-visible 事件；压缩是 surface 替换、原日志可重放；`session_from_messages` 往返等价 |

事件总线原语与插件装载 / disposer 是框架原语，按规格**不设 seam**。

## 7. 运行

```bash
pip install -r requirements.txt
# 在项目根目录创建 config.json（已 .gitignore）
python MiniAgent.py
```

离线跑（不联网，用 mock provider）：

```python
from MiniAgent import build_harness
from miniharness import MockLLM

llm = (MockLLM()
       .then_tool_call("load_skill", {"name": "calculator"})
       .then_tool_call("calculate", {"expression": "6*7"})
       .then_text("42"))
ctx, session, loop = build_harness(model="mock", llm=llm, store_dir="./agent_sessions")
print(loop.turn("6*7 是多少"))
print([e["type"] for e in session.events])
```

测试：

```bash
python -m pytest -q
```
