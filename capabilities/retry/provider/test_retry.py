"""包内测试：重试策略的实现与契约（`retry.definition`）逐字等价。

同一个失败序列分别跑契约包与 `tools/execute` 上的 `RetryPlugin`，断言尝试次数一致：
可重试的瞬时故障退避重试，不可重试的错误一次都不重试，重试耗尽用尽 max_retries + 1 次。
另断言中止结局按**码**决策：`timed_out` / `failed` 重试，`denied` / `cancelled` 不重试。
"""
from __future__ import annotations

import pytest

from capabilities.retry.definition import (
    is_retryable_outcome,
    with_retry,
)
from capabilities.retry.provider import RetryPlugin
from miniharness.core import Plugin
from miniharness.tools.contract import (
    CANCELLED,
    DENIED,
    FAILED,
    OK,
    TIMED_OUT,
    AbortOutcome,
    ToolDefinition,
)
from miniharness.tools.runtime.test_pipeline import _pipeline



def test_retry_plugin_matches_with_retry_semantics():
    # ── legacy 对照：同一失败序列跑 with_retry ──
    legacy_errors = [ConnectionError("抖动"), ConnectionError("抖动")]
    legacy_attempts: list[int] = []

    def legacy_fn() -> str:
        legacy_attempts.append(1)
        if legacy_errors:
            raise legacy_errors.pop(0)
        return "成功"

    assert with_retry(legacy_fn, max_retries=2, base_delay=0.0) == "成功"

    # ── harness：同一个失败序列走 tools/execute 上的 RetryPlugin ──
    errors = [ConnectionError("抖动"), ConnectionError("抖动")]
    attempts: list[int] = []

    def flaky(args: dict) -> str:
        attempts.append(1)
        if errors:
            raise errors.pop(0)
        return "成功"

    _, runtime = _pipeline(RetryPlugin(max_retries=2, base_delay=0.0),
                           ToolDefinition("flaky", "", {}, flaky))
    result = runtime.run({"id": "c1", "name": "flaky", "args": {}})

    assert result["status"] == "ok" and result["content"] == "成功"
    assert len(attempts) == len(legacy_attempts) == 3

    # ── 不可重试的错误：一次都不重试（legacy 直接抛出）──
    legacy_fatal: list[int] = []

    def legacy_fatal_fn() -> str:
        legacy_fatal.append(1)
        raise ValueError("参数错了")

    with pytest.raises(ValueError):
        with_retry(legacy_fatal_fn, max_retries=3, base_delay=0.0)

    harness_fatal: list[int] = []

    def fatal(args: dict) -> str:
        harness_fatal.append(1)
        raise ValueError("参数错了")

    _, runtime = _pipeline(RetryPlugin(max_retries=3, base_delay=0.0),
                           ToolDefinition("fatal", "", {}, fatal))
    result = runtime.run({"id": "c2", "name": "fatal", "args": {}})

    assert result["status"] == FAILED and "ValueError" in result["error"]
    assert len(legacy_fatal) == len(harness_fatal) == 1

    # ── 可重试但重试耗尽：与 legacy 一样用尽 max_retries + 1 次 ──
    legacy_exhausted = [ConnectionError("一直抖")] * 3
    legacy_tries: list[int] = []

    def legacy_always_fail() -> str:
        legacy_tries.append(1)
        raise legacy_exhausted.pop(0)

    with pytest.raises(ConnectionError):
        with_retry(legacy_always_fail, max_retries=2, base_delay=0.0)

    exhausted = [ConnectionError("一直抖")] * 3
    harness_tries: list[int] = []

    def always_fail(args: dict) -> str:
        harness_tries.append(1)
        raise exhausted.pop(0)

    _, runtime = _pipeline(RetryPlugin(max_retries=2, base_delay=0.0),
                           ToolDefinition("always_fail", "", {}, always_fail))
    result = runtime.run({"id": "c3", "name": "always_fail", "args": {}})

    assert result["status"] == FAILED and "ConnectionError" in result["error"]
    assert len(legacy_tries) == len(harness_tries) == 3


def test_retry_plugin_decides_by_abort_outcome_code():
    """T5/AC4：中止结局按**码**决策，而不是把所有中止都当一回事。

    `timed_out` / `failed` 可重试（重试到成功为止）；`denied` / `cancelled` 一次都不重试，
    原样返回结构化结局——重试被拒会绕过策略，重试取消会撤销用户刚表达的意图。
    """
    # 契约表本身：稳定码 → 可重试与否
    assert is_retryable_outcome(TIMED_OUT) and is_retryable_outcome(FAILED)
    assert not is_retryable_outcome(DENIED) and not is_retryable_outcome(CANCELLED)

    attempts: dict[str, int] = {}
    ran: list[str] = []

    class _FlakyAbort(Plugin):
        """前两次返回给定的中止结局，第三次放行到工具体（模拟瞬时故障恢复）。"""

        inject = ("tools",)

        def __init__(self, code: str) -> None:
            self.code = code

        def apply(self, ctx) -> None:
            ctx.on("tools/execute", self._wrap)

        def _wrap(self, payload: dict, next_):
            attempts[self.code] = attempts.get(self.code, 0) + 1
            if attempts[self.code] < 3:
                return AbortOutcome(self.code, f"{self.code} 第 {attempts[self.code]} 次")
            return next_()

    for code, expected in ((TIMED_OUT, OK), (FAILED, OK), (DENIED, DENIED),
                           (CANCELLED, CANCELLED)):
        ctx, runtime = _pipeline(
            RetryPlugin(max_retries=3, base_delay=0.0),
            ToolDefinition("flaky", "", {}, lambda a: ran.append(a) or "成功"))
        ctx.load(_FlakyAbort(code))          # 装在 retry 内侧：它的结局由外层重试决策

        result = runtime.run({"id": code, "name": "flaky", "args": {}})

        assert result["status"] == expected, (code, result)
        assert attempts[code] == (3 if expected == OK else 1), (code, attempts[code])
    assert len(ran) == 2                     # 只有两个可重试的码真的走到了工具体
