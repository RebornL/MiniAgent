"""retry —— 工具重试能力。

- 契约：`definition`（`is_retryable` / `with_retry`：哪些故障值得重试）；
- 实现：`provider`（`RetryPlugin`，订阅 `tools/execute` 的 around）；
- 消费方：`miniharness.tools.runtime`（执行流水线消费包装后的调用结果）。
"""