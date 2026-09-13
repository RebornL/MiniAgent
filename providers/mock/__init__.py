"""providers.mock —— 剧本化的 mock provider（离线装配与测试用）。

`MockLLM` 实现 `miniharness.llm.contract.LLM`：按序回放「返回 tool_call」或「返回最终文本」，
并把每次收到的 messages 快照存进 `calls`。它是 seam 的 Provider，不是契约。
"""
from __future__ import annotations

from typing import Any, Iterable

from miniharness.llm.contract import LLM

__all__ = ["MockLLM"]


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
