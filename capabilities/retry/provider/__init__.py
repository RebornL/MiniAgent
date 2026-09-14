"""retry.provider —— 重试策略的实现（Provider）。

`RetryPlugin` 订阅 `tools/execute`（around）：结局码决策（`is_retryable_outcome`）在契约包；
退避循环与瞬时故障判定（`with_retry` / `is_retryable`）在本包。
按**结局码**决策——`timed_out` / `failed` 退避重试，`denied` / `cancelled` 不重试；
未归类为中止结局的异常仍按 `is_retryable` 判瞬时故障（429/5xx、网络错误）。
不可重试者原样返回/抛出，由 `ToolRuntime` 收敛为结构化结局。
"""
from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from capabilities.retry.definition import is_retryable_outcome
from miniharness.core import Context, Plugin
from miniharness.tools.contract import AbortOutcome

__all__ = ["RetryPlugin", "with_retry"]

T = TypeVar("T")


# 哪些 HTTP 状态码值得重试
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# 哪些 openai 错误码值得重试
RETRYABLE_ERROR_CODES = {
    "rate_limit_exceeded",
    "server_error",
    "internal_server_error",
    "service_unavailable",
    "api_connection_error",
    "api_timeout",
}


def is_retryable(error: Exception) -> bool:
    """判断一个异常是否值得重试"""
    # openai 的错误
    if hasattr(error, "status_code"):
        if error.status_code in RETRYABLE_STATUSES:
            return True
        if error.status_code == 400:
            return False  # 请求格式错误，重试没用
        if error.status_code in (401, 403):
            return False  # 鉴权问题，重试没用

    if hasattr(error, "code"):
        if error.code in RETRYABLE_ERROR_CODES:
            return True

    # 网络层错误
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True

    # 兜底：不知道是什么错误，保守不重试
    return False


def with_retry(
    fn: Callable[..., T],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    label: str = "",
    retry_value: Callable[[T], bool] | None = None,
) -> T:
    """
    带指数退避的重试包装器。

    参数:
        fn:          要重试的函数（无参）
        max_retries: 最大重试次数
        base_delay:  初始退避秒数
        max_delay:   最大退避秒数
        label:       日志标签
        retry_value: 可选的值通道判定：`fn` 正常返回的值若被它判为可重试，则同样退避重试
                     （工具中止结局走这条，例如 `timed_out`）；耗尽后返回最后一个值。
                     默认 `None`：值一概不重试（legacy 行为）。

    返回:
        fn 的返回值（或耗尽重试后的最后一个值）

    抛出:
        最后一次重试仍然失败则抛出原异常；不可重试的异常第一次就抛
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            result = fn()
        except Exception as e:
            last_error = e

            if not is_retryable(e):
                raise  # 不可重试的错误，直接抛

            if attempt == max_retries:
                break  # 重试耗尽
            retried = e
        else:
            if retry_value is None or attempt == max_retries or not retry_value(result):
                return result
            retried = result

        delay = min(base_delay * (2 ** attempt), max_delay)
        label_prefix = f"[{label}] " if label else ""
        print(f"{label_prefix}⚠️ 第 {attempt + 1}/{max_retries} 次重试，{delay:.1f}s 后重试: {retried}")
        time.sleep(delay)

    raise last_error  # 重试耗尽


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
