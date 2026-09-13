"""validation.provider —— 输出校验的实现（Provider）。

`ValidationPlugin` 订阅 `tools/post-execute`，复用契约包 `capabilities.validation.definition`
的注入检测与 schema 校验；校验不通过则把权威结果改写为结构化失败（而非抛异常）。
"""
from __future__ import annotations

from typing import Any, Callable

from capabilities.validation.definition import sanitize_output, validate_output
from miniharness.core import Context, Plugin
from miniharness.tools.contract import FAILED, OK

__all__ = ["ValidationPlugin"]




# ═══════════════ 输出校验：Validation → tools/post-execute ═══════════════
class ValidationPlugin(Plugin):
    """输出校验策略：订阅 `tools/post-execute`，复用契约包 `capabilities.validation.definition`
    的 `sanitize_output` / `validate_output`（注入检测与 schema 校验）。

    校验不通过则把权威结果改写为结构化失败（而非抛异常）。
    """

    inject = ("tools",)

    def __init__(self, schemas: dict[str, dict] | None = None) -> None:
        self.schemas = dict(schemas or {})

    def apply(self, ctx: Context) -> None:
        ctx.on("tools/post-execute", self._post)

    def _post(self, payload: dict, next_: Callable[[], Any]) -> dict:
        result = next_()
        if result.get("status") != OK:
            return result
        name = result["name"]
        try:
            value = sanitize_output(result.get("value"))
            schema = self.schemas.get(name)
            if schema is not None:
                value = validate_output(value, schema)
        except ValueError as exc:
            return {"status": FAILED, "name": name, "error": f"输出校验失败: {exc}"}
        return {**result, "value": value}
