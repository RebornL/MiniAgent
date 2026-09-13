"""tools.contract —— 工具契约（Definition）。

工具作者只声明 `ToolDefinition` 的字段；执行失败走结构化结果，而不是抛异常。
本包不含执行逻辑（那是 `tools.runtime` 的事），因此是工具能力里变动最慢的一层。

工具的**结局词汇表**也在这里：一次执行以 `ok` 正常收尾，否则是四种**中止结局**
（超时 / 取消 / 被拒 / 失败）之一。四者带稳定错误码、与成功同构；策略（重试、呈现）
按码决策，而不是去猜异常类型。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "ToolDefinition",
    "AbortOutcome",
    "ABORT_CODES",
    "OK",
    "TIMED_OUT",
    "CANCELLED",
    "DENIED",
    "FAILED",
]


#: 正常收尾的结局码。
OK = "ok"

#: 超过时限仍未返回。本层只给结局码；终止受管范围是策略层的事（见 `capabilities.timeout.provider`）。
TIMED_OUT = "timed_out"
#: 上层请求取消（用户中断、回合被放弃）。
CANCELLED = "cancelled"
#: 被策略拒绝：工具体**未曾启动**，没有权威结果。
DENIED = "denied"
#: 执行出错：工具体抛出了异常。
FAILED = "failed"

#: 四种中止结局的错误码（`ok` 之外的 `status` 取值即出于此）。
ABORT_CODES = (TIMED_OUT, CANCELLED, DENIED, FAILED)


@dataclass
class ToolDefinition:
    """工具契约：工具作者只声明这些字段，执行失败走结构化结果而非抛异常。"""

    name: str
    description: str
    parameters: dict
    execute: Callable[[dict], Any]
    timeout_ms: int = 0
    finalize_content: Callable[[Any], str] | None = None


@dataclass(frozen=True)
class AbortOutcome:
    """一次工具执行的**中止结局**：策略在 `tools/execute` seam 上短路时返回它。

    中止与成功走同一条结果通道——`ToolRuntime` 把它规范化成
    `{"status": <code>, "name", "error", "content"}`，因此调用方不需要接异常，
    重试策略也只需读 `code` 就能决定是否重试。
    """

    code: str
    error: str

    def __post_init__(self) -> None:
        if self.code not in ABORT_CODES:
            raise ValueError(f"未知的中止结局码: {self.code!r}（应为 {ABORT_CODES} 之一）")
