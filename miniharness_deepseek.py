"""miniharness_deepseek.py — LLM seam 的真实 provider（DeepSeek / OpenAI 兼容接口）

`miniharness-spec.md` 的「能力 seam」在这里落到真实后端：`DeepSeekProvider` 只实现
`complete(messages) -> {"text", "tool_calls"}`，工具描述经 `ctx.get("tools").specs()` 自取；
循环与其他插件完全不认识它（换 provider 不动循环）。

import 本模块不做任何网络调用；真实联调 demo：`python miniharness_deepseek.py`。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from openai import OpenAI

from miniharness import Context, LLM

__all__ = ["DeepSeekProvider"]


class DeepSeekProvider(LLM):
    """流式累积 content 与 tool_calls 分片的 DeepSeek provider。

    两个方向的形状适配都留在这里（循环只看到归一化形状）：
    出站 `_wire_messages` 把 Session 的 `{id, name, args}` 译成线格式，
    入站 `complete` 把线格式的分片/响应归一化回 `{id, name, args}`。
    `on_delta(text)` 在流式模式下逐片回调（供实时打印）；非流式模式不回调。
    """

    inject = ("tools",)

    def __init__(
        self,
        client: OpenAI,
        model: str,
        *,
        stream: bool = True,
        on_delta: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.stream = stream
        self.on_delta = on_delta

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        ctx.provide("llm", self)

    def complete(self, messages: list[dict]) -> dict:
        tools = self._ctx.get("tools")
        specs = tools.specs() if tools is not None else []
        response = self.client.chat.completions.create(
            model=self.model,
            messages=_wire_messages(messages),
            tools=specs or None,
            tool_choice="auto" if specs else None,
            stream=self.stream,
        )
        if not self.stream:
            return self._from_message(response.choices[0].message)
        return self._from_chunks(response)

    # ── 分片/响应归一化（provider 的私事：loop 只看到 {text, tool_calls}）──
    def _from_chunks(self, response: Any) -> dict:
        """流式：content 边收边回调，tool_calls 分片按 index 归并。"""
        text: list[str] = []
        slots: dict[int, dict] = {}
        for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            if delta.content:
                text.append(delta.content)
                if self.on_delta is not None:
                    self.on_delta(delta.content)
            for tc_delta in delta.tool_calls or ():
                slot = slots.setdefault(tc_delta.index or 0, {"id": "", "name": "", "args": ""})
                if tc_delta.id:
                    slot["id"] = tc_delta.id
                if tc_delta.function is not None:
                    if tc_delta.function.name:
                        slot["name"] = tc_delta.function.name
                    if tc_delta.function.arguments:
                        slot["args"] += tc_delta.function.arguments
        return {
            "text": "".join(text),
            "tool_calls": [self._call(slot) for _, slot in sorted(slots.items())],
        }

    def _from_message(self, message: Any) -> dict:
        """非流式：直接归一化 SDK 的 message（arguments 是 JSON 文本）。"""
        return {
            "text": message.content or "",
            "tool_calls": [
                {"id": tool_call.id or "", "name": tool_call.function.name,
                 "args": _parse_args(tool_call.function.arguments)}
                for tool_call in message.tool_calls or ()
            ],
        }

    @staticmethod
    def _call(slot: dict) -> dict:
        return {"id": slot["id"], "name": slot["name"], "args": _parse_args(slot["args"])}


def _parse_args(raw: str) -> dict:
    """tool_call 的 arguments 是 JSON 文本；解析失败（或不是对象）记 `{}`。"""
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _wire_messages(messages: list[dict]) -> list[dict]:
    """把 Session 投影出的 `messages` 译成 OpenAI 线格式（出站方向的形状适配）。

    Session 里的 tool_calls 是归一化形状 `{id, name, args}`（provider 无关，见
    `MockLLM.complete` 的契约）；到这里要转成 `{id, type, function: {name, arguments}}`，
    否则第二轮采样会被 API 打回。已是线格式的条目（legacy 落盘的会话）原样保留。
    """
    wire: list[dict] = []
    for message in messages:
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            wire.append(message)
            continue
        wire.append({**message, "tool_calls": [
            call if "function" in call else {
                "id": call.get("id", ""),
                "type": "function",
                "function": {
                    "name": call.get("name", ""),
                    "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                },
            }
            for call in calls
        ]})
    return wire


# ═══════════════ 真实联调 demo（会发起网络调用，不在测试里跑） ═══════════════
def _demo() -> None:
    """装配 harness + 真实 DeepSeek：一个 calculate 工具 + final_output 终结。

    `python miniharness_deepseek.py`（需要 config.json 里的凭据）
    """
    from miniharness import Loop, Session, ToolDefinition, ToolRuntime
    from miniharness_plugins import FinalOutputPlugin

    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)

    ctx = Context()
    ctx.provide("session", Session())
    loop = Loop()
    ctx.load(loop)
    ctx.load(ToolRuntime())
    ctx.load(FinalOutputPlugin())
    ctx.load(DeepSeekProvider(
        OpenAI(base_url=config["base_url"], api_key=config["api_key"]),
        "deepseek-v4-flash",
        on_delta=lambda text: print(text, end="", flush=True),
    ))

    runtime: ToolRuntime = ctx.get("tools")
    runtime.register(ToolDefinition(
        name="calculate", description="执行数学计算，输入数学表达式",
        parameters={"type": "object",
                    "properties": {"expression": {"type": "string", "description": "数学表达式"}},
                    "required": ["expression"]},
        execute=lambda args: str(eval(args["expression"])),  # noqa: S307 - demo only
    ))
    runtime.register(ToolDefinition(
        name="final_output", description="以结构化格式输出最终答案，调用即表示回答完成。",
        parameters={"type": "object",
                    "properties": {"result": {"type": "object", "description": "结构化结果"},
                                   "summary": {"type": "string", "description": "一句话总结"}},
                    "required": ["result"]},
        execute=lambda args: json.dumps(args, ensure_ascii=False),
    ))

    session: Session = ctx.get("session")
    print("🧠 ", end="", flush=True)
    answer = loop.turn("用 calculate 算出 156*23，再用 final_output 输出结果")
    print(f"\nturn -> {answer}")
    print("log  ->", [(e["seq"], e["type"]) for e in session.events])


if __name__ == "__main__":
    _demo()
