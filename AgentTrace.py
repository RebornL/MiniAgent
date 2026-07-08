import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

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

    def log_llm_call(self, messages: list, response, duration: float) -> Span:
        span = Span(
            span_id=f"llm_{uuid.uuid4().hex[:8]}",
            type="llm_call",
            start_time=time.time() - duration,
            end_time=time.time(),
            input={"message_count": len(messages)},
            output={
                "content": response.choices[0].message.content,
                "tool_calls": [
                    {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                    for tc in (response.choices[0].message.tool_calls or [])
                ],
            },
            tokens_used=response.usage.total_tokens if response.usage else 0,
        )
        return span

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