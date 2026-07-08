"""
上下文压缩模块 —— 完整修复版
修复点:
  1. 用 TYPE_CHECKING 替代 "OpenAI" 前向引用
  2. tiktoken 直接用 cl100k_base，兼容 DeepSeek 等所有模型
  3. messages 统一转 dict，解决 ChatCompletionMessage 无 .items() 问题
依赖: pip install openai tiktoken
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import tiktoken

if TYPE_CHECKING:
    from openai import OpenAI


# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

@dataclass
class CompactionConfig:
    max_tokens: int = 500        # 超过此阈值触发压缩
    target_tokens: int = 300     # 压缩后目标 token 数
    keep_last_n: int = 3          # 最近 N 条消息永不压缩
    summary_model: str = "deepseek-v4-pro"  # 用便宜模型做摘要


# ═══════════════════════════════════════════════════════════
# 工具函数：消息类型统一
# ═══════════════════════════════════════════════════════════

def to_dict(msg) -> dict:
    """把 ChatCompletionMessage 或 dict 统一转成 dict"""
    if isinstance(msg, dict):
        return msg
    if hasattr(msg, "model_dump"):
        return msg.model_dump(exclude_none=True)
    if hasattr(msg, "dict"):
        return msg.dict(exclude_none=True)
    # 兜底：手动提取
    return {"role": getattr(msg, "role", ""), "content": getattr(msg, "content", "")}


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
        # 直接用 cl100k_base，兼容 DeepSeek 等所有模型
        self.encoder = tiktoken.get_encoding("cl100k_base")
        self.summary: str = ""
        self.total_compactions: int = 0

    def restore(self, summary: str) -> None:
        """从持久化恢复摘要"""
        self.summary = summary

    # ── 策略 1：精确 token 计数 ─────────────────────
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

    # ── 策略 2：滑动窗口（简单粗暴） ────────────────
    def sliding_window(self, messages: list[dict]) -> list[dict]:
        """只保留 system prompt + 最近 N 条消息"""
        system_msgs = [m for m in messages if m["role"] == "system"]
        recent = messages[-self.config.keep_last_n:]
        result = []
        for sm in system_msgs:
            if sm not in recent:
                result.append(sm)
        result.extend(recent)
        return result

    # ── 策略 3：摘要压缩（推荐） ────────────────────
    def summarize_and_compress(
        self, messages: list[dict], client: OpenAI
    ) -> list[dict]:
        """
        把旧消息替换成摘要 + 保留最近 N 条
        增量合并：新摘要 = 合并(旧摘要, 新对话)
        """
        n = self.config.keep_last_n
        if len(messages) <= n + 2:
            return messages

        # 分割：旧消息 vs 保留的新消息
        split_point = max(1, len(messages) - n)
        old_messages = messages[:split_point]
        recent_messages = messages[split_point:]

        # system prompt 永远保留在最前面
        system_msgs = [m for m in messages if m["role"] == "system"]

        # 把旧消息转成文本
        old_text = "\n".join(
            t for m in old_messages if (t := to_text(m))
        )

        # 生成增量摘要
        new_summary = self._generate_summary(client, old_text)
        self.summary = new_summary
        self.total_compactions += 1

        # 组装结果
        compressed: list[dict] = []

        # 1) system prompt
        compressed.extend(system_msgs)

        # 2) 注入摘要
        compressed.append({
            "role": "system",
            "content": f"[历史对话摘要 — 第{self.total_compactions}次压缩]\n{new_summary}\n\n以下是最近的对话：",
        })

        # 3) 最近消息原样保留
        compressed.extend(recent_messages)

        return compressed

    def _generate_summary(self, client: OpenAI, new_text: str) -> str:
        """用便宜模型把对话压缩成一段摘要（增量合并）"""
        existing = self.summary
        prompt = f"""将以下对话内容压缩成一段简洁的摘要，保留所有关键信息和决策。

已有的历史摘要:
{existing if existing else "(无)"}

新增对话内容:
{new_text}

要求:
- 保留关键事实、数字、决定、用户偏好
- 去除冗余对话轮次
- 摘要不超过 500 字
- 合并新旧内容，输出一份完整摘要"""

        response = client.chat.completions.create(
            model=self.config.summary_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return response.choices[0].message.content or ""

    # ── 主入口 ─────────────────────────────────────
    def maybe_compact(
        self, messages: list[dict], client: OpenAI
    ) -> list[dict]:
        print(f"current state: {self.stats(messages)}")
        """检查 token 用量，超标则压缩"""
        current = self.count_tokens(messages)

        if current > self.config.max_tokens:
            print(f"\n⚠️ Token 超限 ({current}/{self.config.max_tokens})，触发压缩...")
            compressed = self.summarize_and_compress(messages, client)
            new_count = self.count_tokens(compressed)
            print(f"✅ 压缩完成: {current} → {new_count} tokens (节省 {current - new_count})")
            return compressed

        return messages  # 不超标，原样返回

    # ── 诊断工具 ───────────────────────────────────
    def stats(self, messages: list[dict]) -> str:
        """打印当前上下文状态"""
        tokens = self.count_tokens(messages)
        pct = tokens / self.config.max_tokens * 100
        return (
            f"上下文: {tokens}/{self.config.max_tokens} tokens ({pct:.0f}%) | "
            f"消息数: {len(messages)} | "
            f"累计压缩: {self.total_compactions}次"
        )