"""llm.contract —— LLM 能力的契约（Definition）。

provider 只需实现 `complete(messages) -> {text, tool_calls?}`；工具描述经
`ctx.get("tools").specs()` 自取，循环不认识任何具体 provider。
本包不含任何后端实现（见 `providers/`）。
"""
from __future__ import annotations

from miniharness.core import Context, Plugin

__all__ = ["LLM"]


class LLM(Plugin):
    """LLM seam 的 Definition：provider 只需实现 `complete(messages) -> {text, tool_calls?}`。

    工具描述通过 `ctx.get("tools").specs()` 自取，循环不认识任何具体 provider。
    """

    def apply(self, ctx: Context) -> None:
        ctx.provide("llm", self)

    def complete(self, messages: list[dict]) -> dict:
        raise NotImplementedError
