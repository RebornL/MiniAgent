"""validation —— 输出校验能力。

- 契约：`definition`（`sanitize_output` / `validate_output` 的注入检测与 schema 校验）；
- 实现：`provider`（`ValidationPlugin`，订阅 `tools/post-execute`）；
- 消费方：`miniharness.tools.runtime`（消费被改写后的权威结果）。
"""