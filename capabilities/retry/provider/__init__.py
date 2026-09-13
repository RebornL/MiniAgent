"""retry.provider —— 重试策略的实现（Provider）。

`RetryPlugin` 订阅 `tools/execute`（around），复用契约包 `capabilities.retry.definition` 的语义：
只重试瞬时故障（429/5xx、网络错误），指数退避；不可重试的错误立即抛出，
由 `ToolRuntime` 收敛为结构化失败结果。
"""
from __future__ import annotations

from typing import Any, Callable

from capabilities.retry.definition import with_retry
from miniharness.core import Context, Plugin

__all__ = ["RetryPlugin"]




# ═══════════════ 耐用性：Retry → tools/execute ═══════════════
class RetryPlugin(Plugin):
    """重试策略：订阅 `tools/execute`（around），复用 `with_retry` 的语义。

    只重试 `capabilities.retry.definition.is_retryable` 认定的瞬时故障（429/5xx、网络错误），指数退避；
    不可重试的错误立即抛出，由 ToolRuntime 收敛为结构化失败结果。
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
        )
