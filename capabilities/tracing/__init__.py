"""tracing —— 执行追踪能力。

- 契约：无独立契约包（本能力无跨包 import 的稳定契约符号——消费方是鸭子类型，照
  validation / permission / final_output 先例）；span 类型词汇：
  `agent_run` / `llm_call` / `tool_call`；`log_llm_call` 只接收已归一化的纯数据、
  不接触任何 SDK 响应对象（对 provider 的耦合留在产生这些值的适配层）；`to_dicts()`
  产出的 span 字典形状经 `ctx.get("trace")` 被持久化落盘为 `traces.json`
  （展示 / 诊断用，不参与恢复）——实现与细节见 `provider`；
- 实现：`provider`（`AgentTracer` + `TraceConsumer`，订阅 Session 日志）；
- 消费方：`capabilities.persistence.provider`（鸭子类型，把 span 一并落盘）。
"""