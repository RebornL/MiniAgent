"""skills.provider —— 技能装载的实现（Provider）。

`SkillRegistry` 把技能工具经 `ToolRuntime` 可逆注册（装载即挂、卸载即撤销），
复用契约包 `capabilities.skills.definition` 的技能目录与激活记录，
并暴露 legacy 的 `load_skill` / `unload_skill` 两个 meta 工具；两者的描述里列出
**当前可用 / 已加载**的技能名（legacy `{available_skills}` 行为），因此默认不装载的
技能（如 `shell`）也能被模型发现——这是发现技能的唯一入口。

装载/卸载同时写成 `skill/loaded` / `skill/unloaded` 事件：状态由日志决定，
`restore(events)` 折叠这些事件重建激活集（含工具注册与领域提示），不读任何旁路元数据。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from capabilities.skills.definition import (
    LOADED_EVENT,
    UNLOADED_EVENT,
    Skill,
    SkillManager,
    active_skills,
)
from miniharness.core import Context, Plugin
from miniharness.session import Session
from miniharness.tools.contract import ToolDefinition
from miniharness.tools.runtime import ToolRuntime

__all__ = ["SkillRegistry"]




# ═══════════════ 技能装载：SkillManager → 可逆注册 ═══════════════
class SkillRegistry(Plugin):
    """技能的可逆注册（原语 5）：装载即把技能工具挂进 ToolRuntime，卸载即撤销。

    复用 `SkillManager` 的技能目录/激活记录（`get_active_prompt` / `get_active_tools`
    语义不变），并暴露 legacy 的 `load_skill` / `unload_skill` 两个 meta 工具。
    每次装载/卸载追加一条事件，因此技能状态是日志的函数，而不是进程内的旁路。
    """

    inject = ("tools", "session")

    def __init__(self, manager: SkillManager | None = None) -> None:
        self.manager = manager or SkillManager()
        self._skills: dict[str, Skill] = {}   # SkillManager 不提供按名查询
        self._active: dict[str, Callable[[], None]] = {}
        self._meta_tools: dict[str, ToolDefinition] = {}   # 描述随技能目录与激活集刷新

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        self._tools: ToolRuntime = ctx.get("tools")
        self._session: Session = ctx.get("session")
        ctx.provide("skills", self)
        ctx.effect(self.unload_all)          # 插件卸载时撤销全部已装载技能（可逆注册）
        # meta 工具随插件卸载一并撤销。描述在这里留空、由 `_refresh_meta_tool_descriptions`
        # 填上当前可用 / 已加载的技能名——模型只看得见 meta 工具，这是技能的可发现性来源。
        self._meta_tools = {
            "load_skill": ToolDefinition(
                name="load_skill", description="",
                parameters={"type": "object", "properties": {"name": {"type": "string"}},
                            "required": ["name"]},
                execute=lambda args: self.load_skill(args.get("name", "")),
            ),
            "unload_skill": ToolDefinition(
                name="unload_skill", description="",
                parameters={"type": "object", "properties": {"name": {"type": "string"}},
                            "required": ["name"]},
                execute=lambda args: self.unload_skill(args.get("name", "")),
            ),
        }
        for tool in self._meta_tools.values():
            self._tools.register(tool)
        self._refresh_meta_tool_descriptions()

    def _refresh_meta_tool_descriptions(self) -> None:
        """把「可用技能 / 当前已加载」写进 meta 工具的**描述**（legacy `{available_skills}` 行为）。

        描述不再是硬编码的一句，而是随技能目录与激活集刷新（`ToolRuntime.specs()` 每次读的
        都是同一对象上的当前值）。这是模型发现技能的唯一入口：默认不装载的技能（如 `shell`）
        既不在 base prompt 里点名，装载前也不注入自己的提示，只能从这里点得到。
        """
        if not self._meta_tools:
            return
        available = ", ".join(self._skills) or "无"
        loaded = ", ".join(self.active_names()) or "无"
        self._meta_tools["load_skill"].description = (
            f"加载一个技能模块，其工具立即可用。可用技能: {available}")
        self._meta_tools["unload_skill"].description = (
            f"卸载一个技能模块，其工具立即撤销。当前已加载: {loaded}")

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill
        self.manager.register(skill)
        self._refresh_meta_tool_descriptions()

    def load(self, name: str) -> Callable[[], None]:
        """装载技能：可逆注册 + 记一条 `skill/loaded`；返回**会记日志**的卸载 disposer（幂等）。

        返回的口子走 `unload()`，因此「内存里撤下」与「日志里撤下」同步——否则调用者
        用这个句柄卸载后，日志仍说装着，重启（日志是权威源）就会把卸载撤销。
        是否真的装载过由 `_apply_load` 的返回值决定，重复装载不再写事件。
        """
        if self._apply_load(name) is not None:
            self._session.append(LOADED_EVENT, name=name)
        return lambda: self.unload(name)

    def unload(self, name: str) -> bool:
        """卸载技能：撤销注册 + 记一条 `skill/unloaded`；未装载则不动日志。"""
        if not self._apply_unload(name):     # 是否真的撤下了由 `_apply_unload` 决定
            return False
        self._session.append(UNLOADED_EVENT, name=name)
        return True

    def restore(self, events: Iterable[dict]) -> None:
        """从事件日志重放技能状态：折叠装载/卸载事件后重建工具注册与领域提示。

        日志里出现而已不存在的技能名（改名/删除）直接跳过，不影响其余技能恢复。
        """
        self.unload_all()
        for name in active_skills(events):
            if name in self._skills:
                self._apply_load(name)

    # ── 应用/撤销（静默：不改日志，供重放与插件卸载使用；也是「是否真的动了」的唯一判据）──
    def _apply_load(self, name: str) -> Callable[[], None] | None:
        """装载：注册工具并返回撤销 disposer；已装载则返回 `None`（没有真的装载）。

        不改日志——重放（`restore`）与插件卸载（`unload_all`）走这条路径；
        `load()` 也用它判定该不该记一条 `skill/loaded`，判定只在这一处。
        """
        if name in self._active:
            return None
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
        self._refresh_meta_tool_descriptions()   # 「当前已加载」随激活集刷新
        return self._ctx.effect(unload)

    def _apply_unload(self, name: str) -> bool:
        """卸载：撤下注册；返回「是否真的撤下了」（`unload()` 据此决定记不记事件）。

        不改日志——重放与插件卸载也走这条路径；判定只在这一处，避免日志记下一次
        并未真正发生的卸载。
        """
        disposer = self._active.get(name)
        if disposer is None:
            return False
        disposer()
        self._refresh_meta_tool_descriptions()   # 「当前已加载」随激活集刷新
        return True

    def unload_all(self) -> None:
        """撤销全部已装载技能（插件卸载与重放前的清场入口；不改日志）。"""
        for name in list(self._active):
            self._apply_unload(name)

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
        """当前已激活的技能名（进程内视图；权威状态是日志里的 `skill/*` 事件）。

        保留此读取口：它是 `load` / `unload` 的对偶——registry 自己的激活集视图，
        不经过 legacy `SkillManager` 镜像（`get_active_tools` / `get_active_prompt`
        读的是那份镜像）。因此它既是「这个 registry 现在认哪些技能」的天然读取口，
        也是重放断言的观测面：技能状态只由日志折叠重建，断言必须能直接看这份状态。
        """
        return sorted(self._active)

    def get_active_prompt(self) -> str:
        return self.manager.get_active_prompt()


def _bind_legacy(fn: Callable[..., Any]) -> Callable[[dict], Any]:
    """legacy 工具是 `fn(**args)`，ToolDefinition.execute 收一个 args dict。"""
    def execute(args: dict) -> Any:
        return fn(**args)
    return execute
