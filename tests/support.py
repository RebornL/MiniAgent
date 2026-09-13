"""tests.support —— 跨包集成测试的共享装配 helper。

只被 `tests/` 下的集成测试使用；包内测试自带所需的最小 helper，也不依赖本模块。
日志型 helper（`_event_types()`）按「下层 helper 留下层」的规则放在
`miniharness/session/test_projection.py`、进程存活探针（`_alive` / `_assert_gone`）放在
`providers/process/probe.py`，需要它们的测试直接向下 import——**整个仓库只有一份实现**，
抄第二份会让「独立确认」失去独立性。
"""
from __future__ import annotations

import sys

from capabilities.shell.definition import RUN_COMMAND_NAME, RUN_COMMAND_TOOL
from capabilities.shell.provider import ShellTool
from miniharness.core import Context
from miniharness.loop import Loop
from miniharness.sandbox.contract import SandboxPolicy
from miniharness.session import Session
from miniharness.tools.contract import ToolDefinition
from miniharness.tools.runtime import ToolRuntime
from providers.mock import MockLLM
from providers.process import SubprocessSeam
from providers.sandbox import EnvSandbox

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


def _marker_command(marker) -> list[str]:
    """一条「执行了就留下文件」的命令：用来证明工具体到底跑没跑（沙箱 / shell 集成共用）。"""
    return [sys.executable, "-c", f"open(r'{marker}', 'w', encoding='utf-8').write('ran')"]


def _shell_harness(*, seam: SubprocessSeam | None = None,
                   sandbox: EnvSandbox | None = None, load_sandbox: bool = True,
                   plugin=None, tool: ShellTool | None = None, approver: bool = False,
                   policy: SandboxPolicy | None = None) -> tuple[Context, ToolRuntime]:
    """`run_command` 的最小装配：工具流水线 + 受管范围 seam +（可选）沙箱 seam。

    `tests/test_shell.py` 与 `tests/test_sandbox.py` 共用这一份：前者用 `seam` / `plugin` /
    `tool` / `approver` 验进程边界与审批，后者用 `load_sandbox=False` 做**失败注入**
    （不装载沙箱 seam）。返回 `(ctx, runtime)`，两处按需取用。
    """
    ctx = Context()
    ctx.provide("session", Session())
    runtime = ToolRuntime()
    ctx.load(runtime)
    ctx.load(seam or SubprocessSeam())
    if load_sandbox:
        ctx.load(sandbox or EnvSandbox())
    if plugin is not None:
        ctx.load(plugin)
    if approver:
        ctx.on("tools/approve", lambda payload, next_: {"kind": "allow"})
    shell = tool or ShellTool(policy=policy)
    ctx.load(shell)
    runtime.register(ToolDefinition(
        RUN_COMMAND_NAME, RUN_COMMAND_TOOL["function"]["description"],
        RUN_COMMAND_TOOL["function"]["parameters"], lambda args: shell.run_command(**args)))
    return ctx, runtime


def _scripted(tool_name: str, args: dict, text: str) -> MockLLM:
    """按剧本装配 mock provider：先发一个 tool_call，再以文本收尾。"""
    return MockLLM().then_tool_call(tool_name, args).then_text(text)
