"""validation —— 输出校验能力。

- 契约：无独立契约包（本能力无稳定契约符号，照 permission / final_output 先例）；
  三层防线语义：①schema 安全校验（防注入/防递归/防超大 schema，白名单 type）
  ②输出严格 schema 校验（最后防线）③注入检测（不静默删除，抛错）——实现与细节见 `provider`；
- 实现：`provider`（三层防线 + `ValidationPlugin`）；
- 消费方：`miniharness.tools.runtime`（消费被改写后的权威结果）、`app.tools`（直接用
  `sanitize_output` / `sanitize_string`）、`app.assembly`（装配 `ValidationPlugin`）。
"""