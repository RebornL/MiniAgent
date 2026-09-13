"""包内测试：超时护栏的实现（`timeout.provider`）。

超时必须以稳定的 `timed_out` 结局返回、与成功和其它中止结局可区分；且卡死的工具体不得
拖住进程退出（工具体跑在 daemon 线程里，子进程验证）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from capabilities.timeout.provider import ToolTimeoutPlugin
from miniharness.tools.contract import OK, TIMED_OUT, ToolDefinition
from miniharness.tools.runtime.test_pipeline import _pipeline


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

    # 未超时的调用不受影响，仍是 ok
    _, ok_runtime = _pipeline(ToolTimeoutPlugin(default_ms=1000),
                              ToolDefinition("fast", "", {}, lambda a: "快", timeout_ms=1000))
    ok = ok_runtime.run({"id": "c2", "name": "fast", "args": {}})
    assert ok["status"] == OK and ok["content"] == "快"


def test_timeout_does_not_block_process_exit():
    """超时后必须立即返回，且卡死的工具体不得拖住进程退出。

    工具体跑在无法被杀死的线程里，超时只能「停止等待」、不能「取消执行」。但 worker
    必须是 daemon 线程：legacy 的 `ThreadPoolExecutor` 会在解释器退出时 join 它的
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
