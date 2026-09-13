"""包内测试：命令执行工具（`shell.provider`）。

只依赖本能力与更下层的契约：用实现 `ProcessSeam` / `SandboxSeam` 契约的**测试替身**记录
`spawn` 与 `wrap` 收到了什么、并回放输出，所以这里不跑真实子进程（真实子进程的验收在
`tests/test_shell.py` / `tests/test_sandbox.py`——那里才允许跨到 `providers.*`）。

覆盖：调用意图（argv / cwd）与策略原样交给沙箱、沙箱给的 argv 与环境才是真正被执行的、
`cwd` 转交、结果形状与退出码、输出上限与截断标记、命令字符串被拒绝、
**沙箱缺失或拒绝时 fail-closed（一次都没走到受管范围）**、工具不自己终止受管范围。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Sequence

from capabilities.shell.definition import RUN_COMMAND_TOOL, truncated_note
from capabilities.shell.provider import ShellTool
from miniharness.core import Context
from miniharness.process.contract import DEFAULT_GRACE_MS, ManagedRange, ProcessSeam
from miniharness.sandbox.contract import (
    CommandIntent,
    IntegrityRequirements,
    SandboxedCommand,
    SandboxPolicy,
    SandboxSeam,
    SandboxUnavailableError,
)
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
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": env, "stdin": stdin})
        for stream, payload in ((stdout, self.stdout), (stderr, self.stderr)):
            if stream is not None:
                stream.write(payload)
                stream.flush()
        self.range_ = _FakeRange(self.code)
        return self.range_


class _FakeSandbox(SandboxSeam):
    """沙箱替身：记下收到的调用意图与策略，回放一组固定的完整性要求。"""

    def __init__(self, env: dict[str, str] | None = None,
                 error: str | None = None) -> None:
        self.env = {"FAKE_SANDBOX": "1"} if env is None else env
        self.error = error
        self.calls: list[tuple[CommandIntent, SandboxPolicy]] = []

    def wrap(self, intent: CommandIntent, policy: SandboxPolicy) -> SandboxedCommand:
        self.calls.append((intent, policy))
        if self.error is not None:
            raise SandboxUnavailableError(self.error)
        return SandboxedCommand(
            argv=tuple(intent.argv),
            requirements=IntegrityRequirements(env=self.env, cwd=intent.cwd))


class _RewritingSandbox(_FakeSandbox):
    """沙箱替身：产物与调用意图**不同**（走的是沙箱给的 argv / env / cwd）。"""

    def wrap(self, intent: CommandIntent, policy: SandboxPolicy) -> SandboxedCommand:
        self.calls.append((intent, policy))
        return SandboxedCommand(
            argv=("resolved/program", *intent.argv[1:]),
            requirements=IntegrityRequirements(env={"ONLY": "this"}, cwd="/pinned"))


#: 缺省沙箱：装一个替身（正常路径）；显式传 `sandbox=None` 即**不装载沙箱**（失败注入）。
_DEFAULT_SANDBOX = object()


def _harness(seam: _FakeSeam, sandbox: SandboxSeam | None = _DEFAULT_SANDBOX,
             **shell_kwargs: Any) -> tuple[ToolRuntime, ShellTool, _FakeSandbox | None]:
    """最小装配：工具流水线 + 一个会话 + 沙箱 seam + 受管范围 seam + `run_command`。"""
    ctx = Context()
    ctx.provide("session", Session())
    runtime = ToolRuntime()
    ctx.load(runtime)
    ctx.load(seam)
    sandbox = _FakeSandbox() if sandbox is _DEFAULT_SANDBOX else sandbox
    if sandbox is not None:
        ctx.load(sandbox)
    tool = ShellTool(**shell_kwargs)
    ctx.load(tool)
    runtime.register(ToolDefinition(
        "run_command", RUN_COMMAND_TOOL["function"]["description"],
        RUN_COMMAND_TOOL["function"]["parameters"], lambda args: tool.run_command(**args)))
    return runtime, tool, sandbox


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


def test_argv_and_cwd_reach_the_sandbox_then_the_managed_range_verbatim():
    """调用意图逐项原样交给沙箱（工具不拼命令、不经 shell）；被执行的 argv / env / cwd
    全部来自沙箱的产物（完整性要求），而不是调用方自己的环境。"""
    seam = _FakeSeam(stdout=b"hello\n", code=7)
    runtime, _, sandbox = _harness(seam)

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
    assert sandbox.calls == [                             # 调用意图 + 默认策略（最严）交给沙箱
        (CommandIntent(("git", "commit", "-m", "a; b c"), "/somewhere/else"), SandboxPolicy())]
    assert seam.calls == [{"argv": ["git", "commit", "-m", "a; b c"],
                           "cwd": "/somewhere/else",
                           "env": {"FAKE_SANDBOX": "1"},      # 执行用的是沙箱给的环境
                           "stdin": subprocess.DEVNULL}]  # 不把 agent 的 stdin 交给命令
    assert seam.range_.terminated == 0                    # 工具不自己终止受管范围


def test_the_callers_policy_reaches_the_sandbox_and_its_product_is_what_runs():
    """策略由调用方（装配层）给：工具原样交给沙箱；被执行的 argv / env / cwd 以沙箱产物为准。"""
    seam = _FakeSeam()
    policy = SandboxPolicy(env_allowlist=("PATH",))
    sandbox = _RewritingSandbox()
    runtime, _, _ = _harness(seam, sandbox=sandbox, policy=policy)

    result = runtime.run({"id": "c1", "name": "run_command",
                          "args": {"argv": ["prog", "arg"]}})

    assert result["status"] == OK
    assert sandbox.calls == [(CommandIntent(("prog", "arg"), None), policy)]
    assert seam.calls == [{"argv": ["resolved/program", "arg"],   # 执行的是沙箱的 argv
                           "cwd": "/pinned",                       # 以及沙箱给的工作目录
                           "env": {"ONLY": "this"},                # 以及沙箱给的环境
                           "stdin": subprocess.DEVNULL}]


def test_a_missing_sandbox_seam_fails_closed_without_starting_anything():
    """fail-closed：沙箱没装时工具走结构化 `failed`，受管范围一次都没被碰（更别说执行）。"""
    seam = _FakeSeam()
    runtime, _, sandbox = _harness(seam, sandbox=None)

    result = runtime.run({"id": "c1", "name": "run_command",
                          "args": {"argv": ["anything", "at", "all"]}})

    assert result["status"] == FAILED
    assert "沙箱不可用" in result["error"]
    assert seam.calls == []                            # 没有回退到无约束执行
    assert sandbox is None

    # 对照（非空洞）：同一个工具，装上沙箱后同一条命令就走通了
    working = _harness(_FakeSeam())[0]
    assert working.run({"id": "c2", "name": "run_command",
                        "args": {"argv": ["anything", "at", "all"]}})["status"] == OK


def test_a_sandbox_that_refuses_the_call_fails_closed_too():
    """沙箱拒绝服务（策略超出后端能力、解析不出可执行文件…）同样失败，不降级执行。"""
    seam = _FakeSeam()
    refusing = _FakeSandbox(error="沙箱不可用：本后端不能强制断网")
    runtime, _, _ = _harness(seam, sandbox=refusing)

    result = runtime.run({"id": "c1", "name": "run_command",
                          "args": {"argv": ["anything"]}})

    assert result["status"] == FAILED
    assert "不能强制断网" in result["error"]
    assert len(refusing.calls) == 1                    # 确实问过沙箱（不是没问就拒）
    assert seam.calls == []


def test_output_beyond_the_limit_is_capped_and_marked():
    """输出有上限：超出即截断、结果里标明，且两个流各算各的。"""
    limit = 16
    seam = _FakeSeam(stdout=b"x" * (limit + 5), stderr=b"short")
    runtime, _, _ = _harness(seam, limit=limit)

    value = runtime.run({"id": "c1", "name": "run_command",
                         "args": {"argv": ["flood"]}})["value"]

    assert value["stdout"] == "x" * limit + truncated_note(limit)
    assert value["stdout_truncated"] is True
    assert value["stderr"] == "short"                     # 未超限的流不动它
    assert value["stderr_truncated"] is False


def test_output_exactly_at_the_limit_is_not_marked():
    """边界：恰好等于上限不算截断（只有**超出**才标记）。"""
    seam = _FakeSeam(stdout=b"x" * 16)
    runtime, _, _ = _harness(seam, limit=16)

    value = runtime.run({"id": "c1", "name": "run_command",
                         "args": {"argv": ["exact"]}})["value"]

    assert value["stdout"] == "x" * 16
    assert value["stdout_truncated"] is False


def test_a_command_string_or_empty_argv_is_rejected():
    """只收 argv 列表：shell 字符串与空列表都走结构化 `failed` 结局（工具体不执行）。"""
    seam = _FakeSeam()
    runtime, _, sandbox = _harness(seam)

    for argv in ("echo hi", [], [1, 2]):
        result = runtime.run({"id": "c1", "name": "run_command", "args": {"argv": argv}})

        assert result["status"] == FAILED, argv
        assert "argv 列表" in result["error"], argv

    assert seam.calls == []                               # 一次都没走到受管范围
    assert sandbox.calls == []                            # 也没惊动沙箱：校验发生在最前面
