"""app.skeleton_demo —— 骨架的 smoke run：`python -m app.skeleton_demo`（离线，不联网）。

只装骨架（Context / Session / Loop / ToolRuntime / LLM 契约）+ mock provider + 一个示例审批策略，
用它证明「策略注入不改循环」与依赖驱动的激活顺序。
"""
from __future__ import annotations

from capabilities.permission.provider import PermissionPlugin
from miniharness.core import Context
from miniharness.loop import Loop
from miniharness.session import Session
from miniharness.tools.contract import ToolDefinition
from miniharness.tools.runtime import ToolRuntime
from providers.mock import MockLLM


# ═══════════════ 装配示例（smoke run：python -m app.skeleton_demo） ═══════════════
def _demo() -> None:
    """装配一个可运行的 miniharness：故意乱序装载，由依赖关系决定激活顺序。"""
    ctx = Context()
    ctx.provide("session", Session())

    loop, llm = Loop(), MockLLM()
    llm.then_tool_call("calculate", {"expression": "16 * 2"}).then_text("16 * 2 = 32")

    # 乱序装载：Loop 依赖未就绪 → pending；provide 后自动激活
    for name, plugin in (("loop", loop), ("permission", PermissionPlugin(denied={"write_file"})),
                         ("llm", llm), ("tools", ToolRuntime())):
        print(f"load {name:<10} -> {ctx.load(plugin)}")

    tools: ToolRuntime = ctx.get("tools")
    tools.register(ToolDefinition(
        name="calculate", description="算数", parameters={"expression": {"type": "string"}},
        execute=lambda a: eval(a["expression"]),  # noqa: S307 - demo only
    ))
    tools.register(ToolDefinition(
        name="write_file", description="写文件",
        parameters={"path": {"type": "string"}, "content": {"type": "string"}},
        execute=lambda a: "written",
    ))

    session: Session = ctx.get("session")
    print("tools.specs:", [s["function"]["name"] for s in tools.specs()])
    print("turn ->", loop.turn("16 * 2 是多少")["text"])
    print("log   ->", [(e["seq"], e["type"]) for e in session.events])

    llm2 = ctx.get("llm")
    llm2.script = [{"text": "", "tool_calls": [{"id": "c9", "name": "write_file",
                                                "args": {"path": "x", "content": "y"}}]}]
    print("turn ->", loop.turn("写个文件")["text"])


if __name__ == "__main__":
    _demo()
