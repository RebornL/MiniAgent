"""tools.contract —— 工具契约（Definition）。

工具作者只声明 `ToolDefinition` 的字段；执行失败走结构化结果，而不是抛异常。
本包不含执行逻辑（那是 `tools.runtime` 的事），因此是工具能力里变动最慢的一层。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["ToolDefinition"]


@dataclass
class ToolDefinition:
    """工具契约：工具作者只声明这些字段，执行失败走结构化结果而非抛异常。"""

    name: str
    description: str
    parameters: dict
    execute: Callable[[dict], Any]
    timeout_ms: int = 0
    finalize_content: Callable[[Any], str] | None = None
