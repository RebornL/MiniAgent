"""重试契约 —— 哪些中止结局码值得重试。

重试模块只对瞬时故障与可重试的中止结局重试，不重试业务错误；本包只定契约：
按**结局码**决策（`is_retryable_outcome`，只认码，不猜异常类型）。
退避循环与瞬时故障判定（`with_retry` / `is_retryable`）在实现包
`capabilities.retry.provider`。
"""
from miniharness.tools.contract import FAILED, TIMED_OUT

__all__ = ["RETRYABLE_OUTCOMES", "is_retryable_outcome"]

#: 可按结局码重试的中止结局：超时与失败可重试；被拒（策略否决）与被取消（上层意图）不可
#: ——重试一个取消会撤销用户刚刚表达的意图。
RETRYABLE_OUTCOMES = frozenset({TIMED_OUT, FAILED})


def is_retryable_outcome(code: str) -> bool:
    """按中止结局码决定是否重试（只认码，不猜异常类型）。"""
    return code in RETRYABLE_OUTCOMES
