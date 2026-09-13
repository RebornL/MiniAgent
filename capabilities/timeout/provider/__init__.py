"""timeout.provider —— 超时护栏的实现（Provider）。

`ToolTimeoutPlugin` 订阅 `tools/execute`（around）：超时即返回 `AbortOutcome(TIMED_OUT)`，
由 `ToolRuntime` 规范化成结构化的 `timed_out` 结局——与成功同构，不靠异常表达。默认超时
沿用契约包 `capabilities.timeout.definition` 的 `DEFAULT_TOOL_TIMEOUT`。
工具体跑在 daemon 线程里，因此卡死的工具不会拖住进程退出。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from capabilities.timeout.definition import DEFAULT_TOOL_TIMEOUT
from miniharness.core import Context, Plugin
from miniharness.tools.contract import TIMED_OUT, AbortOutcome

__all__ = ["ToolTimeoutPlugin"]


# ═══════════════ 耐用性：超时 → tools/execute ═══════════════
class ToolTimeoutPlugin(Plugin):
    """超时护栏：订阅 `tools/execute`（around），超时返回 `timed_out` 结局。

    与 legacy `CallFunc.call_with_timeout` 的**有意差别**（用户裁定）：超时不再伪装成
    一个 `ok` 的字符串结果，也不再是与其他错误混同的 `error`——权威结果带稳定的
    `timed_out` 码，下游（重试、呈现）按码决策。默认超时沿用 `DEFAULT_TOOL_TIMEOUT`；
    工具可用 `timeout_ms` 覆盖。

    **超时只能「停止等待」，不能「取消执行」**：Python 杀不掉线程，被放弃的工具体仍会跑到底
    （结果丢弃、无副作用回滚）。因此工具体一律跑在 **daemon** 线程里——legacy 用的
    `ThreadPoolExecutor` 会在解释器退出时 join 它的 worker，一个卡死的工具能把进程挂到它跑完。
    真正的取消与终止归 `process.contract` 的受管范围（见 T8）。
    """

    inject = ("tools",)

    def __init__(self, default_ms: int = DEFAULT_TOOL_TIMEOUT * 1000) -> None:
        self.default_ms = default_ms

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/execute", self._wrap)

    def _wrap(self, payload: dict, next_: Callable[[], Any]) -> Any:
        timeout_ms = payload.get("timeout_ms") or self.default_ms
        outcome: list[Any] = []
        failure: list[BaseException] = []
        finished = threading.Event()

        def invoke() -> None:
            try:
                outcome.append(next_())
            except BaseException as exc:       # 工具体异常照常上抛，由 ToolRuntime 收敛
                failure.append(exc)
            finally:
                finished.set()

        threading.Thread(target=invoke, daemon=True).start()
        if not finished.wait(timeout_ms / 1000):
            name = payload["call"].get("name")
            return AbortOutcome(TIMED_OUT, f"工具 {name} 执行超时（{timeout_ms}ms 未返回）")
        if failure:
            raise failure[0]
        return outcome[0]
