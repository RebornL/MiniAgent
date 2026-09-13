"""包内测试（S3 辅助 seam）：工具执行流水线契约。

只依赖 `miniharness.tools`：deny 不执行工具体、单调 guard 不可反向放行、
ask 无审批者默认拒绝、工具异常收敛为结构化 error。

本模块的 `_pipeline()` 也被 `capabilities/*/provider/test_*.py` 复用：
能力测试只需在工具流水线上多装一个策略插件。
"""
from __future__ import annotations

from miniharness.core import Context
from miniharness.session import Session
from miniharness.tools.contract import ToolDefinition
from miniharness.tools.runtime import ToolRuntime


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

    assert result == {"status": "denied", "name": "danger", "reason": "危险工具被拒绝"}
    assert ran == []


def test_tool_runtime_guard_cannot_loosen_a_deny():
    ran: list[dict] = []
    ctx, runtime = _runtime_with(
        ToolDefinition("danger", "危险工具", {}, lambda a: ran.append(a) or "done"))
    ctx.on("tools/pre-execute", lambda payload, next_: {"kind": "deny", "reason": "策略拒绝"})
    ctx.on("tools/guard", lambda payload, decision: {"kind": "allow"})   # 试图反向放行

    result = runtime.run({"id": "c1", "name": "danger", "args": {}})

    assert result["status"] == "denied"
    assert ran == []


def test_tool_runtime_ask_needs_approver_and_errors_are_structured():
    ran: list[dict] = []

    def boom(args: dict) -> str:
        raise ValueError("坏了")

    ctx, runtime = _runtime_with(
        ToolDefinition("moderate", "中等风险", {}, lambda a: ran.append(a) or "done"),
        ToolDefinition("boom", "会炸", {}, boom),
    )
    ctx.on("tools/pre-execute", lambda payload, next_: {"kind": "ask", "reason": "需要批准"})

    # 无审批者 → 默认拒绝（安全侧），工具体不执行
    ask_result = runtime.run({"id": "c1", "name": "moderate", "args": {}})
    assert ask_result["status"] == "denied" and ask_result["reason"] == "需要批准"
    assert ran == []

    # 有审批策略放行 → 工具体执行
    ctx.on("tools/approve", lambda payload, next_: {"kind": "allow"})
    ok_result = runtime.run({"id": "c2", "name": "moderate", "args": {}})
    assert ok_result["status"] == "ok" and ok_result["content"] == "done"
    assert len(ran) == 1

    # 工具抛异常 → 结构化失败结果，不崩整轮
    error_result = runtime.run({"id": "c3", "name": "boom", "args": {}})
    assert error_result["status"] == "error"
    assert "ValueError" in error_result["error"] and error_result["content"] == error_result["error"]


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
