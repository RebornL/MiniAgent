"""tests.support —— 跨包集成测试的共享装配 helper。

只被 `tests/` 下的集成测试使用；包内测试自带所需的最小 helper，也不依赖本模块。
日志型 helper（`_event_types()`）按「下层 helper 留下层」的规则放在
`miniharness/session/test_projection.py`，需要它的测试直接向下 import。
"""
from __future__ import annotations

from miniharness.core import Context
from miniharness.loop import Loop
from miniharness.session import Session
from miniharness.tools.runtime import ToolRuntime
from providers.mock import MockLLM


CALC_PARAMS = {"expression": {"type": "string"}}
WRITE_PARAMS = {"path": {"type": "string"}, "content": {"type": "string"}}


def _assemble(llm: MockLLM, *, plugins=(), tools=(), session: Session | None = None):
    """装配一个最小 harness：Loop 先装载（依赖未就绪 → pending），依赖齐后自动激活。"""
    ctx = Context()
    session = session if session is not None else Session()
    ctx.provide("session", session)
    loop, runtime = Loop(), ToolRuntime()
    ctx.load(loop)
    ctx.load(runtime)
    ctx.load(llm)
    for plugin in plugins:
        ctx.load(plugin)
    for tool in tools:
        runtime.register(tool)
    return ctx, session, loop, runtime


def _scripted(tool_name: str, args: dict, text: str) -> MockLLM:
    """按剧本装配 mock provider：先发一个 tool_call，再以文本收尾。"""
    return MockLLM().then_tool_call(tool_name, args).then_text(text)
