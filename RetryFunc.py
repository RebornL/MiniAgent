"""
重试模块 —— 只对瞬时故障重试，不重试业务错误
"""
import time
from typing import TypeVar, Callable

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
) -> T:
    """
    带指数退避的重试包装器。

    参数:
        fn:          要重试的函数（无参）
        max_retries: 最大重试次数
        base_delay:  初始退避秒数
        max_delay:   最大退避秒数
        label:       日志标签

    返回:
        fn 的返回值

    抛出:
        最后一次重试仍然失败则抛出原异常
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e

            if not is_retryable(e):
                raise  # 不可重试的错误，直接抛

            if attempt == max_retries:
                break  # 重试耗尽

            delay = min(base_delay * (2 ** attempt), max_delay)
            label_prefix = f"[{label}] " if label else ""
            print(f"{label_prefix}⚠️ 第 {attempt + 1}/{max_retries} 次重试，{delay:.1f}s 后重试: {e}")
            time.sleep(delay)

    raise last_error  # 重试耗尽