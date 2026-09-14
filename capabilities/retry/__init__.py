"""retry —— 工具重试能力。

- 契约：`definition`（`RETRYABLE_OUTCOMES` / `is_retryable_outcome`：哪些中止结局码值得重试）；
- 实现：`provider`（`with_retry` / `is_retryable`：退避循环与瞬时故障判定；`RetryPlugin`，订阅 `tools/execute` 的 around）；
- 消费方：`miniharness.tools.runtime`（执行流水线消费包装后的调用结果）。
"""
