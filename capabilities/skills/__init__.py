"""skills —— 技能装载能力（一组工具 + 一段领域提示，按需装载 / 卸载）。

- 契约：`definition`（`Skill` / `SkillManager`：技能目录与激活记录）；
- 实现：`provider`（`SkillRegistry`，把技能工具经 ToolRuntime 可逆注册）；
- 消费方：`consumer`（`SystemPromptPlugin`：把已激活技能的提示同步进模型可见历史）
  与 `app.assembly`（恢复已激活技能）。
"""