"""包内测试：技能装载的可逆注册（`skills.provider`）。

装载即把技能工具挂进 `ToolRuntime`（可逆），卸载即撤销；meta 工具形状与 legacy 一致；
卸载插件时其全部注册（含运行期装载的技能与 meta 工具）逆序撤销；
装载/卸载写成 `skill/loaded` / `skill/unloaded` 事件，技能状态可由日志重放重建。
"""
from __future__ import annotations

from capabilities.skills.definition import Skill
from capabilities.skills.provider import SkillRegistry
from miniharness.session import Session
from miniharness.tools.contract import FAILED
from miniharness.tools.runtime.test_pipeline import _pipeline


def _math_skill(system_prompt: str = "") -> Skill:
    """测试用技能 `math`：一个 `double` 工具 + 可选的领域提示。"""
    def double(n: int) -> int:
        return n * 2

    return Skill(
        name="math", description="算数技能",
        tools=[{"type": "function", "function": {
            "name": "double", "description": "翻倍",
            "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}}],
        tool_map={"double": double},
        system_prompt=system_prompt,
    )


def test_skill_registry_load_is_reversible():
    registry = SkillRegistry()
    registry.register(_math_skill())
    ctx, runtime = _pipeline(registry)

    assert runtime.get("double") is None                  # 未装载 → 工具不可见
    disposer = registry.load("math")
    assert runtime.run({"id": "c1", "name": "double", "args": {"n": 21}})["content"] == "42"
    # legacy 视图（SkillManager.get_active_tools / get_active_prompt）保持可用
    assert "double" in [t["function"]["name"] for t in registry.get_active_tools()]

    disposer()                                            # 卸载即撤销
    result = runtime.run({"id": "c2", "name": "double", "args": {"n": 21}})
    assert result["status"] == FAILED and "工具未注册" in result["content"]

    # legacy meta 工具形状：模型可通过工具装载/卸载技能
    assert registry.load_skill("math").startswith("✅")
    assert runtime.run({"id": "c3", "name": "double", "args": {"n": 2}})["content"] == "4"
    assert runtime.run({"id": "c4", "name": "unload_skill", "args": {"name": "math"}})["content"].startswith("✅")
    assert runtime.run({"id": "c5", "name": "double", "args": {"n": 2}})["status"] == FAILED
    assert "不存在" in registry.load_skill("nope")
    assert "未加载" in registry.unload_skill("math")

    # 卸载插件：其全部注册（含运行期装载的技能与 meta 工具）逆序撤销
    registry.load("math")
    assert ctx.unload(registry) is True
    assert runtime.get("double") is None
    assert runtime.get("load_skill") is None and runtime.get("unload_skill") is None


def test_the_meta_tools_advertise_the_available_and_loaded_skills():
    """F2：模型只看得见 meta 工具时也能发现技能——描述里列出可用 / 已加载的技能名。

    默认不装载的技能（如装配层的 `shell`）既不在 base prompt 里点名，装载前也不注入自己的
    提示，所以「可用技能」只能从 `load_skill` 的描述里发现（legacy `{available_skills}` 行为）。
    """
    registry = SkillRegistry()
    registry.register(_math_skill())
    ctx, runtime = _pipeline(registry)

    def descriptions() -> dict[str, str]:
        return {spec["function"]["name"]: spec["function"]["description"]
                for spec in runtime.specs()}

    assert "math" in descriptions()["load_skill"]         # 未装载，但点得到
    assert "math" not in descriptions()["unload_skill"]   # 可卸载的是「已加载」集，不是目录

    registry.load("math")

    assert "math" in descriptions()["unload_skill"]       # 装载后：可卸载
    registry.unload("math")
    assert "math" not in descriptions()["unload_skill"]   # 卸载后：刷新回未加载


def test_skill_state_is_rebuilt_from_the_log_alone():
    """装载/卸载是一等事件：新 registry 只折叠同一份日志就能重建工具注册与领域提示。"""
    registry = SkillRegistry()
    registry.register(_math_skill("你拥有算数能力。"))
    ctx, runtime = _pipeline(registry)
    session: Session = ctx.get("session")

    registry.load("math")

    # 装载写成事件：日志里有名字，没有旁路名单
    assert session.events == [{"seq": 1, "type": "skill/loaded", "name": "math"}]

    # 「重启」：新 registry + 新 Session，只把同一份日志重放进去
    fresh = SkillRegistry()
    fresh.register(_math_skill("你拥有算数能力。"))
    _, fresh_runtime = _pipeline(fresh)
    fresh.restore(session.events)

    assert fresh.active_names() == ["math"]                       # 状态从日志恢复
    assert fresh_runtime.get("double") is not None                # 工具注册被重建
    assert "你拥有算数能力。" in fresh.get_active_prompt()          # 领域提示被重建
    assert "double" in [t["function"]["name"] for t in fresh.get_active_tools()]

    # 卸载同样进日志；重放这份日志（装载 + 卸载）得到空状态
    assert registry.unload("math") is True
    assert session.events[-1] == {"seq": 2, "type": "skill/unloaded", "name": "math"}

    replayed = SkillRegistry()
    replayed.register(_math_skill("你拥有算数能力。"))
    _, replayed_runtime = _pipeline(replayed)
    replayed.restore(session.events)

    assert replayed.active_names() == []
    assert replayed_runtime.get("double") is None


def test_the_disposer_from_load_writes_the_unload_to_the_log():
    """F2 回归：`load()` 返回的句柄与 `unload()` 同一条路径——内存撤下，日志也记撤下。

    否则调用者用句柄卸载后日志仍说装着（日志是权威源），重启时这份卸载就会被撤销。
    """
    registry = SkillRegistry()
    registry.register(_math_skill())
    ctx, runtime = _pipeline(registry)
    session: Session = ctx.get("session")

    disposer = registry.load("math")
    disposer()

    assert session.events == [
        {"seq": 1, "type": "skill/loaded", "name": "math"},
        {"seq": 2, "type": "skill/unloaded", "name": "math"},
    ]
    assert runtime.get("double") is None                  # 内存里也真撤下了

    disposer()                                            # 幂等：没有第二次卸载事件
    assert len(session.events) == 2

    # 「重启」：只重放同一份日志，被卸载的技能不会复活
    fresh = SkillRegistry()
    fresh.register(_math_skill())
    _, fresh_runtime = _pipeline(fresh)
    fresh.restore(session.events)

    assert fresh.active_names() == []
    assert fresh_runtime.get("double") is None
