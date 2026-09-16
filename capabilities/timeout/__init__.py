"""timeout —— 工具超时 / 取消能力。

- 契约：无独立契约包（本能力无跨包 import 的契约符号——结局码词汇 `TIMED_OUT` / `CANCELLED`
  在骨架 `miniharness.tools.contract`，照 validation / tracing / permission / final_output 先例）。
  语义：超时 = 停止等待并终止本次调用起的受管范围，返回结构化 `timed_out` / `cancelled`
  结局（不再是伪装成 ok 的字符串）；取消粘住本轮；默认超时 30 秒（`DEFAULT_TOOL_TIMEOUT`，
  现为本包实现常量），工具可 `timeout_ms` 覆盖——细节见 `provider`；
- 实现：`provider`：
  - `ToolTimeoutPlugin`，订阅 `tools/execute` 的 around：本次调用期间把 `process` 服务包成
    登记册，超时 / 取消即终止这些受管范围，返回 `timed_out` / `cancelled` 结局；`cancel()` 同时
    登记**本轮取消**（入口复查让重试重入 `tools/execute` 也不再起进程）；
  - `TurnCancelPlugin`，消费那条本轮取消：`tools/guard` 拒绝本轮余下的工具调用（跑在审批之前）、
    `agent/post-tool` 以取消结局收尾本轮、`agent/turn-stopping` 把取消后的 `Loop` 自主收尾
    （纯文本答复 / 工具全被拒 / 步数用尽）一律改判为取消结局；
  - legacy `call_with_timeout`（线程池超时实现）零调用方，原样保留；
- 消费方：`miniharness.tools.runtime`（把中止结局规范化成结构化结果）、`capabilities.retry.provider`
  （按结局码决定是否重试）、`app.cli.InterruptSource`（信号装配：把回合执行期间的中断换成一次
  `cancel()`）。
"""
