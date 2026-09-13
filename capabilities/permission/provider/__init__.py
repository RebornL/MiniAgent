"""permission.provider —— 工具审批策略的实现（Provider）。

`PermissionPlugin` 挂 `tools/pre-execute`：拒绝名单里的工具直接 `deny`（工具体不会执行），
审批名单里的工具给出 `ask`——走 `tools/approve` 瀑布线，**无审批者时默认拒绝**。
决策词汇（`allow` / `ask` / `deny`，单调收紧）是 `miniharness.tools.runtime` 的契约。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from miniharness.core import Context, Plugin

__all__ = ["PermissionPlugin"]


# ═══════════════ 示例策略插件（证明「策略注入不改循环」） ═══════════════

class PermissionPlugin(Plugin):
    """审批策略：挂 `tools/pre-execute`——`denied` 直接拒绝，`approval_required` 转 `ask`。

    `ask` 只是「交给审批者裁决」：没有装 `tools/approve` 监听器（无审批者）时，
    `ToolRuntime` 按安全侧默认拒绝，工具体不会执行。
    """

    inject = ("tools",)

    def __init__(self, denied: Iterable[str] = (), reason: str = "该工具需要人工批准",
                 approval_required: Iterable[str] = ()) -> None:
        self.denied = set(denied)
        self.approval_required = set(approval_required)
        self.reason = reason

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/pre-execute", self._pre)

    def _pre(self, payload: dict, next_: Callable[[], Any]) -> dict:
        name = payload["call"].get("name")
        if name in self.denied:
            return {"kind": "deny", "reason": f"{self.reason}: {name}"}
        if name in self.approval_required:
            return {"kind": "ask", "reason": f"{self.reason}: {name}"}
        return next_()
