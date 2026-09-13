"""skills.provider —— 技能装载的实现（Provider）。

`SkillRegistry` 把技能工具经 `ToolRuntime` 可逆注册（装载即挂、卸载即撤销），
复用契约包 `capabilities.skills.definition` 的技能目录与激活记录，
并暴露 legacy 的 `load_skill` / `unload_skill` 两个 meta 工具。
"""
from __future__ import annotations

from typing import Any, Callable

from capabilities.skills.definition import Skill, SkillManager
from miniharness.core import Context, Plugin
from miniharness.tools.contract import ToolDefinition
from miniharness.tools.runtime import ToolRuntime

__all__ = ["SkillRegistry"]




# ═══════════════ 技能装载：SkillManager → 可逆注册 ═══════════════
class SkillRegistry(Plugin):
    """技能的可逆注册（原语 5）：装载即把技能工具挂进 ToolRuntime，卸载即撤销。

    复用 `SkillManager` 的技能目录/激活记录（`get_active_prompt` / `get_active_tools`
    语义不变），并暴露 legacy 的 `load_skill` / `unload_skill` 两个 meta 工具。
    """

    inject = ("tools",)

    def __init__(self, manager: SkillManager | None = None) -> None:
        self.manager = manager or SkillManager()
        self._skills: dict[str, Skill] = {}   # SkillManager 不提供按名查询
        self._active: dict[str, Callable[[], None]] = {}

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        self._tools: ToolRuntime = ctx.get("tools")
        ctx.provide("skills", self)
        ctx.effect(self.unload_all)          # 插件卸载时撤销全部已装载技能（可逆注册）
        # meta 工具随插件卸载一并撤销
        self._tools.register(ToolDefinition(
            name="load_skill", description="加载一个技能模块，其工具立即可用",
            parameters={"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"]},
            execute=lambda args: self.load_skill(args.get("name", "")),
        ))
        self._tools.register(ToolDefinition(
            name="unload_skill", description="卸载一个技能模块，其工具立即撤销",
            parameters={"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"]},
            execute=lambda args: self.unload_skill(args.get("name", "")),
        ))

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill
        self.manager.register(skill)

    def load(self, name: str) -> Callable[[], None]:
        """装载技能：工具经 ToolRuntime 可逆注册；返回卸载 disposer（幂等）。"""
        existing = self._active.get(name)
        if existing is not None:
            return existing
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"技能 '{name}' 不存在。可用: {', '.join(self._skills)}")

        self.manager.load(name)                            # 复用 legacy 的激活记录
        disposers = []
        for tool_def in skill.tools:
            function = tool_def.get("function", {})
            fn = skill.tool_map.get(function.get("name", ""))
            if fn is None:
                continue
            disposers.append(self._tools.register(ToolDefinition(
                name=function.get("name", ""),
                description=function.get("description", ""),
                parameters=function.get("parameters", {}),
                execute=_bind_legacy(fn),
            )))

        def unload() -> None:
            for dispose in reversed(disposers):
                dispose()
            disposers.clear()
            self._active.pop(name, None)
            self.manager.unload(name)

        self._active[name] = unload
        return self._ctx.effect(unload)

    def unload(self, name: str) -> bool:
        disposer = self._active.get(name)
        if disposer is None:
            return False
        disposer()
        return True

    def unload_all(self) -> None:
        """撤销全部已装载技能（插件卸载时逆序 unwind 的入口）。"""
        for name in list(self._active):
            self.unload(name)

    # ── legacy meta 工具形状：返回给模型的提示字符串 ──
    def load_skill(self, name: str) -> str:
        try:
            self.load(name)
        except KeyError as exc:
            return str(exc.args[0])
        return f"✅ 已加载技能 '{name}'（{self._skills[name].description}）"

    def unload_skill(self, name: str) -> str:
        if not self.unload(name):
            return f"技能 '{name}' 当前未加载。"
        return f"✅ 已卸载技能 '{name}'"

    def get_active_tools(self) -> list[dict]:
        return self.manager.get_active_tools()

    def active_names(self) -> list[str]:
        """已激活技能名（供持久化恢复/保存会话时记录）。"""
        return sorted(self._active)

    def get_active_prompt(self) -> str:
        return self.manager.get_active_prompt()


def _bind_legacy(fn: Callable[..., Any]) -> Callable[[dict], Any]:
    """legacy 工具是 `fn(**args)`，ToolDefinition.execute 收一个 args dict。"""
    def execute(args: dict) -> Any:
        return fn(**args)
    return execute
