"""shell.provider —— 命令执行工具的实现（Provider）。

`ShellTool` 一次调用经过**两个互不相干的 seam**：

1. **沙箱 seam**（`miniharness.sandbox.contract`）：把「调用意图 + 策略」交给
   `ctx.get("sandbox").wrap(...)`，拿回**可执行的 argv 与完整性要求**；
2. **受管范围 seam**（`miniharness.process.contract`）：用沙箱给的 argv / 环境 / 工作目录
   `spawn`，等**整个范围**退出，读回有上限的输出。

它**不设超时、不终止**——那两件事属于策略层（超时 / 取消 → 终止受管范围，见 T8）；沙箱也
不负责终止（沙箱只包装 argv）。工具只保证命令**在沙箱里、跑在受管范围里**，因而随时可被
外部（策略层）终止。

**沙箱不可用即失败**：`ctx.get("sandbox")` 取不到（未装载 / 已卸载）或 `wrap` 抛错时，工具
抛 `SandboxUnavailableError`，由 `ToolRuntime` 规范成结构化的 `failed` 结局，**绝不回退到
不经沙箱直接执行**。

三处实现取舍：

- **经服务取 seam，而不是把 seam 存进字段**：`sandbox` 与 `process` 都在**调用时刻**向
  Context 要，因此策略层可以在这里包一层（记录范围句柄、请求终止），沙箱也可以在装配后
  被卸载而立刻生效（fail-closed）；「终止受管范围」不必由工具实现。
- **环境与工作目录由沙箱决定**：执行时用 `requirements.env` / `requirements.cwd`，而不是
  调用方的 `os.environ`——凡能影响子进程看到什么的东西，都经过沙箱。
- **输出用临时文件捕获，而不是管道**：若「等范围退出」排在「读管道」之前，写满管道缓冲的
  子进程会卡死（经典的 pipe 死锁）；临时文件让两者互不阻塞，读回时又只读 `limit + 1` 字节，
  于是内存是**有界**的。`wait_for_exit` 返回时范围已空，`release()` 只是释放范围句柄
  （`terminate` 在已空的范围上是 no-op）。
"""
from __future__ import annotations

import subprocess
import tempfile
from typing import Any

from capabilities.shell.definition import DEFAULT_OUTPUT_LIMIT, truncated_note
from miniharness.core import Context, Plugin
from miniharness.sandbox.contract import (
    CommandIntent,
    SandboxPolicy,
    SandboxUnavailableError,
)

__all__ = ["ShellTool"]


class ShellTool(Plugin):
    """`run_command`：argv → 沙箱 seam → 受管范围 → `{exit_code, stdout, stderr, *_truncated}`。

    装载方式（`app.assembly`）：工具经技能 `shell` 可逆注册，因此**默认不可用**，
    模型要先 `load_skill('shell')`；装配层另外给它装审批策略（`ask`，无审批者默认拒绝）。

    `policy` 是给沙箱 seam 的隔离策略（默认最严：不继承任何环境变量）；装配层按需要放宽。
    """

    inject = ("process", "sandbox")

    def __init__(self, limit: int = DEFAULT_OUTPUT_LIMIT,
                 policy: SandboxPolicy | None = None) -> None:
        self.limit = limit
        self.policy = policy or SandboxPolicy()
        self._ctx: Context | None = None

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx

    def run_command(self, argv: Any, cwd: str | None = None) -> dict[str, Any]:
        """先经沙箱 seam 包装（拿回 argv 与完整性要求），再在受管范围里执行；等范围退出后
        返回结构化结果（不经 shell）。沙箱不可用时抛 `SandboxUnavailableError`——绝不回退
        到无约束执行。"""
        command = _argv_list(argv)
        sandbox = self._ctx.get("sandbox") if self._ctx is not None else None
        if sandbox is None:
            raise SandboxUnavailableError(
                "沙箱不可用：run_command 拒绝在无约束的环境里执行外部命令")
        wrapped = sandbox.wrap(CommandIntent(tuple(command), cwd), self.policy)
        seam = self._ctx.get("process")
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            range_ = seam.spawn(wrapped.argv,
                                cwd=wrapped.requirements.cwd,
                                env=dict(wrapped.requirements.env),
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            try:
                exit_code = range_.wait_for_exit()      # 一直等：时限是策略层的事
            finally:
                range_.release()                        # 释放范围句柄（已退出 → 幂等）
            stdout, out_truncated = _read_capped(out, self.limit)
            stderr, err_truncated = _read_capped(err, self.limit)
        return {
            "exit_code": exit_code,
            "stdout": stdout + (truncated_note(self.limit) if out_truncated else ""),
            "stderr": stderr + (truncated_note(self.limit) if err_truncated else ""),
            "stdout_truncated": out_truncated,
            "stderr_truncated": err_truncated,
        }


def _argv_list(argv: Any) -> list[str]:
    """校验并归一化 argv：只接受非空的字符串列表——既不经 shell，也不收命令字符串。"""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError(
            'run_command 只接受非空的 argv 列表（不经 shell），'
            '例如 ["git", "status"]；不要传 shell 字符串')
    if not all(isinstance(arg, str) for arg in argv):
        raise ValueError("run_command 的 argv 列表每一项都必须是字符串（不经 shell）")
    return list(argv)


def _read_capped(stream: Any, limit: int) -> tuple[str, bool]:
    """从捕获文件读回有上限的输出：最多 `limit` 字节，超出即标记截断。"""
    stream.seek(0)
    data = stream.read(limit + 1)
    return data[:limit].decode("utf-8", "replace"), len(data) > limit
