"""包内测试（S3 辅助 seam）：工具执行流水线契约。

只依赖 `miniharness.tools`：deny 不执行工具体、单调 guard 不可反向放行、
ask 无审批者默认拒绝、四种中止结局（超时 / 取消 / 被拒 / 失败）各有稳定码且与成功同构。

本模块的 `_pipeline()` 也被 `capabilities/*/provider/test_*.py` 复用：
能力测试只需在工具流水线上多装一个策略插件。
"""
from __future__ import annotations

from typing import Any, Callable

from miniharness.core import Context, Plugin
from miniharness.session import Session
from miniharness.tools.contract import (
    ABORT_CODES,
    CANCELLED,
    DENIED,
    FAILED,
    OK,
    TIMED_OUT,
    AbortOutcome,
    ToolDefinition,
)
from miniharness.tools.runtime import ToolRuntime


class _AbortOn(Plugin):
    """测试用策略：对指定工具短路为给定的中止结局（这就是超时 / 取消策略的形态）。"""

    inject = ("tools",)

    def __init__(self, codes: dict[str, str]) -> None:
        self.codes = codes

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/execute", self._wrap)

    def _wrap(self, payload: dict, next_: Callable[[], Any]) -> Any:
        code = self.codes.get(payload["call"].get("name"))
        return AbortOutcome(code, f"{code}：未完成") if code else next_()


def _runtime_with(*tools: ToolDefinition) -> tuple[Context, ToolRuntime]:
    ctx = Context()
    ctx.provide("session", Session())
    runtime = ToolRuntime()
    ctx.load(runtime)
    for tool in tools:
        runtime.register(tool)
    return ctx, runtime


def test_tool_runtime_deny_skips_execute_body():
    ran: list[dict] = []
    ctx, runtime = _runtime_with(
        ToolDefinition("danger", "危险工具", {}, lambda a: ran.append(a) or "done"))
    ctx.on("tools/pre-execute",
           lambda payload, next_: {"kind": "deny", "reason": "危险工具被拒绝"})

    result = runtime.run({"id": "c1", "name": "danger", "args": {}})

    assert result == {"status": DENIED, "name": "danger",
                      "error": "危险工具被拒绝", "content": "危险工具被拒绝"}
    assert ran == []


def test_tool_runtime_guard_cannot_loosen_a_deny():
    ran: list[dict] = []
    ctx, runtime = _runtime_with(
        ToolDefinition("danger", "危险工具", {}, lambda a: ran.append(a) or "done"))
    ctx.on("tools/pre-execute", lambda payload, next_: {"kind": "deny", "reason": "策略拒绝"})
    ctx.on("tools/guard", lambda payload, decision: {"kind": "allow"})   # 试图反向放行

    result = runtime.run({"id": "c1", "name": "danger", "args": {}})

    assert result["status"] == DENIED
    assert ran == []


def test_tool_runtime_ask_needs_approver():
    ran: list[dict] = []
    ctx, runtime = _runtime_with(
        ToolDefinition("moderate", "中等风险", {}, lambda a: ran.append(a) or "done"))
    ctx.on("tools/pre-execute", lambda payload, next_: {"kind": "ask", "reason": "需要批准"})

    # 无审批者 → 默认拒绝（安全侧），工具体不执行
    ask_result = runtime.run({"id": "c1", "name": "moderate", "args": {}})
    assert ask_result["status"] == DENIED and ask_result["error"] == "需要批准"
    assert ran == []

    # 有审批策略放行 → 工具体执行
    ctx.on("tools/approve", lambda payload, next_: {"kind": "allow"})
    ok_result = runtime.run({"id": "c2", "name": "moderate", "args": {}})
    assert ok_result["status"] == OK and ok_result["content"] == "done"
    assert len(ran) == 1


def test_tool_runtime_tool_exception_is_a_failed_outcome():
    """工具体抛异常 → `failed` 结局（不崩整轮，也不与超时 / 被拒混同）。"""

    def boom(args: dict) -> str:
        raise ValueError("坏了")

    _, runtime = _runtime_with(ToolDefinition("boom", "会炸", {}, boom))

    result = runtime.run({"id": "c1", "name": "boom", "args": {}})

    assert result["status"] == FAILED
    assert "ValueError" in result["error"] and result["content"] == result["error"]


def test_tool_runtime_cancelled_outcome_skips_execute_body():
    """取消：策略给出 `cancelled` 结局——工具体不启动，结果仍是结构化结局而非异常。"""
    ran: list[dict] = []
    ctx, runtime = _runtime_with(
        ToolDefinition("slow", "慢工具", {}, lambda a: ran.append(a) or "不该执行"))
    ctx.load(_AbortOn({"slow": CANCELLED}))

    result = runtime.run({"id": "c1", "name": "slow", "args": {}})

    assert result["status"] == CANCELLED
    assert result["error"] == "cancelled：未完成" and result["content"] == result["error"]
    assert ran == []                                     # 未启动的调用不启动


def test_abort_outcomes_are_isomorphic_with_success_on_one_channel():
    """AC2：四种中止结局与成功同构——同形状的结果 dict，且同走 `tools/result` 权威通道。"""
    ran: list[str] = []
    channel: list[dict] = []

    def body(label: str) -> Callable[[dict], str]:
        def run(args: dict) -> str:
            ran.append(label)
            return f"{label} 的值"
        return run

    def boom(args: dict) -> str:
        ran.append(FAILED)
        raise ValueError("坏了")

    ctx, runtime = _runtime_with(
        ToolDefinition("ok_tool", "", {}, body(OK)),
        ToolDefinition("failed_tool", "", {}, boom),
        ToolDefinition("timed_out_tool", "", {}, body(TIMED_OUT)),
        ToolDefinition("cancelled_tool", "", {}, body(CANCELLED)),
        ToolDefinition("denied_tool", "", {}, body(DENIED)),
    )
    ctx.load(_AbortOn({"timed_out_tool": TIMED_OUT, "cancelled_tool": CANCELLED}))
    ctx.on("tools/pre-execute", lambda payload, next_: {"kind": "deny", "reason": "策略拒绝"}
           if payload["call"]["name"] == "denied_tool" else next_())
    ctx.on("tools/result", lambda payload: channel.append(payload["result"]))
    names = ("ok_tool", "failed_tool", "timed_out_tool", "cancelled_tool", "denied_tool")

    results = {name: runtime.run({"id": name, "name": name, "args": {}}) for name in names}

    # 每类结局都有稳定码；`ok` 之外必是四种中止结局之一（超时绝不会被当成成功）
    assert {name: r["status"] for name, r in results.items()} == {
        "ok_tool": OK, "failed_tool": FAILED, "timed_out_tool": TIMED_OUT,
        "cancelled_tool": CANCELLED, "denied_tool": DENIED}
    assert set(r["status"] for r in results.values()) == {OK} | set(ABORT_CODES)
    # 同构：status / name / content 恒在；成功多一个 value，中止多一个 error
    assert all({"status", "name", "content"} <= set(r) for r in results.values())
    assert all(isinstance(r["content"], str) for r in results.values())
    assert set(results["ok_tool"]) == {"status", "name", "value", "content"}
    assert all(set(r) == {"status", "name", "error", "content"}
               for name, r in results.items() if name != "ok_tool")
    # 同一条结果通道：除 denied（工具体未执行、无权威结果）外都派发 tools/result
    assert [r["status"] for r in channel] == [OK, FAILED, TIMED_OUT, CANCELLED]
    # 只有 ok 与 failed 到过工具体：超时被放弃、取消与被拒压根没启动
    assert ran == [OK, FAILED]


# ═══════════════ S3 辅助 seam（契约）：工具执行流水线 ═══════════════
def _pipeline(plugin, *tools: ToolDefinition) -> tuple[Context, ToolRuntime]:
    ctx = Context()
    ctx.provide("session", Session())
    runtime = ToolRuntime()
    ctx.load(runtime)
    if plugin is not None:
        ctx.load(plugin)
    for tool in tools:
        runtime.register(tool)
    return ctx, runtime
