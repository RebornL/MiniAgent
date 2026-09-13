"""providers.sandbox —— 沙箱 seam 的平台后端（Provider）。

`EnvSandbox` 只强制它**真能强制**的两件事：

1. **环境收敛**：子进程只看到策略白名单里的变量（外加沙箱自己的标记），所以 agent 进程
   **环境变量里**的凭据 / token 不会随一条命令泄漏给子进程；
2. **argv 解析**：`argv[0]` 是裸名字时，在**收敛后的 PATH** 里解析成绝对路径——此时「跑的
   到底是哪一个二进制」由沙箱决定，而不是由调用方所处环境里的 PATH 碰运气决定。

**它不做什么**（别把它当成完整沙箱）：

- **不隔离文件系统**：子进程的 cwd 就是 agent 的 cwd，磁盘上任何进程可读的文件都仍然可读——
  一条 `run_command`（例如 `python -c`）能读走仓库根的 `config.json` 或 `~/.aws/credentials`；
- **不强制断网**：`deny_network` 只是**拒绝服务**，不是隔离机制；
- **白名单只限环境变量**：它管的是「子进程继承哪些变量」，`HOME` / `USERPROFILE` 仍在白名单里，
  环境之外的凭据（文件、socket、系统钥匙串）一概不在它的射程内；
- **绝对路径不受限制**：`argv[0]` 带路径（含绝对路径）时 `_resolve` 原样放行，上面的「跑哪个
  二进制由沙箱决定」**只对裸名成立**。

凡是策略能要求、而它做不到的（例如 `deny_network`），它**拒绝服务**
（`SandboxUnavailableError`），而不是假装隔离生效——这是 `sandbox.contract` 的 fail-closed
承诺在实现侧的落点。

**它不负责终止**：本后端只产出 argv 与完整性要求，一个进程都不起；起进程、等退出、终止
整棵树都是 `providers.process`（受管范围）的事。
"""
from __future__ import annotations

import os
import shutil
from typing import Mapping

from miniharness.sandbox.contract import (
    CommandIntent,
    IntegrityRequirements,
    SandboxedCommand,
    SandboxPolicy,
    SandboxSeam,
    SandboxUnavailableError,
)

__all__ = ["SANDBOX_MARKER", "EnvSandbox"]

#: 子进程环境里的沙箱标记：值是后端名，子进程据此知道自己跑在沙箱里。
SANDBOX_MARKER = "MINIHARNESS_SANDBOX"


class EnvSandbox(SandboxSeam):
    """以环境收敛与 argv 解析为强制手段的沙箱后端（不负责终止）。"""

    #: 后端名：写在子进程环境的标记里（`SANDBOX_MARKER`）。
    name = "env"

    def wrap(self, intent: CommandIntent, policy: SandboxPolicy) -> SandboxedCommand:
        """收敛环境并解析 `argv[0]`；满足不了策略时拒绝服务，不降级执行。"""
        if not intent.argv:
            raise ValueError("沙箱包装需要一个非空 argv")
        if policy.deny_network:
            raise SandboxUnavailableError(
                f"{self.name} 沙箱不能强制断网：策略要求 deny_network，"
                "拒绝在隔离承诺不成立的情况下执行")
        env = {name: os.environ[name] for name in policy.env_allowlist if name in os.environ}
        env[SANDBOX_MARKER] = self.name
        return SandboxedCommand(
            argv=(_resolve(intent.argv[0], env), *intent.argv[1:]),
            requirements=IntegrityRequirements(env=env, cwd=intent.cwd),
        )


def _resolve(program: str, env: Mapping[str, str]) -> str:
    """把裸名字解析成绝对路径：解析只在**收敛后的环境**里做，用不到调用方的环境。"""
    if os.path.dirname(program):
        return program                      # 已给路径（含绝对路径）：原样，不做二次解析
    path = env.get("PATH")
    found = shutil.which(program, path=path) if path else None
    if found is None:
        raise SandboxUnavailableError(
            f"沙箱无法在收敛后的环境里解析 {program!r}："
            "策略需允许 PATH，或在 argv 里给出可执行文件的完整路径")
    return found
