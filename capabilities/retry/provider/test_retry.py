"""包内测试：重试策略的实现与契约（`retry.definition`）逐字等价。

同一个失败序列分别跑契约包与 `tools/execute` 上的 `RetryPlugin`，断言尝试次数一致：
可重试的瞬时故障退避重试，不可重试的错误一次都不重试，重试耗尽用尽 max_retries + 1 次。
"""
from __future__ import annotations

import pytest

from capabilities.retry.definition import with_retry
from capabilities.retry.provider import RetryPlugin
from miniharness.tools.contract import ToolDefinition
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

    assert result["status"] == "error" and "ValueError" in result["error"]
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

    assert result["status"] == "error" and "ConnectionError" in result["error"]
    assert len(legacy_tries) == len(harness_tries) == 3
