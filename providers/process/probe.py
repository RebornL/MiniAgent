"""进程存活探针 —— 只服务测试的**独立确认**（`providers.process` 边界）。

断言「终止后进程真的没了」时，不采信被测实现自己的返回值（`ManagedRange.poll()`），而是直接
问操作系统进程还在不在。这份探针被包内测试（`providers/process/test_managed_range.py`）与跨包
集成测试（`tests/test_timeout.py`）共用，**整个仓库只有这一份实现**：抄第二份等于毁掉它的
独立性——一处漂移，同一个进程会在两处得出相反结论，依赖它的断言静默变成假证据。

放在非 `test_*.py` 模块里（`docs/packaging.md` §6）：探针问的是进程边界，不属于任何一族的
测试文件；两处测试都向下 import 同一个实现。
"""
from __future__ import annotations

import ctypes
import os
import time

#: Windows 上「还活着」的退出码（`STILL_ACTIVE`）与「打不开这个 pid」的错误码。
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87


def _alive(pid: int) -> bool:
    """进程是否还在跑——**独立于被测实现的返回值**，直接问操作系统。

    Windows 上「pid 存在」不等于「还活着」：已终止的进程只要还有句柄就被占着号，所以要问
    退出码；OpenProcess 打不开、错误码是「参数无效」才是真的不存在。
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
            return False
        raise OSError(f"OpenProcess({pid}) 失败：错误码 {ctypes.get_last_error()}")
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) \
            and code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _assert_gone(*pids: int, timeout_s: float = 5.0) -> None:
    """这些进程必须都不再存在（给终止一点收尾时间）。"""
    deadline = time.monotonic() + timeout_s
    while any(_alive(pid) for pid in pids):
        if time.monotonic() >= deadline:
            raise AssertionError(f"这些进程仍然存在: {[pid for pid in pids if _alive(pid)]}")
        time.sleep(0.05)
