"""timeout —— 工具超时能力。

- 契约：`definition`（`DEFAULT_TOOL_TIMEOUT` 与超时调用语义）；
- 实现：`provider`（`ToolTimeout` / `ToolTimeoutPlugin`，订阅 `tools/execute` 的 around）；
- 消费方：`miniharness.tools.runtime`（把超时收敛为结构化 error 结果）。
"""