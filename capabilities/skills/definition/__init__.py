"""
Agent 核心完整实现 —— Skills 渐进加载
依赖: pip install openai tiktoken
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import tiktoken

if TYPE_CHECKING:
    from openai import OpenAI

@dataclass
class Skill:
    """一个 Skill = 一组工具 + 一段领域知识"""
    name: str
    description: str
    tools: list[dict]                    # OpenAI tool 定义
    tool_map: dict[str, Callable]        # 工具名 → 函数
    system_prompt: str = ""              # 激活时追加到 system prompt

class SkillManager:
    # load_skill / unload_skill 的 tool 定义（始终可用）
    META_TOOLS: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "加载一个技能模块。可用技能: {available_skills}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "要加载的技能名称"}
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "unload_skill",
                "description": "卸载一个技能模块。当前已加载: {loaded_skills}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "要卸载的技能名称"}
                    },
                    "required": ["name"],
                },
            },
        },
    ]

    def __init__(self):
        self._skills: dict[str, Skill] = {}
        self._active: set[str] = set()

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def load(self, name: str) -> str:
        if name not in self._skills:
            return f"技能 '{name}' 不存在。可用: {', '.join(self._skills.keys())}"
        if name in self._active:
            return f"技能 '{name}' 已经加载过了。"
        self._active.add(name)
        return f"✅ 已加载技能 '{name}'（{self._skills[name].description}）"

    def unload(self, name: str) -> str:
        if name not in self._active:
            return f"技能 '{name}' 当前未加载。"
        self._active.discard(name)
        return f"✅ 已卸载技能 '{name}'"

    def get_active_tools(self) -> list[dict]:
        """只返回当前激活 skill 的工具 + meta tools"""
        tools: list[dict] = []
        available = ", ".join(self._skills.keys())
        loaded = ", ".join(self._active) if self._active else "无"
        for mt in self.META_TOOLS:
            tool_def = json.loads(json.dumps(mt))
            tool_def["function"]["description"] = \
                tool_def["function"]["description"].format(
                    available_skills=available, loaded_skills=loaded
                )
            tools.append(tool_def)
        for name in self._active:
            tools.extend(self._skills[name].tools)
        return tools

    def get_active_tool_map(self) -> dict[str, Callable]:
        tm = {"load_skill": self.load, "unload_skill": self.unload}
        for name in self._active:
            tm.update(self._skills[name].tool_map)
        return tm

    def get_active_prompt(self) -> str:
        parts = []
        for name in self._active:
            p = self._skills[name].system_prompt
            if p:
                parts.append(f"# {name}\n{p}")
        return "\n\n".join(parts)

    def stats(self) -> str:
        loaded = ", ".join(self._active) if self._active else "无"
        return f"Skills: {len(self._active)}/{len(self._skills)} 已加载 [{loaded}]"

    def reset(self) -> None:
        """清空所有已激活的 skill（切换会话时用）"""
        self._active.clear()