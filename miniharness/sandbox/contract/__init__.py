"""sandbox.contract —— 沙箱 seam 的契约（Definition）。

**沙箱 seam 只包装 argv**：调用方给出「调用意图 + 策略」，它返回「可执行的 argv + 执行它
必须满足的完整性要求」。它**不负责终止**——终止归受管范围（`miniharness.process.contract`），
两者是两个独立 seam，不得混做：沙箱只决定「拿什么 argv、在什么环境下跑」，进程的生死与
终止另有其人。

- **调用意图（`CommandIntent`）**：调用方想执行什么——已经切分好的 argv 与工作目录；
- **策略（`SandboxPolicy`）**：调用方要求的隔离强度。沙箱据它包装，**做不到就拒绝服务**
  （`SandboxUnavailableError`），绝不退化成无约束执行——这是本 seam 的 fail-closed 承诺：
  隔离承诺要么成立，要么调用方看见失败；
- **产物（`SandboxedCommand`）**：可执行的 argv + `IntegrityRequirements`（子进程必须看到的
  确切环境与工作目录）。执行方必须逐条满足这些要求再启动进程；能满足要求的是进程 seam
  （`ProcessSeam.spawn(argv, cwd=..., env=...)`），**沙箱自己不起进程**。

强制手段随平台与后端变（环境收敛、可执行文件解析…），属于实现，在 `providers/sandbox`；
本包只写形状与语义。消费方是 `capabilities.shell.provider`（`run_command`），装配在 `app/`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from miniharness.core import Context, Plugin

__all__ = [
    "CommandIntent",
    "IntegrityRequirements",
    "SandboxPolicy",
    "SandboxSeam",
    "SandboxUnavailableError",
    "SandboxedCommand",
]


class SandboxUnavailableError(RuntimeError):
    """沙箱不可用，或满足不了调用方要求的策略：调用方必须看见，绝不放行无约束执行。"""


@dataclass(frozen=True)
class CommandIntent:
    """调用意图：要执行什么——argv 已切分（不经 shell），cwd 可选。不含任何执行机制。"""

    argv: tuple[str, ...]
    cwd: str | None = None


@dataclass(frozen=True)
class SandboxPolicy:
    """调用方要求的隔离强度；沙箱据它包装，做不到就 fail-closed。

    - `env_allowlist`：允许子进程继承的环境变量名（**白名单**，默认为空 = 一个都不继承）；
    - `deny_network`：要求断网。只有真能强制断网的后端才敢接受它——接受了却做不到，
      隔离承诺就被悄悄打破，正是本 seam 禁止的「静默降级」。
    """

    env_allowlist: tuple[str, ...] = ()
    deny_network: bool = False


@dataclass(frozen=True)
class IntegrityRequirements:
    """沙箱对执行环境提出的完整性要求：执行方必须逐条满足，否则命令不得运行。

    这是「沙箱承诺」的可传递形态——谁能满足它，谁才能把命令放出去。
    """

    env: Mapping[str, str]
    cwd: str | None = None


@dataclass(frozen=True)
class SandboxedCommand:
    """沙箱包装的产物：`argv`（在 `requirements` 描述的环境下可执行）+ 完整性要求。"""

    argv: tuple[str, ...]
    requirements: IntegrityRequirements


class SandboxSeam(Plugin):
    """沙箱 seam 的 Definition：`wrap(intent, policy) -> SandboxedCommand`。

    只包装 argv 与环境，**不认识终止**（没有 `terminate` / `wait` 之类的动词）：消费方拿到
    产物后交给进程 seam 去起进程，终止与否则由受管范围与策略层决定。
    """

    def apply(self, ctx: Context) -> None:
        ctx.provide("sandbox", self)

    def wrap(self, intent: CommandIntent, policy: SandboxPolicy) -> SandboxedCommand:
        """按策略包装调用意图，返回可执行的 argv 与完整性要求。

        沙箱不可用（未装载 / 平台机制不可得 / 策略超出后端能力）时抛
        `SandboxUnavailableError`：调用方据此得到结构化的失败结局，而不是一次无约束执行。
        """
        raise NotImplementedError
