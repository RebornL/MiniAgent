"""tracing.provider —— 执行追踪的实现（Provider）。

`TraceConsumer` 订阅 Session 日志，把事件喂给契约包 `capabilities.tracing.definition` 的
`AgentTracer`；日志里没有真实耗时，span 的 duration 记 0。
"""
from __future__ import annotations

import json

from capabilities.tracing.definition import AgentTracer, Span
from miniharness.core import Context, Plugin
from miniharness.session import Session

__all__ = ["TraceConsumer"]





class TraceConsumer(Plugin):
    """把 Session 日志喂给 `AgentTracer`（复用 start_run / log_llm_call / log_tool_call）。

    日志里没有真实耗时（不在本阶段范围），span 的 duration 记 0。
    """

    inject = ("session",)

    def __init__(self, tracer: AgentTracer | None = None,
                 run_id: str | None = None) -> None:
        self.tracer = tracer or AgentTracer()
        self.run_id = run_id or "run_0"
        self.run: Span | None = None

    def apply(self, ctx: Context) -> None:
        self._session: Session = ctx.get("session")
        ctx.provide("trace", self)          # 供 PersistenceConsumer 落盘真实 span
        ctx.effect(self._session.subscribe(self._on_event))

    def _on_event(self, event: dict) -> None:
        kind = event["type"]
        if kind == "turn/start":
            self.run = self.tracer.start_run(self.run_id, event.get("input", ""))
        elif kind == "assistant/message" and event.get("tool_calls"):
            # 只传纯数据：不再伪造 provider 响应对象，追踪器也不认识任何 SDK 类型
            messages = self._session.derive_messages()[:-1]   # 去掉刚追加的这条回复
            self._span(self.tracer.log_llm_call(
                messages,
                content=event.get("content", ""),
                tool_calls=[
                    {"name": call.get("name", ""),
                     "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)}
                    for call in event["tool_calls"]
                ],
            ))
        elif kind == "tool/result":
            self._span(self.tracer.log_tool_call(
                event.get("name", ""), event.get("args") or {},
                str(event.get("content", "")), 0.0))

    def _span(self, span: Span) -> None:
        if self.run is not None:
            self.run.children.append(span)
