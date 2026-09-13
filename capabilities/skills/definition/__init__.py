"""skills.definition —— 技能装载的契约（低频）。

- 事件词汇：`LOADED_EVENT` / `UNLOADED_EVENT`（`skill/loaded` / `skill/unloaded`）——
  装载 / 卸载是日志里的一等事件，技能状态可由日志重放；
- 投影：`active_skills(events)`——从事件日志按序折叠装载 / 卸载事件，恢复已激活的
  技能名（纯函数、可重放）；
- 形状：`Skill`——一个技能 = 一组工具 + 一段领域知识（dataclass，形状即契约）。

装载实现（`SkillManager` 的注册 / 装载 / 卸载、`SkillRegistry` 的可逆注册与
`skill/*` 事件记录）见 `capabilities.skills.provider`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

__all__ = ["LOADED_EVENT", "UNLOADED_EVENT", "active_skills", "Skill"]


# ═══════════════════════════════════════════════════════════════
# 技能事件词汇表：装载 / 卸载是日志里的一等事件（状态可重放）
# ═══════════════════════════════════════════════════════════════
LOADED_EVENT = "skill/loaded"
UNLOADED_EVENT = "skill/unloaded"


def active_skills(events: Iterable[dict]) -> list[str]:
    """从事件日志投影技能状态：按序折叠装载/卸载事件（纯函数、可重放）。

    状态只由日志决定，因此恢复不需要任何旁路元数据；返回排序后的名字，
    与 provider 侧 `SkillManager` 的 `get_active_tools()` / `get_active_prompt()` 的稳定性一致。
    """
    active: list[str] = []
    for event in events:
        name = event.get("name")
        kind = event.get("type")
        if kind == LOADED_EVENT and name not in active:
            active.append(name)
        elif kind == UNLOADED_EVENT and name in active:
            active.remove(name)
    return sorted(active)


@dataclass
class Skill:
    """一个 Skill = 一组工具 + 一段领域知识"""
    name: str
    description: str
    tools: list[dict]                    # OpenAI tool 定义
    tool_map: dict[str, Callable]        # 工具名 → 函数
    system_prompt: str = ""              # 激活时追加到 system prompt