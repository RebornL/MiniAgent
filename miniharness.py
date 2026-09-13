"""miniharness.py — 最简化 Agent Harness（miniharness）

与既有 `MiniAgent.py` 及各模块**并存**：本文件不修改任何既有模块，可独立装配运行。

落地 5 个原语（右侧是 dsh / Cordis 的真实结构）：

1. **Context**     -> `vendor/cordis/src/{context,events}.ts`：服务注册表 + 事件总线（emit/waterfall/serial）
2. **Session**     -> `packages/core/session`：append-only 事件日志 + `derive_messages` 投影
3. **能力 seam**    -> `packages/*/README.md`：Definition（`LLM.complete` 契约）+ Provider + Consumer
4. **Plugin**      -> `vendor/cordis/src/{registry,fiber}.ts`：`inject` 声明依赖，依赖就绪才激活
5. **可逆注册**     -> `ctx.effect(disposer)`：每个注册返回 disposer，卸载/释放逆序 unwind

在此之上是两个消费者：

- **ToolRuntime** -> `packages/core/tools/src/index.ts`，工具执行流水线固定顺序：
  `tools/pre-execute (allow|deny|ask)` → 单调 guard → `tools/execute`(around)
  → `tools/post-execute` → `finalizeContent` → `tools/result`（不可变权威结果）
- **Loop** -> `packages/core/agent-loop`：只做「驱动 + 派发事件」，**零策略**。
  权限/超时/重试/压缩全部是订阅事件的插件（见本文件 `PermissionPlugin`，以及
  `miniharness_plugins.py` 的 `ToolTimeoutPlugin` / `RetryPlugin` / `CompactionPlugin` /
  `ValidationPlugin` 等迁移插件）。

核心原则：**Model-visible means logged** —— 凡进模型的内容都必须能从 Session 日志重建。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "Context",
    "Plugin",
    "Session",
    "ToolDefinition",
    "ToolRuntime",
    "Loop",
    "LLM",
    "MockLLM",
    "PermissionPlugin",
]

_MISSING = object()

#: 工具流水线上「决策」的严重度：guard 只能沿此序收紧，不可反向放行。
_DECISION_RANK = {"allow": 0, "ask": 1, "deny": 2}


# ═══════════════ 原语 1 + 5：Context（注册表 + 事件总线 + 可逆效应栈） ═══════════════
class Context:
    """唯一的注册表与事件总线，所有扩展点汇聚于此。

    - 服务：`provide(key, service)` / `get(key)`；
    - 事件三语义：`emit`（观察）/ `waterfall`（洋葱，可 short-circuit）/ `serial`（按序，返回假则停）；
    - 效应：`effect(disposer)` 入栈，`dispose()` 逆序 unwind；插件 `apply` 期间的注册
      自动归属该插件的作用域，`unload(plugin)` 可单独逆序撤销。
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._effects: list[Callable[[], None]] = []      # 全局 disposer 栈
        self._scopes: list[list[Callable[[], None]]] = []  # 插件 apply 期间的子作用域
        self._activations: dict[int, _Activation] = {}
        self._pending: list[Any] = []                      # 依赖未就绪的插件
        self._activating = False

    # ── 服务 ───────────────────────────────────────
    def provide(self, key: str, service: Any) -> Callable[[], None]:
        """注册服务，返回撤销它的 disposer；注册后重试激活挂起的插件。"""
        previous = self._services.get(key, _MISSING)
        self._services[key] = service

        def dispose() -> None:
            if previous is _MISSING:
                self._services.pop(key, None)
            else:
                self._services[key] = previous

        self.effect(dispose)
        self._activate_pending()
        return dispose

    def get(self, key: str, default: Any = None) -> Any:
        return self._services.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._services

    # ── 事件三语义 ─────────────────────────────────
    def on(self, event: str, fn: Callable[..., Any]) -> Callable[[], None]:
        """订阅事件，返回退订 disposer（在插件作用域内自动随插件卸载撤销）。"""
        handlers = self._listeners.setdefault(event, [])
        handlers.append(fn)

        def dispose() -> None:
            if fn in handlers:
                handlers.remove(fn)

        return self.effect(dispose)

    def listeners(self, event: str) -> tuple[Callable[..., Any], ...]:
        return tuple(self._listeners.get(event, ()))

    def emit(self, event: str, payload: dict) -> None:
        """观察：逐个通知，忽略返回值。"""
        for fn in self.listeners(event):
            fn(payload)

    def waterfall(self, event: str, payload: dict, default: Any) -> Any:
        """洋葱：监听器拿到 `next` 委托后继；不调 `next` 即 short-circuit。"""
        handlers = self.listeners(event)

        def run(i: int) -> Any:
            if i == len(handlers):
                return default(payload) if callable(default) else default
            return handlers[i](payload, lambda: run(i + 1))

        return run(0)

    def serial(self, event: str, payload: dict) -> None:
        """按序：任一监听器返回假（False）即停止后续。"""
        for fn in self.listeners(event):
            if fn(payload) is False:
                break

    # ── 可逆效应（原语 5）───────────────────────────
    def effect(self, disposer: Callable[[], None]) -> Callable[[], None]:
        if self._scopes:
            self._scopes[-1].append(disposer)
        self._effects.append(disposer)
        return disposer

    def dispose(self) -> None:
        """逆序 unwind 全部注册。"""
        self._pending.clear()
        while self._effects:
            self._effects.pop()()
        self._activations.clear()

    # ── 插件装载（原语 4）───────────────────────────
    def load(self, plugin: Any) -> str:
        """装载插件：依赖（`inject`）未就绪则挂起，就绪后自动激活。"""
        if id(plugin) in self._activations:
            return "active"
        if plugin not in self._pending:
            self._pending.append(plugin)
        self._activate_pending()
        return "active" if id(plugin) in self._activations else self._pending_reason(plugin)

    def unload(self, plugin: Any) -> bool:
        """卸载插件：其全部注册逆序撤销，不留悬空引用。"""
        if plugin in self._pending:
            self._pending.remove(plugin)
            return True
        activation = self._activations.pop(id(plugin), None)
        if activation is None:
            return False
        owned = list(activation.disposers)
        activation.unwind()
        self._effects = [d for d in self._effects if d not in owned]
        return True

    def _pending_reason(self, plugin: Any) -> str:
        missing = [d for d in getattr(plugin, "inject", ()) if not self.has(d)]
        return f"pending，等待服务: {missing}"

    def _deps_ready(self, plugin: Any) -> bool:
        return all(self.has(dep) for dep in getattr(plugin, "inject", ()))

    def _activate_pending(self) -> None:
        if self._activating:      # 激活过程中又 provide 时由外层循环兜住
            return
        self._activating = True
        try:
            while True:
                ready = [p for p in self._pending if self._deps_ready(p)]
                if not ready:
                    return
                for plugin in ready:
                    self._pending.remove(plugin)
                    self._apply(plugin)
        finally:
            self._activating = False

    def _apply(self, plugin: Any) -> None:
        scope: list[Callable[[], None]] = []
        self._scopes.append(scope)
        try:
            plugin.apply(self)
        except Exception:
            rolled_back = list(scope)
            while scope:          # 激活失败：回滚本次注册
                scope.pop()()
            self._effects = [d for d in self._effects if d not in rolled_back]
            raise
        finally:
            self._scopes.pop()
        self._activations[id(plugin)] = _Activation(scope)


@dataclass
class _Activation:
    """一次插件激活及其注册的 disposer（逆序 unwind）。"""

    disposers: list[Callable[[], None]] = field(default_factory=list)

    def unwind(self) -> None:
        while self.disposers:
            self.disposers.pop()()


# ═══════════════ 原语 4：Plugin（inject 声明依赖，按需激活） ═══════════════
class Plugin:
    """插件契约：声明 `inject` 依赖，在 `apply(ctx)` 里注册能力（注册皆可逆）。"""

    inject: Sequence[str] = ()

    def apply(self, ctx: Context) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


# ═══════════════ 原语 2：Session（append-only 事件日志 + surface 投影） ═══════════════
@dataclass
class Session:
    """会话 = append-only 的 typed 事件日志；模型历史是 `derive_messages()` 的投影。

    - `append(type, **data)` 只增不改，历史可重放、可审计；
    - `compact()` 做 **surface 替换**（日志保留原始事件，仅在投影时遮蔽被替换区间）；
    - 凡进模型的内容必然先入日志（Model-visible means logged）。
    """

    events: list[dict] = field(default_factory=list)
    seq: int = 0
    _subscribers: list[Callable[[dict], None]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.events:
            self.seq = max(self.seq, self.events[-1]["seq"])

    def append(self, type: str, **data: Any) -> dict:
        """只增不改：追加一条事件并返回它。"""
        self.seq += 1
        event = {"seq": self.seq, "type": type, **data}
        self.events.append(event)
        for fn in tuple(self._subscribers):   # 消费者观察日志，不修改日志
            fn(event)
        return event

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """订阅后续追加的事件（持久化/追踪等日志消费者），返回退订 disposer。"""
        self._subscribers.append(fn)

        def dispose() -> None:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

        return dispose

    def compact(self, summary: str, replaced_seqs: Iterable[int]) -> dict:
        """压缩 = surface 替换：被替换的原始事件留在日志里，投影时以摘要代之。"""
        return self.append("context/compacted", summary=summary,
                           replaced_seqs=sorted(set(replaced_seqs)))

    def derive_messages(self) -> list[dict]:
        """把日志投影成模型可见的 messages（确定且幂等）。"""
        return [message for _, message in self.derive_entries()]

    def derive_entries(self) -> list[tuple[dict, dict]]:
        """带来源的投影：`(日志事件, 模型可见消息)` 对。

        策略（如压缩）可据此定位要 surface 替换的 `seq`，而不用重新实现投影逻辑。
        """
        masked: set[int] = set()
        anchors: dict[int, list[str]] = {}
        for event in self.events:
            if event["type"] != "context/compacted":
                continue
            seqs = event.get("replaced_seqs") or []
            masked.update(seqs)
            if seqs:
                anchors.setdefault(min(seqs), []).append(event["summary"])

        # system prompt 是「状态」而非「历史」：多条 system/message 只投影最后一条，且恒置最前。
        # legacy 每步覆写 messages[0]；把 system 留在原位置会产生中位 system 消息，
        # 部分 OpenAI 兼容接口会拒绝这种排列。
        latest_system: tuple[dict, dict] | None = None
        for event in self.events:
            if event["type"] == "system/message" and event["seq"] not in masked:
                latest_system = (event, {"role": "system", "content": event["content"]})

        entries: list[tuple[dict, dict]] = []
        if latest_system is not None:
            entries.append(latest_system)
        for event in self.events:
            for summary in anchors.get(event["seq"], ()):
                # 摘要落在被替换区间的起始位置（日志尾部 append，投影时归位）
                entries.append((event, {"role": "user", "content": f"[上下文已压缩] {summary}"}))
            if event["seq"] in masked:
                continue
            kind = event["type"]
            if kind == "system/message":
                continue
            if kind == "user/message":
                entries.append((event, {"role": "user", "content": event["content"]}))
            elif kind == "assistant/message":
                message = {"role": "assistant", "content": event.get("content", "")}
                if event.get("tool_calls"):
                    message["tool_calls"] = event["tool_calls"]
                entries.append((event, message))
            elif kind == "tool/result":
                entries.append((event, {"role": "tool", "content": event["content"],
                                        "tool_call_id": event.get("call_id", "")}))
            elif kind == "tool/denied":
                # 被拒的调用没有权威结果，但模型仍需看到这次观察（保持 tool_calls 配对完整）
                entries.append((event, {"role": "tool", "content": event["reason"],
                                        "tool_call_id": event.get("call_id", "")}))
        return entries

    def restore(self, messages: list[dict]) -> None:
        """把持久化的 messages 还原进日志（`session_from_messages` 的逆投影）。

        恢复的是「这些历史已经发生过」的那段日志：直接赋值（而非 append），
        以免把重放当成新事件通知日志消费者。
        """
        restored = self.session_from_messages(messages)
        self.events = restored.events
        self.seq = restored.seq

    @classmethod
    def session_from_messages(cls, messages: list[dict]) -> "Session":
        """`derive_messages` 的逆：把模型可见的 messages 还原成事件日志。

        用于恢复持久化的会话（`Persistence.load_session` 只存 messages）：
        `session_from_messages(msgs).derive_messages() == msgs`。
        """
        session = cls()
        for message in messages:
            role = message.get("role")
            if role == "system":
                session.append("system/message", content=message.get("content", ""))
            elif role == "user":
                session.append("user/message", content=message.get("content", ""))
            elif role == "assistant":
                session.append("assistant/message", content=message.get("content", ""),
                               tool_calls=list(message.get("tool_calls") or []))
            elif role == "tool":
                session.append("tool/result", content=message.get("content", ""),
                               call_id=message.get("tool_call_id", ""))
        return session


# ═══════════════ 原语 3：ToolRuntime（工具定义 + 固定流水线） ═══════════════
@dataclass
class ToolDefinition:
    """工具契约：工具作者只声明这些字段，执行失败走结构化结果而非抛异常。"""

    name: str
    description: str
    parameters: dict
    execute: Callable[[dict], Any]
    timeoutMs: int = 0
    finalizeContent: Callable[[Any], str] | None = None


class ToolRuntime(Plugin):
    """工具 seam 的 Provider：注册表 + 固定顺序的执行流水线。

    `tools/pre-execute (allow|deny|ask)` → 单调 guard → `tools/execute`(around)
    → `tools/post-execute` → `finalizeContent` → `tools/result`。

    返回值：`{"status": "ok"|"error"|"denied", "name", "content", ...}`；
    只有 `ok`/`error` 是**权威结果**（会派发 `tools/result`），`denied` 表示工具体未执行。
    """

    inject = ("session",)

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        self._tools: dict[str, ToolDefinition] = {}
        ctx.provide("tools", self)

    # ── 注册（可逆）────────────────────────────────
    def register(self, tool: ToolDefinition) -> Callable[[], None]:
        """注册工具，返回撤销注册的 disposer。"""
        previous = self._tools.get(tool.name)
        self._tools[tool.name] = tool

        def dispose() -> None:
            if previous is None:
                self._tools.pop(tool.name, None)
            else:
                self._tools[tool.name] = previous

        return self._ctx.effect(dispose)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def specs(self) -> list[dict]:
        """供 LLM provider 消费的工具描述（OpenAI function 形状）。"""
        return [
            {"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in self._tools.values()
        ]

    # ── 执行流水线 ─────────────────────────────────
    def run(self, call: dict) -> dict:
        """执行一次工具调用，返回结构化结果（永不抛异常）。"""
        name = call.get("name", "")
        tool = self._tools.get(name)
        payload = {"call": call, "tool": tool, "args": call.get("args") or {}}
        if tool is None:
            error = f"工具未注册: {name}"
            result: dict = {"status": "error", "name": name, "error": error, "content": error}
        else:
            result = self._pipeline(payload, tool)
            if result["status"] == "denied":
                return result

        # 4) post-execute：观察或改写结果（错误结果同样可见）
        result = self._ctx.waterfall("tools/post-execute", {**payload, "result": result},
                                     lambda p: p["result"])
        # 5) finalizeContent：最后的 content 不变量
        result = self._finalize(tool, result)
        # 6) tools/result：不可变权威结果（仅 ok/error 会走到这里）
        self._ctx.emit("tools/result", {**payload, "result": result})
        return dict(result)

    def _pipeline(self, payload: dict, tool: ToolDefinition) -> dict:
        """pre-execute → guard → execute，返回 ok/error/denied 结果。"""
        name = tool.name
        # 1) pre-execute：权限/审批/沙箱
        decision = self._ctx.waterfall("tools/pre-execute", payload, lambda p: {"kind": "allow"})
        # 2) 单调 guard：只允许收紧（allow → ask → deny），不可反向放行
        decision = self._guard(payload, decision)
        kind = decision.get("kind", "allow")
        if kind == "ask":
            kind = self._approve(payload, decision)
        if kind == "deny":
            return {"status": "denied", "name": name,
                    "reason": decision.get("reason") or f"工具调用被拒绝: {name}"}

        # 3) execute：around 包装（超时/重试/计量挂这里）
        try:
            value = self._ctx.waterfall(
                "tools/execute",
                {**payload, "timeoutMs": tool.timeoutMs},
                lambda p: tool.execute(p["args"]),
            )
        except Exception as exc:  # 工具异常 → 结构化失败结果，不崩整轮
            return {"status": "error", "name": name, "error": f"{type(exc).__name__}: {exc}"}
        return {"status": "ok", "name": name, "value": value}

    def _guard(self, payload: dict, decision: dict) -> dict:
        """单调 guard：监听器可返回更严格的决策；更宽松的返回被忽略。"""
        for fn in self._ctx.listeners("tools/guard"):
            tighter = fn(payload, dict(decision))
            if not tighter:
                continue
            kind = tighter.get("kind")
            if kind in _DECISION_RANK and _DECISION_RANK[kind] > _DECISION_RANK[decision.get("kind", "allow")]:
                decision = tighter
        return decision

    def _approve(self, payload: dict, decision: dict) -> str:
        """ask → 交给审批策略裁决；无审批者时默认拒绝（安全侧）。"""
        fallback = decision.get("reason") or f"需要人工批准: {payload['call'].get('name')}"
        resolved = self._ctx.waterfall(
            "tools/approve", {**payload, "reason": fallback},
            lambda p: {"kind": "deny", "reason": fallback},
        )
        return "allow" if resolved.get("kind") == "allow" else "deny"

    def _finalize(self, tool: ToolDefinition | None, result: dict) -> dict:
        """收敛出模型可见的 `content`（恒为字符串）。"""
        if result["status"] == "ok":
            value = result.get("value")
            result["content"] = (tool.finalizeContent(value) if tool.finalizeContent
                                 else _to_content(value))
        else:
            result["content"] = result.get("error", "")
        return result


def _to_content(value: Any) -> str:
    """canonical 值 → 模型可见文本。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


# ═══════════════ Loop：可替换的循环，零策略 ═══════════════
class Loop(Plugin):
    """只做「驱动 + 派发事件」：取输入 → `agent/pre-step` → llm seam → tools 管线 → 落日志。

    每个工具结果入日志后派发 `agent/post-tool`（waterfall，默认 `{"continue": True}`）；
    监听者返回 `{"continue": False, "answer": ...}` 即以该 answer 收尾本轮。

    循环体内没有任何权限、超时、重试、压缩判断——它们都是 `ctx.on(...)` 订阅者。
    """

    inject = ("session", "tools", "llm")

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        ctx.provide("loop", self)

    def turn(self, user_input: str, max_steps: int = 8) -> str:
        ctx = self._ctx
        session: Session = ctx.get("session")
        tools: ToolRuntime = ctx.get("tools")
        llm: LLM = ctx.get("llm")

        session.append("turn/start", input=user_input)

        # 派发 pre-step（插件可拒绝/改写输入），默认放行
        step = ctx.waterfall("agent/pre-step", {"input": user_input, "session": session},
                             lambda p: {"enter": True})
        if not step.get("enter", True):
            session.append("turn/end", status="rejected")
            return step.get("reason", "(已拒绝)")
        session.append("user/message", content=user_input)

        for _ in range(max_steps):
            reply = llm.complete(session.derive_messages())   # 只依赖 llm seam
            text = reply.get("text") or ""
            calls = list(reply.get("tool_calls") or [])
            session.append("assistant/message", content=text, tool_calls=calls)

            if not calls:
                ctx.serial("agent/turn-stopping", {"session": session, "text": text})
                session.append("turn/end", status="done")
                return text

            # 先跑完整批（每个 tool_call 都必须落一条配对事件），再决定收尾。
            # 中途 return 会让后续调用永不落日志 → 下轮上行 tool_calls 配对不完整（400）。
            answer: str | None = None
            denial: str | None = None
            for call in calls:
                result = tools.run(call)                      # 只依赖 tools seam
                if result["status"] == "denied":
                    # 没有权威结果 → 不写 tool/result，但仍需落配对事件并继续本批
                    session.append("tool/denied", name=result["name"],
                                   call_id=call.get("id", ""), reason=result["reason"])
                    if denial is None:
                        denial = result["reason"]
                    continue
                session.append("tool/result", name=result["name"], call_id=call.get("id", ""),
                               args=call.get("args") or {},
                               content=result["content"], status=result["status"])
                # 通用收尾 seam：终结策略在此短路本轮（Loop 不认识任何具体工具名）
                stop = ctx.waterfall(
                    "agent/post-tool",
                    {"session": session, "call": call, "result": result},
                    lambda p: {"continue": True},
                )
                if not stop.get("continue", True) and answer is None:
                    answer = stop.get("answer", "")

            if answer is not None:
                session.append("turn/end", status="done")
                return answer
            if denial is not None:
                session.append("turn/end", status="denied")
                return denial

        session.append("turn/end", status="max-steps")
        return "⚠️ 达到最大步数限制"


# ═══════════════ 能力 seam：LLM 的 Definition 与 mock Provider ═══════════════
class LLM(Plugin):
    """LLM seam 的 Definition：provider 只需实现 `complete(messages) -> {text, tool_calls?}`。

    工具描述通过 `ctx.get("tools").specs()` 自取，循环不认识任何具体 provider。
    """

    def apply(self, ctx: Context) -> None:
        ctx.provide("llm", self)

    def complete(self, messages: list[dict]) -> dict:
        raise NotImplementedError


class MockLLM(LLM):
    """剧本化的 mock provider：按序回放「返回 tool_call」或「返回最终文本」。

    剧本项可以是 dict，也可以是 `callable(messages) -> dict`（用于让 mock 观察模型可见历史）。
    每次调用都把收到的 messages 快照存进 `calls`，供测试断言 seam 契约。
    """

    def __init__(self, script: Iterable[Any] | None = None) -> None:
        self.script: list[Any] = list(script or [])
        self.calls: list[list[dict]] = []

    def then_tool_call(self, name: str, args: dict | None = None, *,
                       text: str = "", call_id: str | None = None) -> "MockLLM":
        self.script.append({"text": text, "tool_calls": [{
            "id": call_id or f"call_{len(self.script) + 1}",
            "name": name,
            "args": args or {},
        }]})
        return self

    def then_text(self, text: str) -> "MockLLM":
        self.script.append({"text": text})
        return self

    def complete(self, messages: list[dict]) -> dict:
        self.calls.append([dict(m) for m in messages])
        if not self.script:
            return {"text": ""}
        item = self.script.pop(0)
        return item(messages) if callable(item) else dict(item)


# ═══════════════ 示例策略插件（证明「策略注入不改循环」） ═══════════════
class PermissionPlugin(Plugin):
    """审批策略：挂 `tools/pre-execute`，危险工具直接 deny（工具体不会执行）。"""

    inject = ("tools",)

    def __init__(self, denied: Iterable[str] = (), reason: str = "该工具需要人工批准") -> None:
        self.denied = set(denied)
        self.reason = reason

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/pre-execute", self._pre)

    def _pre(self, payload: dict, next_: Callable[[], Any]) -> dict:
        name = payload["call"].get("name")
        if name in self.denied:
            return {"kind": "deny", "reason": f"{self.reason}: {name}"}
        return next_()


# ═══════════════ 装配示例（也是 smoke run：python miniharness.py） ═══════════════
def _demo() -> None:
    """装配一个可运行的 miniharness：故意乱序装载，由依赖关系决定激活顺序。"""
    ctx = Context()
    ctx.provide("session", Session())

    loop, llm = Loop(), MockLLM()
    llm.then_tool_call("calculate", {"expression": "16 * 2"}).then_text("16 * 2 = 32")

    # 乱序装载：Loop 依赖未就绪 → pending；provide 后自动激活
    for name, plugin in (("loop", loop), ("permission", PermissionPlugin(denied={"write_file"})),
                         ("llm", llm), ("tools", ToolRuntime())):
        print(f"load {name:<10} -> {ctx.load(plugin)}")

    tools: ToolRuntime = ctx.get("tools")
    tools.register(ToolDefinition(
        name="calculate", description="算数", parameters={"expression": {"type": "string"}},
        execute=lambda a: eval(a["expression"]),  # noqa: S307 - demo only
    ))
    tools.register(ToolDefinition(
        name="write_file", description="写文件",
        parameters={"path": {"type": "string"}, "content": {"type": "string"}},
        execute=lambda a: "written",
    ))

    session: Session = ctx.get("session")
    print("tools.specs:", [s["function"]["name"] for s in tools.specs()])
    print("turn ->", loop.turn("16 * 2 是多少"))
    print("log   ->", [(e["seq"], e["type"]) for e in session.events])

    llm2 = ctx.get("llm")
    llm2.script = [{"text": "", "tool_calls": [{"id": "c9", "name": "write_file",
                                                "args": {"path": "x", "content": "y"}}]}]
    print("turn ->", loop.turn("写个文件"))


if __name__ == "__main__":
    _demo()
