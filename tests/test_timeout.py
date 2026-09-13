"""跨包集成：超时 / 取消 → 终止受管范围（`capabilities.timeout` + `providers.process` + shell）。

用**真实子进程**验证这条接线（`capabilities/timeout/provider`）：超时与取消走同一条终止路径并
给出结构化结局；已启动的工具体跑到静止才让结局替换它的结果；终止后不残留进程——**独立确认**：
用操作系统问进程是否还在跑（`providers.process.probe._alive`，只此一份实现），不采信被测实现
自己的返回值；下一次调用不受影响。

终止手段随平台，宽限档不是普适承诺：**Windows 后端没有升级档**（Job Object 一次强杀到底，
`grace_ms` 在那里只是等工具体静止的上限）；POSIX 后端才有「先 SIGTERM、宽限期满再 SIGKILL」，
它的真实触发用例在 `providers/process/test_managed_range.py`（本机 skip）。

两处边界态单独收：组长已自行退出而后代仍在跑（Windows 上 `taskkill /T` 靠活着的组长遍历进程
树，救不回后代，必须靠 Job Object）；以及命令自己正好在终止与正常退出之间退出。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from app.assembly import SHELL_ENV_ALLOWLIST
from capabilities.timeout.provider import ToolTimeoutPlugin
from miniharness.core import Context
from miniharness.sandbox.contract import SandboxPolicy
from miniharness.tools.contract import CANCELLED, OK, TIMED_OUT, ToolDefinition
from miniharness.tools.runtime import ToolRuntime
from providers.process import SubprocessRange, SubprocessSeam
from providers.process.probe import _alive, _assert_gone
from tests.support import _shell_harness

#: 与装配层同一份沙箱策略（`app.assembly.SHELL_ENV_ALLOWLIST`）：这里验的是装配后的行为。
_POLICY = SandboxPolicy(env_allowlist=SHELL_ENV_ALLOWLIST)

#: 子进程树剧本：组长再派生一个后代、把后代的 pid 写进 argv[1]，然后两个一起长睡。
_TREE_SCRIPT = """\
import subprocess, sys, time
from pathlib import Path
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path(sys.argv[1]).write_text(str(grandchild.pid))
time.sleep(30)
"""

#: 孤儿剧本：组长派生一个长睡后代、写下它的 pid，然后**自己立刻退出**。
_ORPHAN_SCRIPT = """\
import subprocess, sys
from pathlib import Path
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path(sys.argv[1]).write_text(str(grandchild.pid))
"""


class _RecordingSeam(SubprocessSeam):
    """记下每一次 `spawn` 产出的受管范围：测试据此做独立确认。"""

    def __init__(self) -> None:
        self.ranges: list[SubprocessRange] = []

    def spawn(self, argv, **kwargs) -> SubprocessRange:
        range_ = super().spawn(argv, **kwargs)
        self.ranges.append(range_)
        return range_


def _harness(plugin: ToolTimeoutPlugin) -> tuple[Context, ToolRuntime, _RecordingSeam]:
    """最小装配：`run_command` + 沙箱 + 受管范围 + 超时护栏（受管范围后端被记录）。"""
    seam = _RecordingSeam()
    ctx, runtime = _shell_harness(seam=seam, plugin=plugin, policy=_POLICY)
    return ctx, runtime, seam


def _run_call(runtime: ToolRuntime, results: list[dict], argv: list[str]) -> threading.Thread:
    """在后台线程里跑一次 `run_command`：主线程借此观察「命令还在跑」的现场。"""
    worker = threading.Thread(
        target=lambda: results.append(runtime.run(
            {"id": "c1", "name": "run_command", "args": {"argv": argv}})),
        daemon=True)
    worker.start()
    return worker


def _wait_until(predicate, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "条件迟迟不成立"
        time.sleep(0.02)


def _write(tmp_path: Path, name: str, script: str) -> Path:
    path = tmp_path / name
    path.write_text(script, encoding="utf-8")
    return path


def test_a_timed_out_command_is_terminated_for_real(tmp_path):
    """AC1 + AC4：超时 → 终止整棵进程树 → 结构化 `timed_out`；返回时组长与后代都不在了，
    且下一次调用照常。"""
    plugin = ToolTimeoutPlugin(default_ms=1500, grace_ms=500)
    _, runtime, seam = _harness(plugin)
    script, pidfile = _write(tmp_path, "tree.py", _TREE_SCRIPT), tmp_path / "child.pid"
    results: list[dict] = []

    started = time.monotonic()
    worker = _run_call(runtime, results, [sys.executable, str(script), str(pidfile)])
    _wait_until(lambda: bool(seam.ranges))
    (range_,) = seam.ranges
    assert range_.poll() is None                       # 空洞性：命令确实在跑，不是白捡的
    worker.join(timeout=20)
    elapsed = time.monotonic() - started

    assert not worker.is_alive()
    result = results[0]
    assert result["status"] == TIMED_OUT and "超时" in result["error"]
    assert "value" not in result                       # 中止结局与成功同构：值通道不掺水
    assert elapsed < 5, "超时后不该等命令跑完（剧本要睡 30 秒）"

    # 独立确认：问操作系统组长与后代是否还在跑
    assert range_.poll() is not None                    # 范围已退出并给出退出码
    _assert_gone(range_.pid)
    _wait_until(pidfile.exists, timeout_s=10)
    _assert_gone(int(pidfile.read_text(encoding="utf-8")))

    after = runtime.run({"id": "c2", "name": "run_command",
                         "args": {"argv": [sys.executable, "-c", "print('after')"]}})
    assert after["status"] == OK and after["value"]["exit_code"] == 0
    assert after["value"]["stdout"].strip() == "after"


def test_termination_reaches_a_descendant_that_outlives_its_leader(tmp_path):
    """边界态：组长已自行退出、后代仍在跑——范围没退（工具还卡着等它），终止必须够得着后代。

    这正是范围本位与组长本位的分界：Windows 上 `taskkill /T /F` 靠**活着的**组长遍历进程树，
    组长已经退出时它什么都够不着，事后也救不回后代。
    """
    plugin = ToolTimeoutPlugin(default_ms=1500, grace_ms=500)
    _, runtime, seam = _harness(plugin)
    script, pidfile = _write(tmp_path, "orphan.py", _ORPHAN_SCRIPT), tmp_path / "child.pid"
    results: list[dict] = []

    worker = _run_call(runtime, results, [sys.executable, str(script), str(pidfile)])
    _wait_until(lambda: pidfile.exists() and bool(seam.ranges))
    (range_,) = seam.ranges
    grandchild = int(pidfile.read_text(encoding="utf-8"))

    assert range_.poll() is None                       # 组长已退，范围没退（后代在跑）
    assert _alive(grandchild)                          # 现场：后代确实在长睡
    worker.join(timeout=20)

    assert not worker.is_alive()
    assert results[0]["status"] == TIMED_OUT
    _assert_gone(range_.pid, grandchild)               # 组长与后代都不留


def test_the_tool_body_reaches_quiescence_before_the_timeout_replaces_its_result():
    """AC3：已启动的工具体跑到静止（实体被终止、它自己的等待返回）后，超时结局才替换它的结果。"""
    plugin = ToolTimeoutPlugin(grace_ms=2000)
    ctx, runtime, seam = _harness(plugin)
    observed: list[str] = []

    def body(args: dict) -> dict:
        range_ = ctx.get("process").spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        code = range_.wait_for_exit()
        observed.append(f"工具体静止（退出码 {code}）")
        return {"exit_code": code}

    runtime.register(ToolDefinition("spawn_and_wait", "", {}, body, timeout_ms=300))
    result = runtime.run({"id": "c1", "name": "spawn_and_wait", "args": {}})

    assert result["status"] == TIMED_OUT and "超时" in result["error"]
    assert observed and observed[0].startswith("工具体静止")   # 结局没有抢在工具体前面
    assert "value" not in result
    _assert_gone(seam.ranges[0].pid)


def test_a_cancelled_command_is_terminated_and_reported_as_cancelled():
    """AC2：取消走与超时同一条路径——终止受管范围，返回 `cancelled`，一个进程都不留。"""
    plugin = ToolTimeoutPlugin(default_ms=60_000)      # 时限很长：结局只能来自取消
    ctx, runtime, seam = _harness(plugin)
    results: list[dict] = []

    worker = _run_call(runtime, results, [sys.executable, "-c", "import time; time.sleep(30)"])
    _wait_until(lambda: bool(seam.ranges) and seam.ranges[0].poll() is None)

    started = time.monotonic()
    assert ctx.get("abort").cancel("用户中断") is True

    worker.join(timeout=20)
    assert not worker.is_alive()
    result = results[0]
    assert result["status"] == CANCELLED
    assert "被取消" in result["error"] and "用户中断" in result["error"]
    assert time.monotonic() - started < 5, "取消后不该等命令跑完（剧本要睡 30 秒）"
    _assert_gone(seam.ranges[0].pid)


def test_terminating_a_range_that_already_exited_is_a_noop():
    """边界态：时限到的时候实体已经**自己正常退出**——终止是 no-op，不改它的结局。

    此时工具还卡在别处（工具体在 Python 里磨蹭），所以结局依旧是超时；但它必须等到工具体
    静止才替换结果，而且不能把一个正常退出的范围改写成「被强杀」。
    """
    plugin = ToolTimeoutPlugin(grace_ms=2000)
    ctx, runtime, seam = _harness(plugin)
    observed: list[str] = []

    def body(args: dict) -> dict:
        range_ = ctx.get("process").spawn([sys.executable, "-c", "pass"])
        code = range_.wait_for_exit()                  # 立刻正常退出
        time.sleep(0.4)                                # 实体已退，工具体还没静止
        observed.append("工具体静止")
        return {"exit_code": code}

    runtime.register(ToolDefinition("spawn_then_linger", "", {}, body, timeout_ms=150))
    result = runtime.run({"id": "c1", "name": "spawn_then_linger", "args": {}})

    assert result["status"] == TIMED_OUT
    assert observed == ["工具体静止"]                   # 结局替换之前工具体已静止
    assert seam.ranges[0].poll() == 0                   # 正常退出的范围没有被改写
    _assert_gone(seam.ranges[0].pid)


def test_a_normal_exit_racing_the_deadline_stays_consistent():
    """竞态：命令自己在时限附近退出——两种走向都必须自洽，且都不留活口。

    时限与退出只差毫秒级：结局是 `ok` 还是 `timed_out` 取决于谁先到（这是**竞态的正确语义**，
    不该被某一次运行的运气写死），但无论哪种走向，结局都是有价值的、不挂死、进程都不留。
    """
    plugin = ToolTimeoutPlugin(default_ms=250, grace_ms=500)
    _, runtime, seam = _harness(plugin)

    for _ in range(3):
        started = time.monotonic()
        result = runtime.run({"id": "c1", "name": "run_command",
                              "args": {"argv": [sys.executable, "-c",
                                                "import time; time.sleep(0.2)"]}})
        elapsed = time.monotonic() - started

        assert result["status"] in (OK, TIMED_OUT), result
        if result["status"] == TIMED_OUT:
            assert "超时" in result["error"] and "value" not in result
        else:
            assert result["value"]["exit_code"] == 0
        assert elapsed < 3, "无论谁先到都不该挂死"
        _assert_gone(seam.ranges[-1].pid)
