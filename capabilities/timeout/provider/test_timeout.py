"""包内测试：超时护栏的实现（`timeout.provider`）。

超时与取消走**同一条**终止路径——终止本次调用登记的全部受管范围，返回前独立确认范围真的空了；
终止没达成目的时不得报成中止结局。这里只装本包 + 更下层的契约，受管范围用替身验接线；真实子
进程的断言（「进程真的没了」由操作系统证明）在 `tests/test_timeout.py`（跨包集成）。

另外守住：卡死的工具体不得拖住进程退出（工具体跑在 daemon 线程里，子进程验证）。
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from capabilities.timeout.provider import ToolTimeoutPlugin
from miniharness.core import Context
from miniharness.process.contract import DEFAULT_GRACE_MS, ManagedRange, ProcessSeam
from miniharness.tools.contract import (
    CANCELLED,
    FAILED,
    OK,
    TIMED_OUT,
    ToolDefinition,
)
from miniharness.tools.runtime import ToolRuntime
from miniharness.tools.runtime.test_pipeline import _pipeline


class _BlockingRange(ManagedRange):
    """受管范围替身：一直「在跑」，直到有人终止它（终止之后才给出退出码）。

    `survives=True` 扮演「终止没达成目的」的后端（强杀之后范围仍在跑）。
    """

    def __init__(self, pid: int = 4242, survives: bool = False) -> None:
        self._pid = pid
        self._survives = survives
        self._exited = threading.Event()
        self.terminations = 0

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return None if self._survives or not self._exited.is_set() else -1

    def wait_for_exit(self, timeout_ms: int | None = None) -> int | None:
        self._exited.wait(None if timeout_ms is None else timeout_ms / 1000)
        return self.poll()

    def terminate(self, grace_ms: int = DEFAULT_GRACE_MS) -> None:
        self.terminations += 1
        if not self._survives:
            self._exited.set()

    def release(self) -> None:
        self.terminate()

    def finish(self) -> None:
        """测试收尾：放行还卡在 `wait_for_exit` 上的工具体线程。"""
        self._survives = False
        self._exited.set()


class _RangeSeam(ProcessSeam):
    """受管范围 seam 替身：交出预置的范围（`spawn` 只被记一笔）。"""

    def __init__(self, range_: ManagedRange) -> None:
        self.range_ = range_
        self.calls: list[list[str]] = []

    def spawn(self, argv: Any, **kwargs: Any) -> ManagedRange:
        self.calls.append(list(argv))
        return self.range_


class _QueueSeam(ProcessSeam):
    """按顺序交出预置范围的 seam 替身：用来验「中止先到、进程后起」这条时序。"""

    def __init__(self, *ranges: ManagedRange) -> None:
        self._queue = list(ranges)
        self.calls: list[list[str]] = []

    def spawn(self, argv: Any, **kwargs: Any) -> ManagedRange:
        self.calls.append(list(argv))
        return self._queue.pop(0)


def _runtime(plugin: ToolTimeoutPlugin, range_: ManagedRange, timeout_ms: int = 60_000
             ) -> tuple[Context, ToolRuntime, list[str], _RangeSeam]:
    """最小流水线：一个「起了受管范围就一直等它退出」的工具（与 `run_command` 同形）。

    受管范围 seam 在**调用时刻**经 `ctx.get("process")` 取——这正是策略层能接线的那个口。
    """
    ctx, runtime = _pipeline(None)
    ctx.load(plugin)
    observed: list[str] = []

    def body(args: dict) -> str:
        code = ctx.get("process").spawn(["slow"]).wait_for_exit()
        observed.append("工具体已静止")
        return f"退出码 {code}"

    runtime.register(ToolDefinition("slow", "", {}, body, timeout_ms=timeout_ms))
    seam = _RangeSeam(range_)
    ctx.provide("process", seam)
    return ctx, runtime, observed, seam


def _run_in_thread(runtime: ToolRuntime, results: list[dict]) -> threading.Thread:
    worker = threading.Thread(
        target=lambda: results.append(runtime.run({"id": "c1", "name": "slow", "args": {}})),
        daemon=True)
    worker.start()
    return worker


def _wait_until(predicate, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "条件迟迟不成立"
        time.sleep(0.01)


def test_timeout_plugin_reports_timed_out_outcome():
    """用户裁定的 Story 15 例外：超时不报 ok；T5 起也不再混同于 `failed`，而是 `timed_out`。"""
    def slow(args: dict) -> str:
        time.sleep(0.2)
        return "慢"

    _, runtime = _pipeline(ToolTimeoutPlugin(default_ms=1000),
                           ToolDefinition("slow", "", {}, slow, timeout_ms=50))
    result = runtime.run({"id": "c1", "name": "slow", "args": {}})

    assert result["status"] == TIMED_OUT
    assert "超时" in result["error"] and result["content"] == result["error"]


def test_a_finished_tool_does_not_wait_for_the_deadline():
    """工具体跑完就立刻返回，不等满时限——装配层的默认时限是 30 秒，等满它等于把工具卡死。"""
    def fast(args: dict) -> str:
        time.sleep(0.1)              # 让等待者先睡下：跑完的那一刻它必须被叫醒
        return "快"

    started = time.monotonic()
    _, runtime = _pipeline(ToolTimeoutPlugin(),          # 默认 30 秒
                           ToolDefinition("fast", "", {}, fast))
    result = runtime.run({"id": "c1", "name": "fast", "args": {}})

    assert result["status"] == OK and result["content"] == "快"
    assert time.monotonic() - started < 5


def test_timeout_terminates_the_range_the_call_spawned():
    """超时不只是「停止等待」：本次调用起的受管范围被请求终止，且返回前已确认它退出了。"""
    range_ = _BlockingRange()
    _, runtime, observed, _ = _runtime(ToolTimeoutPlugin(), range_, timeout_ms=80)

    result = runtime.run({"id": "c1", "name": "slow", "args": {}})

    assert result["status"] == TIMED_OUT and "超时" in result["error"]
    assert range_.terminations >= 1 and range_.poll() is not None
    assert observed == ["工具体已静止"]        # 工具体先静止，结局才替换它的结果


def test_a_range_that_survives_termination_fails_instead_of_reporting_a_timeout():
    """终止没达成目的必须让调用方看见：范围还活着时不许报成 `timed_out`（空洞性反例）。"""
    range_ = _BlockingRange(survives=True)
    _, runtime, _, _ = _runtime(ToolTimeoutPlugin(), range_, timeout_ms=80)
    try:
        result = runtime.run({"id": "c1", "name": "slow", "args": {}})

        assert result["status"] == FAILED
        assert result["status"] != TIMED_OUT
        assert "仍在跑" in result["error"] and "TerminationError" in result["error"]
    finally:
        range_.finish()


def test_cancel_terminates_the_running_call_and_reports_cancelled():
    """取消与超时同一条路径：`cancel()`（`ctx.get("abort")`）终止范围并给出 `cancelled`。"""
    range_ = _BlockingRange(pid=5150)
    plugin = ToolTimeoutPlugin(default_ms=60_000)     # 时限很长：结局只能来自取消
    ctx, runtime, observed, seam = _runtime(plugin, range_)
    results: list[dict] = []
    worker = _run_in_thread(runtime, results)

    _wait_until(lambda: bool(seam.calls) and range_.poll() is None)

    assert ctx.get("abort") is plugin
    assert plugin.cancel("用户中断") is True

    worker.join(timeout=10)
    assert not worker.is_alive()
    assert results[0]["status"] == CANCELLED
    assert "被取消" in results[0]["error"] and "用户中断" in results[0]["error"]
    assert range_.terminations >= 1 and range_.poll() is not None
    assert observed == ["工具体已静止"]
    assert plugin.cancel() is False                   # 没有在跑的调用：幂等 no-op


def test_a_range_that_appears_after_the_abort_is_terminated_on_the_spot():
    """边界态：中止请求先到、工具体后起进程——后起的范围**当场**被终止，不留孤儿。

    工具体是在时限之后才起第二个进程的：它不在终止快照里，唯一的活路是代理的「迟到者」分支。
    """
    first, late = _BlockingRange(pid=1), _BlockingRange(pid=2)
    plugin = ToolTimeoutPlugin(grace_ms=2000)
    ctx, runtime = _pipeline(None)
    ctx.load(plugin)

    def body(args: dict) -> str:
        seam = ctx.get("process")                  # 调用时刻取的代理：迟到 spawn 也过它
        first_code = seam.spawn(["first"]).wait_for_exit()
        time.sleep(0.2)                            # 中止已经发生，工具体还在跑
        late_code = seam.spawn(["late"]).wait_for_exit()
        return f"{first_code}/{late_code}"

    runtime.register(ToolDefinition("slow", "", {}, body, timeout_ms=150))
    seam = _QueueSeam(first, late)
    ctx.provide("process", seam)

    result = runtime.run({"id": "c1", "name": "slow", "args": {}})

    assert result["status"] == TIMED_OUT
    assert seam.calls == [["first"], ["late"]]
    assert first.terminations >= 1 and first.poll() is not None
    # 第二个范围从不进终止快照，只能是「迟到者当场终止」这一条路
    assert late.terminations == 1 and late.poll() is not None


def test_timeout_does_not_block_process_exit():
    """超时后必须立即返回，且卡死的工具体不得拖住进程退出。

    没有受管范围可终止的工具体（纯 Python）仍是旧边界：超时只能「停止等待」、不能杀死线程。
    但 worker 必须是 daemon 线程：legacy 的 `ThreadPoolExecutor` 会在解释器退出时 join 它的
    worker，于是一个卡死的工具能把整个进程挂到它跑完。用子进程验证这两点。
    """
    script = (
        "import time\n"
        "from miniharness.core import Context\n"
        "from miniharness.session import Session\n"
        "from miniharness.tools.contract import ToolDefinition\n"
        "from miniharness.tools.runtime import ToolRuntime\n"
        "from capabilities.timeout.provider import ToolTimeoutPlugin\n"
        "ctx = Context()\n"
        'ctx.provide("session", Session())\n'
        "rt = ToolRuntime()\n"
        "ctx.load(rt)\n"
        "ctx.load(ToolTimeoutPlugin(default_ms=50))\n"
        'rt.register(ToolDefinition("hang", "", {}, lambda a: time.sleep(30)))\n'
        "t0 = time.time()\n"
        'r = rt.run({"id": "c1", "name": "hang", "args": {}})\n'
        'assert r["status"] == "timed_out", r\n'
        'assert time.time() - t0 < 5, "超时后应立即返回，不等工具体跑完"\n'
        'print("timeout-ok")\n'
    )
    proc = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[3],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "timeout-ok"
