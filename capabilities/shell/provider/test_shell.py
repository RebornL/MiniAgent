"""包内测试：命令执行工具（`shell.provider`）。

只依赖本能力与更下层的契约：用一个实现 `ProcessSeam` 契约的**测试替身**记录 `spawn`
收到了什么、并回放输出，所以这里不跑真实子进程（真实子进程的三条验收在
`tests/test_shell.py`——那里才允许跨到 `providers.process`）。

覆盖：argv 逐项原样转交（不是 shell 字符串）、`cwd` 转交、结果形状与退出码、
输出上限与截断标记、命令字符串被拒绝、工具不自己终止受管范围。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Sequence

from capabilities.shell.definition import RUN_COMMAND_TOOL, truncated_note
from capabilities.shell.provider import ShellTool
from miniharness.core import Context
from miniharness.process.contract import DEFAULT_GRACE_MS, ManagedRange, ProcessSeam
from miniharness.session import Session
from miniharness.tools.contract import FAILED, OK, ToolDefinition
from miniharness.tools.runtime import ToolRuntime


class _FakeRange(ManagedRange):
    """受管范围替身：立刻给出退出码，并记下有人调用过终止。"""

    def __init__(self, code: int) -> None:
        self._code = code
        self.terminated = 0

    @property
    def pid(self) -> int:
        return 4242

    def poll(self) -> int | None:
        return self._code

    def wait_for_exit(self, timeout_ms: int | None = None) -> int | None:
        return self._code

    def terminate(self, grace_ms: int = DEFAULT_GRACE_MS) -> None:
        self.terminated += 1

    def release(self) -> None:
        pass


class _FakeSeam(ProcessSeam):
    """记录 `spawn` 入参并回放输出的 seam 替身。"""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", code: int = 0) -> None:
        self.stdout, self.stderr, self.code = stdout, stderr, code
        self.calls: list[dict] = []
        self.range_: _FakeRange | None = None

    def spawn(self, argv: Sequence[str], *, cwd: str | None = None,
              env: dict[str, str] | None = None, stdin: Any = None,
              stdout: Any = None, stderr: Any = None) -> ManagedRange:
        self.calls.append({"argv": list(argv), "cwd": cwd, "stdin": stdin})
        for stream, payload in ((stdout, self.stdout), (stderr, self.stderr)):
            if stream is not None:
                stream.write(payload)
                stream.flush()
        self.range_ = _FakeRange(self.code)
        return self.range_


def _harness(seam: _FakeSeam, **shell_kwargs: Any) -> tuple[ToolRuntime, ShellTool]:
    """最小装配：工具流水线 + 一个会话 + 替身 seam + `run_command`。"""
    ctx = Context()
    ctx.provide("session", Session())
    runtime = ToolRuntime()
    ctx.load(runtime)
    ctx.load(seam)
    tool = ShellTool(**shell_kwargs)
    ctx.load(tool)
    runtime.register(ToolDefinition(
        "run_command", RUN_COMMAND_TOOL["function"]["description"],
        RUN_COMMAND_TOOL["function"]["parameters"], lambda args: tool.run_command(**args)))
    return runtime, tool


def test_declared_tool_shape_takes_an_argv_list_not_a_command_string():
    """模型看到的形状：`argv` 是必填的字符串数组，`cwd` 可选，**没有**超时参数。"""
    function = RUN_COMMAND_TOOL["function"]
    properties = function["parameters"]["properties"]

    assert function["name"] == "run_command"
    assert function["parameters"]["required"] == ["argv"]
    assert properties["argv"]["type"] == "array"
    assert properties["argv"]["items"] == {"type": "string"}
    assert properties["cwd"]["type"] == "string"
    assert "timeout" not in properties               # 时限是策略层的事，不在工具契约里


def test_argv_and_cwd_reach_the_managed_range_verbatim():
    """argv 逐项原样转交（工具不拼命令、不经 shell）；`cwd` 一并转交。"""
    seam = _FakeSeam(stdout=b"hello\n", code=7)
    runtime, _ = _harness(seam)

    result = runtime.run({"id": "c1", "name": "run_command", "args": {
        "argv": ["git", "commit", "-m", "a; b c"],
        "cwd": "/somewhere/else",
    }})

    assert result["status"] == OK
    assert result["value"] == {
        "exit_code": 7,                                   # 非零退出码是正常结果，不是失败
        "stdout": "hello\n",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    assert json.loads(result["content"])["exit_code"] == 7   # 模型看到的就是这份结构化结果
    assert seam.calls == [{"argv": ["git", "commit", "-m", "a; b c"],
                           "cwd": "/somewhere/else",
                           "stdin": subprocess.DEVNULL}]  # 不把 agent 的 stdin 交给命令
    assert seam.range_.terminated == 0                    # 工具不自己终止受管范围


def test_output_beyond_the_limit_is_capped_and_marked():
    """输出有上限：超出即截断、结果里标明，且两个流各算各的。"""
    limit = 16
    seam = _FakeSeam(stdout=b"x" * (limit + 5), stderr=b"short")
    runtime, _ = _harness(seam, limit=limit)

    value = runtime.run({"id": "c1", "name": "run_command",
                         "args": {"argv": ["flood"]}})["value"]

    assert value["stdout"] == "x" * limit + truncated_note(limit)
    assert value["stdout_truncated"] is True
    assert value["stderr"] == "short"                     # 未超限的流不动它
    assert value["stderr_truncated"] is False


def test_output_exactly_at_the_limit_is_not_marked():
    """边界：恰好等于上限不算截断（只有**超出**才标记）。"""
    seam = _FakeSeam(stdout=b"x" * 16)
    runtime, _ = _harness(seam, limit=16)

    value = runtime.run({"id": "c1", "name": "run_command",
                         "args": {"argv": ["exact"]}})["value"]

    assert value["stdout"] == "x" * 16
    assert value["stdout_truncated"] is False


def test_a_command_string_or_empty_argv_is_rejected():
    """只收 argv 列表：shell 字符串与空列表都走结构化 `failed` 结局（工具体不执行）。"""
    seam = _FakeSeam()
    runtime, _ = _harness(seam)

    for argv in ("echo hi", [], [1, 2]):
        result = runtime.run({"id": "c1", "name": "run_command", "args": {"argv": argv}})

        assert result["status"] == FAILED, argv
        assert "argv 列表" in result["error"], argv

    assert seam.calls == []                               # 一次都没走到受管范围
