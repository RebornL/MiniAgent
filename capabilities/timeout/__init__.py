"""timeout —— 工具超时 / 取消能力。

- 契约：`definition`（`DEFAULT_TOOL_TIMEOUT` 与超时调用语义）；
- 实现：`provider`：
  - `ToolTimeoutPlugin`，订阅 `tools/execute` 的 around：本次调用期间把 `process` 服务包成
    登记册，超时 / 取消即终止这些受管范围，返回 `timed_out` / `cancelled` 结局；`cancel()` 同时
    登记**本轮取消**（入口复查让重试重入 `tools/execute` 也不再起进程）；
  - `TurnCancelPlugin`，消费那条本轮取消：`tools/guard` 拒绝本轮余下的工具调用（跑在审批之前）、
    `agent/post-tool` 以取消结局收尾本轮、llm seam 把取消之后模型只回的文本换成取消说明；
- 消费方：`miniharness.tools.runtime`（把中止结局规范化成结构化结果）、`capabilities.retry.provider`
  （按结局码决定是否重试）、`app.cli.InterruptSource`（信号装配：把回合执行期间的中断换成一次
  `cancel()`）。
"""
