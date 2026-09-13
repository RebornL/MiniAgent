"""tools.runtime —— 工具运行时（Provider）：注册表 + 固定顺序的执行流水线。

`tools/pre-execute (allow|deny|ask)` → 单调 guard → `tools/execute`(around)
→ `tools/post-execute` → `finalize_content` → `tools/result`（不可变权威结果）。

权限 / 超时 / 重试 / 校验策略都不写在这里，而是订阅上述事件的插件（见 `capabilities/`）；
本包只保证流水线顺序与「批内每个调用都有配对结果」。

`tools/execute` 的 around 包装可以让出正常返回值，也可以**返回** `AbortOutcome`
（超时 / 取消 / …）来短路——两种结局走同一条结果通道，本包把它们规范化成同构的结果 dict。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from miniharness.core import Context, Plugin
from miniharness.tools.contract import (
    DENIED,
    FAILED,
    OK,
    AbortOutcome,
    ToolDefinition,
)

__all__ = ["ToolRuntime"]


#: 工具流水线上「决策」的严重度：guard 只能沿此序收紧，不可反向放行。
_DECISION_RANK = {"allow": 0, "ask": 1, "deny": 2}


class ToolRuntime(Plugin):
    """工具 seam 的 Provider：注册表 + 固定顺序的执行流水线。

    `tools/pre-execute (allow|deny|ask)` → 单调 guard → `tools/execute`(around)
    → `tools/post-execute` → `finalize_content` → `tools/result`。

    返回值：`{"status": "ok"|"timed_out"|"cancelled"|"denied"|"failed", "name", "content", ...}`；
    成功带 `value`，中止结局带 `error`，两者同构、同走一条结果通道。只有 `denied` 表示
    工具体**未执行**（无权威结果，不派发 `tools/result`），其余结局都会派发。
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
            result: dict = {"status": FAILED, "name": name, "error": error, "content": error}
        else:
            result = self._pipeline(payload, tool)
            if result["status"] == DENIED:
                return result

        # 4) post-execute：观察或改写结果（中止结果同样可见）
        result = self._ctx.waterfall("tools/post-execute", {**payload, "result": result},
                                     lambda p: p["result"])
        # 5) finalize_content：最后的 content 不变量
        result = self._finalize(tool, result)
        # 6) tools/result：不可变权威结果（除 denied 外的结局都走到这里）
        self._ctx.emit("tools/result", {**payload, "result": result})
        return dict(result)

    def _pipeline(self, payload: dict, tool: ToolDefinition) -> dict:
        """pre-execute → guard → execute，返回 ok / 四种中止结局之一。"""
        name = tool.name
        # 1) pre-execute：权限 / 审批（沙箱不在这条事件上：它在工具体那层，见 shell.provider）
        decision = self._ctx.waterfall("tools/pre-execute", payload, lambda p: {"kind": "allow"})
        # 2) 单调 guard：只允许收紧（allow → ask → deny），不可反向放行
        decision = self._guard(payload, decision)
        kind = decision.get("kind", "allow")
        if kind == "ask":
            kind = self._approve(payload, decision)
        if kind == "deny":
            reason = decision.get("reason") or f"工具调用被拒绝: {name}"
            return {"status": DENIED, "name": name, "error": reason, "content": reason}

        # 3) execute：around 包装（超时/重试/计量挂这里）。策略可短路：**返回**
        #    `AbortOutcome`（超时/取消/…）而不调用 next_，中止因此与成功同走一条结果通道。
        try:
            outcome = self._ctx.waterfall(
                "tools/execute",
                {**payload, "timeout_ms": tool.timeout_ms},
                lambda p: tool.execute(p["args"]),
            )
        except Exception as exc:  # 工具体异常 → 结构化 failed 结局，不崩整轮
            return {"status": FAILED, "name": name, "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(outcome, AbortOutcome):
            return {"status": outcome.code, "name": name, "error": outcome.error}
        return {"status": OK, "name": name, "value": outcome}

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
        """收敛出模型可见的 `content`（恒为字符串）：成功取 value，中止取 error。"""
        if result["status"] == OK:
            value = result.get("value")
            result["content"] = (tool.finalize_content(value) if tool.finalize_content
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
