"""app.deepseek_demo —— 真实联调 demo：`python -m app.deepseek_demo`（会联网，需要 config.json）。

装配真实 DeepSeek provider + final_output 终结插件，跑通一次 turn：
真实网络调用 + 真实工具执行 + 日志投影。装配认识所有族，因此这段联调归 `app/`。
"""
from __future__ import annotations

import json

from openai import OpenAI

from capabilities.final_output.provider import FinalOutputPlugin
from miniharness.core import Context
from miniharness.loop import Loop
from miniharness.session import Session
from miniharness.tools.contract import ToolDefinition
from miniharness.tools.runtime import ToolRuntime
from providers.deepseek import DeepSeekProvider


# ═══════════════ 装配示例（真实联调：python -m app.deepseek_demo） ═══════════════
def _demo() -> None:
    """装配 harness + 真实 DeepSeek：一个 calculate 工具 + final_output 终结。

    真实联调 demo：`python -m app.deepseek_demo`（需要 config.json 里的凭据）
    """
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
    outcome = loop.turn("用 calculate 算出 156*23，再用 final_output 输出结果")
    print(f"\nturn -> {outcome['text']}")
    print("log  ->", [(e["seq"], e["type"]) for e in session.events])


if __name__ == "__main__":
    _demo()
