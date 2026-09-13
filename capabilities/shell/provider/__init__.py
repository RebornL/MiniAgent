"""shell.provider —— 命令执行工具的实现（Provider）。

`ShellTool` 是受管范围（`miniharness.process.contract`）的第一个真实消费者：把已经切分好的
argv 交给 `ctx.get("process").spawn(...)`，等**整个范围**退出，读回有上限的输出。它
**不设超时、不终止**——那两件事属于策略层（超时 / 取消 → 终止受管范围，见 T8）；它只保证
命令跑在受管范围里，因而随时可被外部（策略层）终止。

两处实现取舍：

- **经服务取 seam，而不是把 seam 存进字段**：`spawn` 在调用时刻向 Context 要 `process`，
  因此策略层可以在这里包一层（记录范围句柄、请求终止），「终止受管范围」不必由工具实现。
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

__all__ = ["ShellTool"]


class ShellTool(Plugin):
    """`run_command`：argv → 受管范围 → `{exit_code, stdout, stderr, *_truncated}`。

    装载方式（`app.assembly`）：工具经技能 `shell` 可逆注册，因此**默认不可用**，
    模型要先 `load_skill('shell')`；装配层另外给它装审批策略（`ask`，无审批者默认拒绝）。
    """

    inject = ("process",)

    def __init__(self, limit: int = DEFAULT_OUTPUT_LIMIT) -> None:
        self.limit = limit
        self._ctx: Context | None = None

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx

    def run_command(self, argv: Any, cwd: str | None = None) -> dict[str, Any]:
        """在受管范围里执行 `argv`（模型给的 JSON 值，运行时校验为 argv 列表），等范围退出后
        返回结构化结果（不经 shell）。"""
        command = _argv_list(argv)
        seam = self._ctx.get("process")
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            range_ = seam.spawn(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err)
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
