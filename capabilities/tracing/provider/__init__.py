"""tracing.provider —— 执行追踪的实现（Provider）。

`TraceConsumer` 订阅 Session 日志，把事件喂给本包的 `AgentTracer`；
日志里没有真实耗时，span 的 duration 记 0。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from miniharness.core import Context, Plugin
from miniharness.session import Session

__all__ = ["AgentTracer", "TraceConsumer"]





@dataclass
class Span:
    """一个 Span = Agent 运行中的一步操作"""
    span_id: str
    type: str          # "llm_call" | "tool_call" | "agent_run"
    start_time: float
    end_time: float = 0
    input: Any = None
    output: Any = None
    tokens_used: int = 0
    error: str = ""
    children: list["Span"] = field(default_factory=list)

    def duration_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000


class AgentTracer:
    """轻量级 Agent 追踪器"""

    def __init__(self):
        self.runs: list[Span] = []   # 所有运行记录

    def start_run(self, run_id: str, user_input: str) -> Span:
        span = Span(
            span_id=run_id,
            type="agent_run",
            start_time=time.time(),
            input=user_input,
        )
        self.runs.append(span)
        return span

    def log_llm_call(
        self,
        messages: list,
        *,
        content: str = "",
        tool_calls: list | None = None,
        tokens_used: int = 0,
        duration: float = 0.0,
    ) -> Span:
        """记录一次 LLM 调用（provider 无关）。

        只接收已归一化的纯数据，不接触任何 SDK 的响应对象——对 provider 的耦合留在
        产生这些值的那一层（provider 适配器），不渗进追踪器与它的消费者。
        """
        end = time.time()
        return Span(
            span_id=f"llm_{uuid.uuid4().hex[:8]}",
            type="llm_call",
            start_time=end - duration,
            end_time=end,
            input={"message_count": len(messages)},
            output={"content": content, "tool_calls": list(tool_calls or [])},
            tokens_used=tokens_used,
        )

    def log_tool_call(self, name: str, args: dict, result: str, duration: float) -> Span:
        span = Span(
            span_id=f"tool_{uuid.uuid4().hex[:8]}",
            type="tool_call",
            start_time=time.time() - duration,
            end_time=time.time(),
            input={"tool": name, "args": args},
            output=result,
        )
        return span

    def to_dicts(self) -> list[dict]:
        return [asdict(r) for r in self.runs]

    def summary(self, run: Span) -> str:
        """打印人类可读的运行摘要"""
        lines = [f"\n{'='*60}",
                 f"📋 Run: {run.span_id}",
                 f"📥 用户输入: {run.input}",
                 f"⏱️ 总耗时: {run.duration_ms():.0f}ms",
                 f"{'='*60}"]
        total_tokens = 0
        for child in run.children:
            if child.type == "llm_call":
                total_tokens += child.tokens_used
                tc_info = child.output.get("tool_calls", [])
                lines.append(f"  🧠 LLM 调用 ({child.duration_ms():.0f}ms, {child.tokens_used} tokens)")
                for tc in tc_info:
                    lines.append(f"      └─ 决定调用: {tc['name']}({tc['arguments']})")
            elif child.type == "tool_call":
                lines.append(f"  🔧 {child.input['tool']} → {child.output[:80]} ({child.duration_ms():.0f}ms)")
        lines.append(f"  💰 总 Token: {total_tokens}")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


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
