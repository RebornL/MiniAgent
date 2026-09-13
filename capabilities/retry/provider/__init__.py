"""retry.provider —— 重试策略的实现（Provider）。

`RetryPlugin` 订阅 `tools/execute`（around），复用契约包 `capabilities.retry.definition` 的语义：
按**结局码**决策——`timed_out` / `failed` 退避重试，`denied` / `cancelled` 不重试；
未归类为中止结局的异常仍按 `is_retryable` 判瞬时故障（429/5xx、网络错误）。
不可重试者原样返回/抛出，由 `ToolRuntime` 收敛为结构化结局。
"""
from __future__ import annotations

from typing import Any, Callable

from capabilities.retry.definition import is_retryable_outcome, with_retry
from miniharness.core import Context, Plugin
from miniharness.tools.contract import AbortOutcome

__all__ = ["RetryPlugin"]


def _retryable_value(value: Any) -> bool:
    """值通道上的可重试判定：策略在 `tools/execute` 上返回的中止结局按码决策。"""
    return isinstance(value, AbortOutcome) and is_retryable_outcome(value.code)


# ═══════════════ 耐用性：Retry → tools/execute ═══════════════
class RetryPlugin(Plugin):
    """重试策略：订阅 `tools/execute`（around），复用 `with_retry` 的语义。

    中止结局按**结局码**决策：`timed_out` / `failed` 退避重试到底；`denied` / `cancelled`
    立即原样返回（重试被拒会绕过策略，重试取消会撤销用户意图）。工具直接抛出的异常走
    `is_retryable`：只重试瞬时故障（429/5xx、网络错误），业务错误（如 ValueError）一次都不重试。
    """

    inject = ("tools",)

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0,
                 max_delay: float = 30.0) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/execute", self._wrap)

    def _wrap(self, payload: dict, next_: Callable[[], Any]) -> Any:
        return with_retry(
            next_,
            max_retries=self.max_retries,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            label=payload["call"].get("name", ""),
            retry_value=_retryable_value,
        )
