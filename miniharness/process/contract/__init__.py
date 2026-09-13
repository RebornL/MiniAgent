"""process.contract —— 受管范围（managed range）seam 的契约（Definition）。

**受管范围**是一次执行所拥有的整棵进程树（含后代），终止以它为单位而不是以单个进程为单位。
本包只写形状与语义，不含任何平台实现（平台后端见 `providers.process`）：

- **范围本位**：一次 `spawn` 产出的整棵进程树是**同一个**受管范围；`wait_for_exit` /
  `poll` / `terminate` / `release` 观察和作用的都是它，而不是单个进程。组长自己先退出
  **不**让范围变空——只要还有后代在跑，范围就没退。
- **终止幂等**：`terminate()` 是唯一的终止动词；范围已空时它是 no-op，重复调用安全。
- **释放即收尾**：`release()` 返回时该范围已经退出，不留孤儿；重复调用同样安全。
- **只收 argv**：不走 shell（字符串拼接交给沙箱 seam 去决定，本 seam 只执行 argv）。

**边界**（机制够不到的地方，写在这里而不是藏进后端）：

- POSIX 的终止靠**进程组**：后代若自行 `setsid` / `setpgid` 另立会话或换组，就脱离了范围，
  终止够不着它。这是信号组机制的固有边界。
- Windows 的终止靠 **Job Object**：组长入册后派生的一切后代都在册，且 job 未开 breakaway，
  后代无法脱离；`release()` 关闭 job 句柄（`KILL_ON_JOB_CLOSE`）是最后的兜底。
- 平台机制不可得时（例如 Windows 上建不了 job），`spawn` 抛 `RangeUnavailableError`：
  宁可显式失败，也不退化成「只跟踪组长」——那正是会留孤儿的那种退化。

本包是工具执行里变动最慢的一层：平台终止机制的改动不影响消费方依赖的那个接口。
"""
from __future__ import annotations

from typing import Any, Sequence

from miniharness.core import Context, Plugin

__all__ = [
    "DEFAULT_GRACE_MS",
    "ManagedRange",
    "ProcessSeam",
    "RangeUnavailableError",
    "TerminationError",
]

#: 终止宽限期的默认值（毫秒）：POSIX 后端先温和请求（SIGTERM），宽限期满仍不退再强杀
#: （SIGKILL）；Windows 后端没有温和档，一次强杀到底，此时它不参与升级。
DEFAULT_GRACE_MS = 2000


class RangeUnavailableError(RuntimeError):
    """平台机制不可得，建立不起真正的受管范围：调用方必须看见，不得退化成组长本位。"""


class TerminationError(RuntimeError):
    """受管范围在强杀后仍未退出：终止没能达成目的，必须让调用方看见。"""


class ManagedRange:
    """受管范围句柄的契约（实现见 `providers.process.SubprocessRange`）。

    退出码沿用 `subprocess` 的约定（POSIX 被信号杀死为负值）；进程被终止不等于失败，
    它是与成功同构的一个结局，由上层决定怎么呈现。
    """

    @property
    def pid(self) -> int:
        """范围的组长（`spawn` 直接启动的那个进程）pid，供诊断与日志用。"""
        raise NotImplementedError

    def poll(self) -> int | None:
        """非阻塞地看一眼：范围已退出返回退出码，仍在跑返回 None。"""
        raise NotImplementedError

    def wait_for_exit(self, timeout_ms: int | None = None) -> int | None:
        """等整个范围退出（含后代），返回退出码；到 `timeout_ms` 仍未退返回 None。

        `timeout_ms=None` 表示一直等。等的是**范围**：组长先退出、后代还在跑时，范围没退。
        """
        raise NotImplementedError

    def terminate(self, grace_ms: int = DEFAULT_GRACE_MS) -> None:
        """终止整个范围：手段随平台后端（POSIX：先 SIGTERM、宽限期满仍不退再 SIGKILL；
        Windows：一次强杀整棵树）。返回时范围已退出。

        幂等：范围已空（或已释放）时为 no-op。强杀后仍无法确认退出才抛 `TerminationError`。
        """
        raise NotImplementedError

    def release(self) -> None:
        """收尾并释放：若范围仍在跑就先终止，返回时范围保证已退出，不留孤儿。幂等。"""
        raise NotImplementedError


class ProcessSeam(Plugin):
    """进程 seam 的 Definition：`spawn(argv, ...) -> ManagedRange`。

    只负责进程生命周期；「何时终止」是策略（超时 / 取消）的事，不写在这里，
    也不认识任何策略插件。
    """

    def apply(self, ctx: Context) -> None:
        ctx.provide("process", self)

    def spawn(self, argv: Sequence[str], *, cwd: str | None = None,
              env: dict[str, str] | None = None, stdin: Any = None,
              stdout: Any = None, stderr: Any = None) -> ManagedRange:
        """启动一个受管范围，返回它的句柄；`argv` 为空即 `ValueError`（不做 shell 解析）。

        返回时范围已经完整在册（Windows 上组长是入册之后才被放行的），此后它派生的一切
        后代都属于这个范围。平台机制不可得时抛 `RangeUnavailableError`。
        """
        raise NotImplementedError
