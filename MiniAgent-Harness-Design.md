# MiniAgent Harness 设计 —— 三方对比 + 最简落地

> 结论先行：三个 harness 的**核心循环（LLM ⇄ 工具）完全相同**，真正的差距在「**循环之外**」——
> **Codex** 用进程边界（exec-server）和协议解耦（SQ/EQ）把策略隔到循环外；**deepseek-harness(dsh)** 用「一切皆插件 + 事件瀑布线」把策略挂在循环外；**MiniAgent** 把策略（重试/超时/压缩/持久化）全**硬编码在循环内**。
>
> 本文回答两个问题：三者的本质区别是什么、以及**把 harness 工程的最小形态落地**需要哪几个原语。
> 引用约定：`codex-rs/...` 为 Codex、`packages/...`/`vendor/cordis/...`/`docs/...` 为 dsh、`MiniAgent/*.py::符号` 为 MiniAgent。

---

## 1. 三者一图定位

| | MiniAgent | Codex | deepseek-harness (`dsh`) |
|---|---|---|---|
| 语言/形态 | 单进程 Python，~9 文件 60KB | Rust 单仓 ~150 crate + TS/Python SDK | TS monorepo + Cordis 运行时，~49 能力族 |
| 核心理念 | 把 Agent 做出来 | 工业级/进程隔离/生产 harness | **一切皆插件**（Cordis），连循环都是插件 |
| 循环在哪 | `MiniAgent.py::run_agent_with_trace` 一个 for 循环 | `core/src/session/turn.rs` 双层循环（硬编码在 core） | `packages/core/agent-loop/src/agent.ts::ReactLoopAgent`（硬编码状态机，但**作为可替换 Service** 注册） |
| 策略注入方式 | 硬编码 `with_retry`/`call_with_timeout`/`maybe_compact` | 编译期分层 + 进程边界 + Starlark 策略 | **事件瀑布线** `ctx.on('tools/*', ...)`，不改循环（`docs/tool-execution-pipeline.md`） |
| 会话模型 | 可变 `messages` list（`MiniAgent.py`） | `core::context_manager` history | **append-only 事件日志 + surface 投影**（`packages/core/session`） |
| 扩展方式 | 无（改代码） | plugin Manifest + hooks + MCP | **能力 seam**（Definition/Provider/Consumer）+ hooks 桥 + MCP |

---

## 2. 三个 harness 的核心差异（哲学层）

### 2.1 「策略注入不改循环」——三种做法，同一目标

这是 harness 工程的第一原则：**循环是骨头，策略是肉，肉不能焊在骨头上。**

- **Codex** 靠**进程/协议边界**：命令执行被隔离进 `exec-server`（JSON-RPC 守护进程，`exec/Cargo.toml` 不依赖 `codex-exec-server`，证明分层）；审批靠 `execpolicy` 的 Starlark 前缀规则输出 `Allow/Prompt/Forbidden`（`execpolicy/src/decision.rs`）。策略在**循环外、甚至进程外**。
- **dsh** 靠**事件瀑布线**：循环只 `dispatch.waterfall/serial/emit` 具名事件；工具执行走 `tools/pre-execute → guards → tools/execute → tools/post-execute → finalizeContent → tools/result`（`packages/core/tools/src/index.ts`）。hooks、权限、超时、重试全部是这些事件的 `ctx.on(...)` 订阅者，**没有任何一个策略写进循环**（DshCoreLoop 结论：『循环只暴露事件，「改循环」根本没发生』）。
- **MiniAgent** 全部硬编码：`with_retry(_call)`、`call_with_timeout(...)`、`ctx.maybe_compact(...)` 直接写在 `run_agent_with_trace` 的循环体里。加一个策略 = 改循环 = 牵一发动全身。

### 2.2 会话模型：可变数组 vs 事件溯源

- **MiniAgent**：`messages: list[dict]` 直接 append，压缩时 `summarize_and_compress` 原地替换旧消息——历史**丢失不可逆**。
- **Codex**：`ContextManager`（`core/src/context_manager/history.rs`）管理模型历史，CompactionTask 远端/本地分派。
- **dsh**：`Session` 是 **append-only 的 typed 事件日志**，`system/user/assistant/tool-result` 只是**派生投影**（`deriveMessages()`）；压缩做的是 surface 替换（日志保留原始），核心原则「**Model-visible means logged**」——凡进模型的内容必须能从日志重建。这是最严格的工程形态：持久化、压缩、遥测、fork 全部从这一条日志派生（`packages/core/session/README.md`、`docs/architecture.md`）。

### 2.3 扩展模型：改源码 vs 加 crate vs 加插件

- **MiniAgent**：加工具 = 改 `TOOLS`/`TOOL_MAP` + 改循环。
- **Codex**：加能力 = 加一个 crate + 在 `core/src/tools/spec_plan.rs::build_tool_router` 按 feature 注册。
- **dsh**：加能力 = **设计一个 seam**（Service Definition 契约 + Provider 实现 + Consumer 工具），三个角色各自独立（`packages/AGENTS.md`、`.agents/notes/implemented/architecture/2026-06-13-capability-seams.md`）。连「委派给别的 harness」都是一种能力：`subagent-codex` / `subagent-claude-code` / `subagent-acp` / `subagent-dsh-sdk` 四个 provider 都是把任务甩给出进程的 Codex / Claude Code / ACP / 嵌套 dsh，只回传最终答案（`packages/subagent/*/README.md`）。

---

## 3. harness 工程的五个原语（提炼）

从 dsh 提炼，Codex 佐证，可独立落地：

1. **循环不等于策略**。循环只做「驱动 + 派发事件」；重试/超时/权限/压缩/脱敏全部是事件监听器。事件要有派发语义：`emit`（观察）/ `waterfall`（洋葱，可 short-circuit）/ `serial`（按序，可停）—— Cordis 五种模式里这三种是骨架（`vendor/cordis/src/events.ts`）。

2. **会话是 append-only 事件日志**。`append(event)`，模型历史是 `derive_messages()` 的投影；持久化/压缩/遥测都是日志的消费者，而非直接改 messages。

3. **能力 = Definition + Provider + Consumer（seam）**。契约包只定义 `ctx.<key>` 抽象 + 类型 + 事件词汇表；后端包 `register` 实现；工具/策略包 `ctx.get(key)` 消费。换后端 = 换一个 provider，不动工具。

4. **依赖注入，按需激活**。插件用 `inject: ["tools","llm"]` 声明依赖；服务未就绪时保持 pending，就绪后自动激活——启动顺序由**依赖关系**表达，不靠手写 boot 顺序（`vendor/cordis/src/fiber.ts`）。

5. **注册皆可逆**。每个 `register` 返回 disposer，插件卸载时逆序 unwind（`ctx.effect()`）。

---

## 4. 借鉴与可落地清单（对照 MiniAgent 现状）

按「性价比」排序，`★` = 可立即落地，`★★` = 需小重构，`★★★` = 需架构级改动：

| # | 借鉴项 | 来源 | 难度 | 落到 MiniAgent 的具体动作 |
|---|---|---|---|---|
| 1 | **工具执行瀑布线** pre→guard→execute→post | dsh `tools/*` | ★ | 把 `call_with_timeout`+`with_retry`+`Structure.sanitize` 收敛成 `tools/pre-execute`/`tools/execute`/`tools/post-execute` 三个事件；策略改为 `ctx.on` 订阅，循环不再 if-else 堆策略 |
| 2 | **权限/审批** | codex `execpolicy` + dsh `user-approval` | ★ | 在 pre-execute 瀑布线加一个 `approval` 监听器，输出 allow/deny/ask 三态（MiniAgent 目前零权限） |
| 3 | **会话事件日志** | dsh `session` | ★★ | 把 `messages` list 换成 append-only `Session`，`derive_messages` 供循环、`maybe_compact` 做 surface 替换而非删历史、`Persistence` 直接落日志 |
| 4 | **能力 seam** | dsh seam + codex crate | ★★ | `LLMProvider`/`ToolRuntime`/`Sandbox` 各拆 Definition+Provider+Consumer，register 到 `ctx` 键（替代模块级单例 `client`） |
| 5 | **依赖注入 + 可逆效应** | Cordis `fiber`/`effect` | ★★ | `Skill` 加载改为 `plugin.inject`，每个 `register` 返回 disposer（MiniAgent 的 `SkillManager` 已近雏形，缺 DI 与 disposer） |
| 6 | **system prompt 数据化** | codex `models.json::instructions_template` | ★ | `SYSTEM_PROMPT` 从 config.json 读，而非硬编码 |
| 7 | **进程边界 + 沙箱** | codex `exec-server`/沙箱 | ★★★ | 工具执行下沉到独立进程/至少独立线程；eval/open 进沙箱（MiniAgent 仅 `CallFunc` 线程超时，隔离不彻底） |
| 8 | **client/agent 协议解耦** | codex SQ/EQ | ★★★ | `chat_loop` 与 `run_agent_with_trace` 拆成两进程，走队列通信（可为将来 TUI/SDK 铺路） |
| 9 | **委派外部 harness** | dsh `subagent-*` | 不建议 | 与「学 harness 本身」目标冲突，仅作认知参考 |

**最小可行优先级**：先做 #1+#2+#3（恰好就是下面的最简 harness），它们三件套即可把 MiniAgent 从「单文件脚本」变成「有骨架的 harness」。

---

## 5. 最简化 MiniAgent Harness（代码骨架）

下面 ~200 行 Python 落地上面 5 个原语。**不追求能跑，追求每个原语能对应到 dsh/Cordis 的某个真实结构**，跑通一遍就对 harness 工程怎么落地有了肌肉记忆。

```python
"""miniharness.py — 最简化 Agent Harness
对应关系（右侧是 dsh/Cordis 的真实结构）：
  Context      -> vendor/cordis/src/context.ts + events.ts
  Plugin       -> vendor/cordis/src/registry.ts (function/class/{apply} + inject)
  effect 栈    -> vendor/cordis/src/fiber.ts (effect() 逆序 teardown)
  Session      -> packages/core/session (append-only 事件日志 + deriveMessages)
  Loop         -> packages/core/agent-loop (可替换 Service, 只派发事件)
  ToolRuntime  -> packages/core/tools/src/index.ts (pre→guard→execute→post→finalize→result)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable

# ═══════════════ 原语 1+5：Context（注册表 + 可逆效应 + 事件） ═══════════════
class Context:
    def __init__(self):
        self._svc: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable]] = {}
        self._effects: list[Callable] = []    # disposer 栈

    # —— 服务 ——
    def provide(self, key: str, service: Any) -> None:
        self._svc[key] = service
    def get(self, key: str) -> Any:
        return self._svc.get(key)
    # —— 可逆效应（原语 5）——
    def effect(self, disposer: Callable) -> Callable:
        self._effects.append(disposer)
        return disposer
    def dispose(self) -> None:                 # 逆序 unwind
        while self._effects:
            self._effects.pop()()
    # —— 事件（原语 1 的三种派发）——
    def on(self, event: str, fn: Callable) -> None:
        self._listeners.setdefault(event, []).append(fn)
    def emit(self, event: str, payload: dict) -> None:          # 观察，不返回值
        for fn in self._listeners.get(event, []):
            fn(payload)
    def waterfall(self, event: str, payload: dict, default: Any):  # 洋葱，可 short-circuit
        fns = self._listeners.get(event, [])
        def run(i: int) -> Any:
            if i == len(fns):
                return default(payload) if callable(default) else default
            return fns[i](payload, lambda: run(i + 1))
        return run(0)
    def serial(self, event: str, payload: dict) -> None:        # 按序，fn 返回 False 即停
        for fn in self._listeners.get(event, []):
            if fn(payload) is False:
                break

# ═══════════════ 原语 4：Plugin（inject 声明依赖，按需激活） ═══════════════
class Plugin:
    inject: list[str] = []
    def apply(self, ctx: Context) -> None: ...

def load(ctx: Context, plugin: Plugin) -> str:
    missing = [d for d in plugin.inject if ctx.get(d) is None]
    if missing:
        return f"pending, 等待服务: {missing}"     # 依赖未就绪 → 不激活（对应 fiber PENDING）
    plugin.apply(ctx)
    return "active"

# ═══════════════ 原语 2：Session — append-only 事件日志 ═══════════════
@dataclass
class Session:
    events: list[dict] = field(default_factory=list)
    seq: int = 0

    def append(self, type: str, **data: Any) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "type": type, **data}
        self.events.append(ev)                   # 只增不改
        return ev

    def derive_messages(self) -> list[dict]:     # surface 投影：仅 model-visible 事件进历史
        out = []
        for e in self.events:
            t = e["type"]
            if t == "system/message":  out.append({"role": "system",    "content": e["content"]})
            elif t == "user/message":  out.append({"role": "user",      "content": e["content"]})
            elif t == "assistant/message": out.append({"role": "assistant", "content": e["content"]})
            elif t == "tool/result":    out.append({"role": "user", "content": f"[tool] {e['name']} -> {e['content']}"})
        return out

# ═══════════════ 原语 3：ToolRuntime — 工具 + 瀑布线 ═══════════════
@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    execute: Callable[[dict], Any]
    timeoutMs: int = 0
    finalizeContent: Callable[[dict], Any] | None = None

class ToolRuntime(Plugin):
    inject = ["session"]
    def apply(self, ctx: Context) -> None:
        self.ctx = ctx
        self._tools: dict[str, ToolDefinition] = {}
        ctx.provide("tools", self)

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        self.ctx.effect(lambda t=tool.name: self._tools.pop(t, None))   # 原语 5：可逆

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def run(self, call: dict) -> dict:
        ctx = self.ctx
        # 1) pre-execute 瀑布线（权限/审批/沙箱挂这里）
        decision = ctx.waterfall("tools/pre-execute", call, lambda _: "allow")
        if decision.get("kind") == "deny":
            return {"isError": True, "error": "denied"}

        # 2) execute 瀑布线（超时/重试/计量是 around-wrapper）
        def body(p: dict) -> Any:
            tool = self._tools[p["name"]]
            return tool.execute(p["args"])
        value = ctx.waterfall("tools/execute", call, body)

        # 3) post-execute 瀑布线（结果改写/注入提醒）
        result = {"isError": False, "value": value}
        result = ctx.waterfall("tools/post-execute", {**call, "result": result},
                               lambda p: p["result"])

        # 4) finalizeContent + result
        tool = self._tools[call["name"]]
        if tool.finalizeContent and not result.get("isError"):
            result["value"] = tool.finalizeContent(result["value"])
        ctx.emit("tools/result", {**call, "result": result})
        return result

# ═══════════════ Loop：可替换的循环，只派发事件，不含策略 ═══════════════
class Loop(Plugin):
    inject = ["session", "tools", "llm"]
    def apply(self, ctx: Context) -> None:
        self.ctx = ctx
        ctx.provide("loop", self)

    def turn(self, user_input: str) -> str:
        ctx, session = self.ctx, self.ctx.get("session")
        session.append("turn/start")
        session.append("user/message", content=user_input)

        # 分派 agent/pre-step（插件可拒绝/改写输入）—— 默认放行
        decision = ctx.waterfall("agent/pre-step", {"input": user_input},
                                 lambda p: {"enter": True})
        if not decision.get("enter"):
            return "(rejected)"

        # 调 LLM（seam：ctx.get('llm')，循环不认识具体 provider）
        messages = session.derive_messages()
        reply = self.ctx.get("llm").complete(messages)   # {text, tool_calls?}

        # 若模型要调工具，走 tools 瀑布线
        if reply.get("tool_calls"):
            for call in reply["tool_calls"]:
                r = self.ctx.get("tools").run(call)
                session.append("tool/result", name=call["name"], content=str(r))
            # 简化：工具结果后不再采样

        session.append("assistant/message", content=reply["text"])
        # 分派 agent/turn-stopping（插件可强制续跑）
        ctx.serial("agent/turn-stopping", {"session": session})
        session.append("turn/end")
        return reply["text"]

# ═══════════════ LLM seam：Provider 抽象 ═══════════════
class LLM(Plugin):
    def apply(self, ctx: Context) -> None:
        ctx.provide("llm", self)
    def complete(self, messages: list[dict]) -> dict:
        raise NotImplementedError          # 由具体 provider 覆盖

# ═══════════════════════════════════════════════════════════════
# 现在看「策略如何注入而不改循环」：两个独立插件
# ═══════════════════════════════════════════════════════════════
class PermissionPlugin(Plugin):
    """审批：挂 pre-execute 瀑布线，危险命令问用户"""
    inject = ["tools"]
    def apply(self, ctx: Context) -> None:
        ctx.on("tools/pre-execute", self._pre)
    def _pre(self, call: dict, next_: Callable) -> dict:
        if call["name"] == "write_file":
            return {"kind": "deny", "reason": "写文件需人工批准"}   # short-circuit，不调 next
        return next_()

class TimeoutPlugin(Plugin):
    """超时：挂 execute 瀑布线，around-wrapper（对应 dsh timeout-policy）"""
    inject = ["tools"]
    def apply(self, ctx: Context) -> None:
        ctx.on("tools/execute", self._wrap)
    def _wrap(self, call: dict, next_: Callable) -> Any:
        # 真实实现：用 signal/thread 限时；这里示意直接委托
        return next_()

# ═══════════════ 装配：一个可追踪的运行 ═══════════════
if __name__ == "__main__":
    ctx = Context()
    ctx.provide("session", Session())

    load(ctx, ToolRuntime())
    load(ctx, LLM())                  # TODO: 换成真实的 DeepSeek provider
    load(ctx, Loop())                 # inject 依赖齐了才激活
    load(ctx, PermissionPlugin())
    load(ctx, TimeoutPlugin())

    # 注册一个工具（可逆：dispose 后从 registry 消失）
    ctx.get("tools").register(ToolDefinition(
        name="calculate", description="算数", parameters={"expression": {"type": "string"}},
        execute=lambda a: eval(a["expression"]),
    ))

    out = ctx.get("loop").turn("16 * 2 是多少")
    print(out)
```

**这段骨架的关键断言**：`Loop.turn()` 里**没有一行** `if tool == "write_file": 拒绝` 或 `try: timeout`——策略全部在 `PermissionPlugin`/`TimeoutPlugin` 里通过 `ctx.on('tools/*')` 注入。这就是 harness 工程一句话本质：**循环驱动，事件治理。**

---

## 6. 从现状到 harness 的迁移路径

```
MiniAgent.py 现状                         最简 harness
─────────────────────────               ─────────────────────────
run_agent_with_trace 一个 for 循环       Loop.turn()（只派发事件）
  内联 with_retry(call)      ─────▶     RetryPlugin 挂 tools/execute (around)
  内联 call_with_timeout     ─────▶     TimeoutPlugin 挂 tools/execute (around)
  内联 maybe_compact         ─────▶     CompactionPlugin 挂 agent/pre-step
  模块级 client 单例          ─────▶     ctx.get("llm") provider seam
  messages list 直接改        ─────▶     Session 事件日志 + derive_messages
  TOOLS/TOOL_MAP 硬编码      ─────▶     ToolRuntime.register(tool) + 可逆
  (无权限)                   ─────▶     PermissionPlugin 挂 tools/pre-execute
```

---

## 附：三方源码引用速查

- **Codex 循环/协议/执行**：`codex-rs/core/src/{session/turn.rs,tasks/regular.rs}`、`protocol/src/protocol.rs`、`exec-server/`、`execpolicy/`、`apply-patch/`
- **Codex 能力**：`codex-rs/{skills,codex-mcp,ext/agent,model-provider,otel,plugin,core-plugins,hooks}/`
- **dsh 循环/工具/插件**：`packages/core/agent-loop/src/{agent.ts,tool-calls.ts}`、`packages/core/tools/src/{index.ts,types.ts}`、`vendor/cordis/src/{context.ts,events.ts,service.ts,registry.ts,fiber.ts}`、`packages/core/scope/src/index.ts`
- **dsh 能力 seam**：`packages/{llm,session,compaction,storage,sandbox,shell,fs,subagent,skill}/README.md`、`docs/{architecture.md,tool-execution-pipeline.md,subsystems/tools.md}`
- **MiniAgent 现状**：`MiniAgent/MiniAgent.py`（`run_agent_with_trace`/`chat_loop`/`stream_llm_call`）及 `SkillManager.py`/`Compaction.py`/`Persistence.py`/`AgentTrace.py`/`Structure.py`/`RetryFunc.py`/`CallFunc.py`

> 更细的 Codex 双雄深挖见同目录 `MiniAgent-vs-Codex-Harness.md`；本文是它的三重扩展 + 落地设计。