"""providers.process —— 受管范围的平台后端（Provider）。

`SubprocessSeam` 用 `subprocess.Popen` 起一个受管范围，`SubprocessRange` 负责它的生死：

- **POSIX**：组长 `start_new_session=True` 自立会话，于是自成进程组（pgid == 组长 pid）；
  终止是**信号组升级**——先 `killpg(SIGTERM)`，宽限期满仍不退再 `killpg(SIGKILL)`。
  「范围是否还在跑」以**组内是否还有成员**为准：组长先退出、后代还在跑时，范围没退。
- **Windows**：范围由 **Job Object** 承载——`CreateJobObject` +
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`；组长以 `CREATE_SUSPENDED` 启动，**先入 job 再恢复执行**，
  所以它派生的一切后代从出生起就在册（job 未开 breakaway，后代无法脱离）。
  终止是 `TerminateJobObject`（Windows 对无窗口的控制台子进程没有可用的「温和档」，
  所以不升级，一次强杀到底——与规格「POSIX 升级、Windows 终止整棵树」一致）；
  「范围是否还在跑」以 job 的**活跃进程数**为准（`QueryInformationJobObject`）。
  `release()` 关闭 job 句柄，`KILL_ON_JOB_CLOSE` 是最后的兜底：本进程意外消失也不留孤儿。

平台机制不可得时（`CreateJobObject` / `SetInformationJobObject` / `AssignProcessToJobObject`
失败），后端**显式抛 `RangeUnavailableError`**，不静默退化成「只跟踪组长」——那正是会留孤儿
的那种退化。
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from typing import Any, Sequence

from miniharness.process.contract import (
    DEFAULT_GRACE_MS,
    ManagedRange,
    ProcessSeam,
    RangeUnavailableError,
    TerminationError,
)

__all__ = ["SubprocessRange", "SubprocessSeam"]

#: 轮询组长 / 进程组 / job 的间隔（秒）：够密以尽快返回，够稀以不烧 CPU。
_POLL_S = 0.02

#: 强杀之后的收尾上限（毫秒）：内核保证会死，这里只是等收尸，不该等太久。
_REAP_MS = 5000

_IS_WINDOWS = os.name == "nt"


# ═══════════════════════════════════════════════════════════════
# Windows：Job Object（受管范围的真身）
# ═══════════════════════════════════════════════════════════════
if _IS_WINDOWS:
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _RESUME_FAILED = 0xFFFFFFFF

    #: `CREATE_SUSPENDED`：Python 的 `subprocess` 没有把它导出成常量，自己写。
    _CREATE_SUSPENDED = 0x00000004

    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1

    class _ThreadEntry32(ctypes.Structure):
        """`THREADENTRY32`：只需 pid 与 tid 两个字段，其余照原型保留以对齐布局。"""

        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ThreadID", ctypes.c_uint32),
            ("th32OwnerProcessID", ctypes.c_uint32),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", ctypes.c_uint32),
            ("TotalProcesses", ctypes.c_uint32),
            ("ActiveProcesses", ctypes.c_uint32),
            ("TotalTerminatedProcesses", ctypes.c_uint32),
        ]

    _KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _KERNEL32.CreateJobObjectW.restype = ctypes.c_void_p
    _KERNEL32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    _KERNEL32.SetInformationJobObject.restype = ctypes.c_int
    _KERNEL32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _KERNEL32.AssignProcessToJobObject.restype = ctypes.c_int
    _KERNEL32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _KERNEL32.TerminateJobObject.restype = ctypes.c_int
    _KERNEL32.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    _KERNEL32.QueryInformationJobObject.restype = ctypes.c_int
    _KERNEL32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    _KERNEL32.OpenProcess.restype = ctypes.c_void_p
    _KERNEL32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _KERNEL32.TerminateProcess.restype = ctypes.c_int
    _KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
    _KERNEL32.CloseHandle.restype = ctypes.c_int
    _KERNEL32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    _KERNEL32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _KERNEL32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    _KERNEL32.Thread32First.restype = ctypes.c_int
    _KERNEL32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    _KERNEL32.Thread32Next.restype = ctypes.c_int
    _KERNEL32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    _KERNEL32.OpenThread.restype = ctypes.c_void_p
    _KERNEL32.ResumeThread.argtypes = [ctypes.c_void_p]
    _KERNEL32.ResumeThread.restype = ctypes.c_uint32

    def _win_error(what: str) -> str:
        return f"{what} 失败（Windows 错误 {ctypes.get_last_error()}）"

    def _resume_suspended(pid: int) -> None:
        """恢复一个 `CREATE_SUSPENDED` 启动的进程：按 pid 找到它的主线程并 ResumeThread。"""
        snapshot = _KERNEL32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            raise RangeUnavailableError(_win_error("CreateToolhelp32Snapshot"))
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(_ThreadEntry32)
            found = bool(_KERNEL32.Thread32First(snapshot, ctypes.byref(entry)))
            while found:
                if entry.th32OwnerProcessID == pid:
                    thread = _KERNEL32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if not thread:
                        raise RangeUnavailableError(_win_error(f"OpenThread({entry.th32ThreadID})"))
                    try:
                        if _KERNEL32.ResumeThread(thread) == _RESUME_FAILED:
                            raise RangeUnavailableError(_win_error(f"ResumeThread({pid})"))
                    finally:
                        _KERNEL32.CloseHandle(thread)
                    return
                found = bool(_KERNEL32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            _KERNEL32.CloseHandle(snapshot)
        raise RangeUnavailableError(f"进程 {pid} 没有可恢复的主线程")

    def _hard_kill(pid: int) -> None:
        """兜底硬杀一个进程：只在 `spawn` 失败、范围还没建立时用，避免泄漏半个范围。"""
        process = _KERNEL32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not process:
            return
        try:
            _KERNEL32.TerminateProcess(process, 1)
        finally:
            _KERNEL32.CloseHandle(process)

    class _WindowsJob:
        """Windows 受管范围的真身：一个 `KILL_ON_JOB_CLOSE` 的 Job Object。"""

        def __init__(self, handle: int) -> None:
            self._handle = handle

        @classmethod
        def create(cls) -> "_WindowsJob":
            """建一个「句柄一关就杀光」的 job：这既是范围，也是本进程崩溃时的兜底。"""
            handle = _KERNEL32.CreateJobObjectW(None, None)
            if not handle:
                raise RangeUnavailableError(_win_error("CreateJobObject"))
            job = cls(handle)
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _KERNEL32.SetInformationJobObject(
                    handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits), ctypes.sizeof(limits)):
                job.close()
                raise RangeUnavailableError(_win_error("SetInformationJobObject"))
            return job

        def assign(self, pid: int) -> None:
            """把组长并入 job；此后它派生的后代自动入册（job 未开 breakaway，脱不出去）。"""
            process = _KERNEL32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not process:
                raise RangeUnavailableError(_win_error(f"OpenProcess({pid})"))
            try:
                if not _KERNEL32.AssignProcessToJobObject(self._handle, process):
                    raise RangeUnavailableError(_win_error(f"AssignProcessToJobObject({pid})"))
            finally:
                _KERNEL32.CloseHandle(process)

        def active_processes(self) -> int:
            """job 里还剩几个活着的进程；job 已关闭即范围已销毁，算 0。"""
            if self._handle is None:
                return 0
            info = _BasicAccountingInformation()
            if not _KERNEL32.QueryInformationJobObject(
                    self._handle, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                    ctypes.byref(info), ctypes.sizeof(info), None):
                raise RangeUnavailableError(_win_error("QueryInformationJobObject"))
            return int(info.ActiveProcesses)

        def terminate(self) -> None:
            if not _KERNEL32.TerminateJobObject(self._handle, 1):
                raise TerminationError(_win_error("TerminateJobObject"))

        def close(self) -> None:
            if self._handle is not None:
                _KERNEL32.CloseHandle(self._handle)
                self._handle = None

    def _join_job(pid: int) -> _WindowsJob:
        """为一个刚启动（挂起中）的组长建立范围：建 job → 入册 → 恢复执行。"""
        job = _WindowsJob.create()
        try:
            job.assign(pid)
            _resume_suspended(pid)
        except BaseException:
            job.close()
            raise
        return job


class SubprocessSeam(ProcessSeam):
    """`subprocess` 后端的进程 seam：每一次 `spawn` 产出一个受管范围。"""

    def spawn(self, argv: Sequence[str], *, cwd: str | None = None,
              env: dict[str, str] | None = None, stdin: Any = None,
              stdout: Any = None, stderr: Any = None) -> SubprocessRange:
        argv = [str(arg) for arg in argv]
        if not argv:
            raise ValueError("受管范围需要一个非空 argv")
        kwargs: dict[str, Any] = {
            "cwd": cwd, "env": env, "stdin": stdin, "stdout": stdout, "stderr": stderr,
        }
        if _IS_WINDOWS:
            # 先挂起：入册之前它一条指令都没跑过，范围因此从一出生就是完整的。
            # 同时脱离本进程的控制台组：本进程收 Ctrl+C 不会顺带打死它，生死由本 seam 管。
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | _CREATE_SUSPENDED)
            popen = subprocess.Popen(argv, **kwargs)
            try:
                job = _join_job(popen.pid)
            except BaseException:
                _hard_kill(popen.pid)       # 还没入册、又没人管 → 不能留它在后台跑
                raise
            return SubprocessRange(popen, job=job)
        # POSIX：组长自立会话 → 自成进程组，终止与等待都以这个组为单位
        kwargs["start_new_session"] = True
        return SubprocessRange(subprocess.Popen(argv, **kwargs))


class SubprocessRange(ManagedRange):
    """受管范围句柄：包住组长进程（Windows 上还包住承载它的 job），把这棵树当成一个单位。"""

    def __init__(self, popen: subprocess.Popen, job: "_WindowsJob | None" = None) -> None:
        self._popen = popen
        self._job = job                    # Windows 的范围真身；POSIX 的范围就是组长的进程组
        # 终止动词可能被超时路径与取消路径同时调用：用可重入锁兜住幂等
        self._lock = threading.RLock()
        self._released = False

    @property
    def pid(self) -> int:
        return self._popen.pid

    def poll(self) -> int | None:
        return self.wait_for_exit(0)

    def wait_for_exit(self, timeout_ms: int | None = None) -> int | None:
        deadline = None if timeout_ms is None else time.monotonic() + max(timeout_ms, 0) / 1000
        while True:
            code = self._popen.poll()       # 组长退出即收尸：僵尸也算组成员，不收尸就等不到空
            if code is not None and self._empty():
                return code
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(_POLL_S)

    def terminate(self, grace_ms: int = DEFAULT_GRACE_MS) -> None:
        with self._lock:
            if self._released or self.poll() is not None:
                return                      # 幂等：范围已空 / 已释放 → no-op
            if self._job is not None:
                self._job.terminate()       # Windows：一次强杀整棵树（job 里没有温和档）
            else:
                self._kill_group_posix(grace_ms)
            if self.wait_for_exit(_REAP_MS) is None:
                raise TerminationError(f"受管范围 {self.pid} 在终止后仍未退出")

    def release(self) -> None:
        with self._lock:
            if self._released:
                return                      # 幂等：释放过就什么都不做
            self.terminate()
            if self._job is not None:
                self._job.close()           # KILL_ON_JOB_CLOSE：句柄一关，范围里没有活口
            self._released = True

    # ── 范围状态 ─────────────────────────────────
    def _empty(self) -> bool:
        """范围是否已经空：POSIX 看组里还有没有人，Windows 看 job 里还有没有活进程。"""
        if self._popen.returncode is None:
            return False
        if self._job is not None:
            return self._job.active_processes() == 0
        return not self._group_alive()

    def _group_alive(self) -> bool:
        """POSIX：进程组里是否还有成员（组长先退出、后代仍在跑时，范围就没退）。"""
        try:
            os.killpg(self._popen.pid, 0)
        except ProcessLookupError:
            return False
        return True

    # ── 平台终止 ─────────────────────────────────
    def _kill_group_posix(self, grace_ms: int) -> None:
        """信号组升级：SIGTERM 全组 → 等宽限 → SIGKILL 全组。"""
        self._signal_group(signal.SIGTERM)
        if self.wait_for_exit(grace_ms) is None:
            self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self._popen.pid, sig)
        except ProcessLookupError:
            pass                            # 组已空：幂等路径，不是错误
