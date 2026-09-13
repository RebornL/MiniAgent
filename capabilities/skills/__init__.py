"""skills —— 技能装载能力（一组工具 + 一段领域提示，按需装载 / 卸载）。

- 契约：`definition`（`Skill` / `SkillManager`，以及 `skill/loaded` / `skill/unloaded`
  事件词汇与 `active_skills(events)` 投影）；
- 实现：`provider`（`SkillRegistry`，把技能工具经 ToolRuntime 可逆注册并记事件）；
- 消费方：`consumer`（`SystemPromptPlugin`：把已激活技能的提示同步进模型可见历史）
  与 `app.assembly`（重放 `skill/*` 事件恢复技能状态）。
"""
