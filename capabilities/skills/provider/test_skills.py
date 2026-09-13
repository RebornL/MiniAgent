"""包内测试：技能装载的可逆注册（`skills.provider`）。

装载即把技能工具挂进 `ToolRuntime`（可逆），卸载即撤销；meta 工具形状与 legacy 一致；
卸载插件时其全部注册（含运行期装载的技能与 meta 工具）逆序撤销。
"""
from __future__ import annotations

from capabilities.skills.definition import Skill
from capabilities.skills.provider import SkillRegistry
from miniharness.tools.runtime.test_pipeline import _pipeline


def test_skill_registry_load_is_reversible():
    def double(n: int) -> int:
        return n * 2

    skill = Skill(
        name="math", description="算数技能",
        tools=[{"type": "function", "function": {
            "name": "double", "description": "翻倍",
            "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}}],
        tool_map={"double": double},
    )
    registry = SkillRegistry()
    registry.register(skill)
    ctx, runtime = _pipeline(registry)

    assert runtime.get("double") is None                  # 未装载 → 工具不可见
    disposer = registry.load("math")
    assert runtime.run({"id": "c1", "name": "double", "args": {"n": 21}})["content"] == "42"
    # legacy 视图（SkillManager.get_active_tools / get_active_prompt）保持可用
    assert "double" in [t["function"]["name"] for t in registry.get_active_tools()]

    disposer()                                            # 卸载即撤销
    result = runtime.run({"id": "c2", "name": "double", "args": {"n": 21}})
    assert result["status"] == "error" and "工具未注册" in result["content"]

    # legacy meta 工具形状：模型可通过工具装载/卸载技能
    assert registry.load_skill("math").startswith("✅")
    assert runtime.run({"id": "c3", "name": "double", "args": {"n": 2}})["content"] == "4"
    assert runtime.run({"id": "c4", "name": "unload_skill", "args": {"name": "math"}})["content"].startswith("✅")
    assert runtime.run({"id": "c5", "name": "double", "args": {"n": 2}})["status"] == "error"
    assert "不存在" in registry.load_skill("nope")
    assert "未加载" in registry.unload_skill("math")

    # 卸载插件：其全部注册（含运行期装载的技能与 meta 工具）逆序撤销
    registry.load("math")
    assert ctx.unload(registry) is True
    assert runtime.get("double") is None
    assert runtime.get("load_skill") is None and runtime.get("unload_skill") is None
