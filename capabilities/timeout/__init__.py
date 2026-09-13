"""timeout —— 工具超时 / 取消能力。

- 契约：`definition`（`DEFAULT_TOOL_TIMEOUT` 与超时调用语义）；
- 实现：`provider`（`ToolTimeoutPlugin`，订阅 `tools/execute` 的 around：本次调用期间把
  `process` 服务包成登记册，超时 / 取消即终止这些受管范围，返回 `timed_out` / `cancelled` 结局）；
- 消费方：`miniharness.tools.runtime`（把中止结局规范化成结构化结果）、`capabilities.retry.provider`
  （按结局码决定是否重试）。
"""
