"""tracing —— 执行追踪能力。

- 契约：`definition`（`Span` / `AgentTracer`）；
- 实现：`provider`（`TraceConsumer`，订阅 Session 日志）；
- 消费方：`capabilities.persistence.provider`（把 span 一并落盘）。
"""