"""compaction.provider —— 压缩策略的实现（Provider）。

`CompactionPlugin` 订阅 `agent/pre-step`：超阈值时把「被遮蔽的事件范围 + 替换内容 + 摘要」
写成一条 `context/compacted` 事件（surface 替换）；压缩事件的词汇在 `miniharness.session`，
投影语义（`compaction_summaries`）在契约包，阈值与摘要策略在本包。摘要函数可注入，
默认 `stub_summarizer`（确定性，不调 LLM）。

压缩状态本身（增量摘要链）只由日志重建：`restore(events)` 折叠日志里的压缩事件，
不读任何旁路元数据——重启后接着压缩时，`existing` 就是日志里最后一次压缩的摘要。

legacy 注记（原 `capabilities.compaction.definition` 模块文件头，逐字并入）：

    上下文压缩模块 —— 完整修复版
    修复点:
      1. 用 TYPE_CHECKING 替代 "OpenAI" 前向引用
      2. tiktoken 直接用 cl100k_base，兼容 DeepSeek 等所有模型
      3. messages 统一转 dict，解决 ChatCompletionMessage 无 .items() 问题
    依赖: pip install openai tiktoken
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import tiktoken

from capabilities.compaction.definition import compaction_summaries
from miniharness.core import Context, Plugin
from miniharness.session import Session

__all__ = ["CompactionConfig", "CompactionPlugin", "ContextManager", "stub_summarizer"]


# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

@dataclass
class CompactionConfig:
    max_tokens: int = 2000        # 超过此阈值触发压缩
    target_tokens: int = 500     # 压缩后目标 token 数
    keep_last_n: int = 5          # 最近 N 条消息永不压缩


# ═══════════════════════════════════════════════════════════
# 工具函数：消息类型统一
# ═══════════════════════════════════════════════════════════

def to_text(msg: dict) -> str:
    """单条消息转纯文本"""
    role = msg.get("role", "")
    content = msg.get("content", "")
    if not content:
        return ""
    mapping = {
        "user": f"用户: {content}",
        "assistant": f"助手: {content}",
        "tool": f"工具结果: {content}",
        "system": f"系统: {content}",
    }
    return mapping.get(role, f"{role}: {content}")


# ═══════════════════════════════════════════════════════════
# ContextManager
# ═══════════════════════════════════════════════════════════

class ContextManager:

    def __init__(self, config: CompactionConfig | None = None):
        self.config = config or CompactionConfig()
        # get_encoding 首次调用需联网下载编码文件，延迟到首次计数，离线环境才能完成装配启动
        self._encoder = None
        self.summary: str = ""
        self.total_compactions: int = 0

    @property
    def encoder(self) -> tiktoken.Encoding:
        """cl100k_base 编码器：首次访问才加载（get_encoding 需联网下载编码文件）。"""
        if self._encoder is None:
            self._encoder = tiktoken.get_encoding("cl100k_base")
        return self._encoder

    def restore(self, summaries: list[str]) -> None:
        """从事件日志重放恢复压缩状态（摘要链：末项是当前增量摘要，条数是累计压缩次数）。"""
        self.summary = summaries[-1] if summaries else ""
        self.total_compactions = len(summaries)

    # ── 精确 token 计数 ─────────────────────
    def count_tokens(self, messages: list[dict]) -> int:
        """计算 messages 列表的精确 token 数"""
        total = 0
        for msg in messages:
            total += 4  # 每条消息固定开销
            for key, value in msg.items():
                if isinstance(value, str):
                    total += len(self.encoder.encode(value))
                elif isinstance(value, list):
                    total += len(self.encoder.encode(str(value)))
        return total


# ═══════════════ 上下文治理：Compaction → agent/pre-step ═══════════════
def stub_summarizer(existing: str, new_text: str) -> str:
    """确定性摘要 stub（不调 LLM）：增量合并已有摘要 + 新对话首几行。

    输入 `(已有摘要, 新增对话文本)`，输出合并后的完整摘要。
    """
    lines = [line.strip() for line in new_text.splitlines() if line.strip()]
    parts = ([existing] if existing else []) + lines[:3]
    if len(lines) > 3:
        parts.append(f"…（共 {len(lines)} 行）")
    return " | ".join(parts)


class CompactionPlugin(Plugin):
    """上下文治理策略：订阅 `agent/pre-step`，超阈值时对 Session 做 surface 替换。

    阈值与 keep_last_n 来自 `CompactionConfig`；token 计数与压缩状态复用 `ContextManager`
    （`count_tokens` / `config` / `summary` / `total_compactions`）。切分点与增量合并语义在本插件。
    摘要函数可注入，默认 `stub_summarizer`（确定性，不调真实 LLM）。
    """

    inject = ("session",)

    def __init__(
        self,
        config: CompactionConfig | None = None,
        summarizer: Callable[[str, str], str] | None = None,
        context_manager: ContextManager | None = None,
    ) -> None:
        self.context = context_manager or ContextManager(config)
        self.summarizer = summarizer or stub_summarizer

    def apply(self, ctx: Context) -> None:
        self._session: Session = ctx.get("session")
        ctx.provide("compaction", self)      # 装配层按名字取用它做重放（restore）
        ctx.on("agent/pre-step", self._pre)

    def restore(self, events: Iterable[dict]) -> None:
        """从事件日志重放压缩状态：摘要链只由日志里的压缩事件重建，不读旁路元数据。"""
        self.context.restore(compaction_summaries(events))

    def _pre(self, payload: dict, next_: Callable[[], Any]) -> dict:
        self.compact_if_needed()
        return next_()

    def compact_if_needed(self) -> dict | None:
        """超阈值则压缩；返回 `context/compacted` 事件，未触发则返回 None。"""
        entries = self._session.derive_entries()
        messages = [message for _, message in entries]
        config = self.context.config

        if self.context.count_tokens(messages) <= config.max_tokens:
            return None                                    # 未超阈值：不动

        keep = config.keep_last_n
        if len(messages) <= keep + 2:
            return None                                    # 不足 keep_last_n + 2 条：没有可压缩的旧消息

        split = max(1, len(messages) - keep)
        # system prompt 永不压缩（legacy 也是把它们原样提到最前面）
        replaced = [event["seq"] for event, message in entries[:split]
                    if message["role"] != "system"]
        if not replaced:
            return None

        old_text = "\n".join(
            text for message in messages[:split] if (text := to_text(message))
        )
        summary = self.summarizer(self.context.summary, old_text)
        self.context.summary = summary                      # 增量合并语义：旧摘要进下一次合并
        self.context.total_compactions += 1
        return self._session.compact(summary, replaced)
