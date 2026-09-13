"""final_output.provider —— 声明式终结工具的实现（Provider）。

`FinalOutputPlugin` 订阅 `agent/post-tool`：刚执行的工具名在 `terminal_tools` 内且权威结果
为 `ok` 时，以它的 `content` 短路本轮（不再采样）。收尾协议是 `miniharness.loop` 的契约。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from miniharness.core import Context, Plugin

__all__ = ["FinalOutputPlugin"]




# ═══════════════ 终结行为：FinalOutput → agent/post-tool ═══════════════
class FinalOutputPlugin(Plugin):
    """声明式终结工具：某工具执行成功即可作为本轮最终答复（Loop 零改动）。

    订阅 `agent/post-tool`：刚执行的工具名在 `terminal_tools` 内且权威结果为 `ok` 时，
    以它的 `content` 短路本轮（不再采样）；否则交给后继监听者（`next_()`）。
    """

    inject = ("tools",)

    def __init__(self, terminal_tools: Iterable[str] = ("final_output",)) -> None:
        self.terminal_tools = set(terminal_tools)

    def apply(self, ctx: Context) -> None:
        ctx.on("agent/post-tool", self._post)

    def _post(self, payload: dict, next_: Callable[[], Any]) -> dict:
        result = payload["result"]
        if result.get("status") == "ok" and result.get("name") in self.terminal_tools:
            return {"continue": False, "answer": result.get("content", "")}
        return next_()
