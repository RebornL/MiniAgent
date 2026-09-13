"""skills.consumer —— 技能能力的消费方：把技能状态变成模型可见历史。

`SystemPromptPlugin` 只经 `skills` 契约（`get_active_prompt()`）读取已激活技能，
组合出 system prompt 并在变化时追加一条 `system/message`——它不装载技能、不改技能状态，
是这条能力的消费方而非实现方。
"""
from __future__ import annotations

from typing import Any, Callable

from miniharness.core import Context, Plugin
from miniharness.session import Session

__all__ = ["SystemPromptPlugin"]





class SystemPromptPlugin(Plugin):
    """system prompt 同步策略：技能装载后，其领域提示立刻进入模型可见历史。

    legacy 每步重算 system prompt 并覆写 `messages[0]`；Session 是 append-only 日志，
    这里改为「组合结果与日志里最后一条 `system/message` 不同时追加一条」。
    技能的装载/卸载都走工具调用，故订阅 `tools/result` 就能在**同一轮内**、下次采样前刷新；
    再订阅 `agent/pre-step` 覆盖每轮开始；`apply` 时的首次同步对应 legacy 的 `messages[0]`。
    """

    inject = ("session", "skills")

    def __init__(self, base_prompt: str = "") -> None:
        self.base_prompt = base_prompt

    def apply(self, ctx: Context) -> None:
        self._session: Session = ctx.get("session")
        self._skills = ctx.get("skills")
        self.sync()
        ctx.on("tools/result", self._on_event)
        ctx.on("agent/pre-step", self._pre)

    def compose(self) -> str:
        """base + 已激活技能的领域提示（与 legacy `build_system_prompt` 同形）。"""
        parts = [self.base_prompt]
        skill_prompt = self._skills.get_active_prompt()
        if skill_prompt:
            parts.append(f"\n\n--- 当前激活的技能 ---\n{skill_prompt}")
        return "\n".join(parts)

    def sync(self) -> None:
        """组合结果变化时追加一条 system/message；未变化则不动。"""
        prompt = self.compose()
        if not prompt:
            return
        for event in reversed(self._session.events):
            if event["type"] == "system/message":
                if event["content"] == prompt:
                    return
                break
        self._session.append("system/message", content=prompt)

    def _on_event(self, payload: dict) -> None:
        self.sync()

    def _pre(self, payload: dict, next_: Callable[[], Any]) -> dict:
        self.sync()
        return next_()
