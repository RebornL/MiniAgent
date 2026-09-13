"""session —— 会话事件日志与投影（原语 2）。

`Session` 是 append-only 的 typed 事件日志：`append()` 只增不改，`derive_messages()` 是
「模型可见历史」的投影，`session_from_messages()` 是其逆投影，`compact()` 做 surface 替换。

本包不含任何执行逻辑，是能力契约里最稳定的一层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = ["Session"]


@dataclass
class Session:
    """会话 = append-only 的 typed 事件日志；模型历史是 `derive_messages()` 的投影。

    - `append(type, **data)` 只增不改，历史可重放、可审计；
    - `compact()` 做 **surface 替换**（日志保留原始事件，仅在投影时遮蔽被替换区间）；
    - 凡进模型的内容必然先入日志（Model-visible means logged）。
    """

    events: list[dict] = field(default_factory=list)
    seq: int = 0
    _subscribers: list[Callable[[dict], None]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.events:
            self.seq = max(self.seq, self.events[-1]["seq"])

    def append(self, type: str, **data: Any) -> dict:
        """只增不改：追加一条事件并返回它。"""
        self.seq += 1
        event = {"seq": self.seq, "type": type, **data}
        self.events.append(event)
        for fn in tuple(self._subscribers):   # 消费者观察日志，不修改日志
            fn(event)
        return event

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """订阅后续追加的事件（持久化/追踪等日志消费者），返回退订 disposer。"""
        self._subscribers.append(fn)

        def dispose() -> None:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

        return dispose

    def compact(self, summary: str, replaced_seqs: Iterable[int]) -> dict:
        """压缩 = surface 替换：被替换的原始事件留在日志里，投影时以摘要代之。"""
        return self.append("context/compacted", summary=summary,
                           replaced_seqs=sorted(set(replaced_seqs)))

    def derive_messages(self) -> list[dict]:
        """把日志投影成模型可见的 messages（确定且幂等）。"""
        return [message for _, message in self.derive_entries()]

    def derive_entries(self) -> list[tuple[dict, dict]]:
        """带来源的投影：`(日志事件, 模型可见消息)` 对。

        策略（如压缩）可据此定位要 surface 替换的 `seq`，而不用重新实现投影逻辑。
        """
        masked: set[int] = set()
        anchors: dict[int, list[str]] = {}
        for event in self.events:
            if event["type"] != "context/compacted":
                continue
            seqs = event.get("replaced_seqs") or []
            masked.update(seqs)
            if seqs:
                anchors.setdefault(min(seqs), []).append(event["summary"])

        # system prompt 是「状态」而非「历史」：多条 system/message 只投影最后一条，且恒置最前。
        # legacy 每步覆写 messages[0]；把 system 留在原位置会产生中位 system 消息，
        # 部分 OpenAI 兼容接口会拒绝这种排列。
        latest_system: tuple[dict, dict] | None = None
        for event in self.events:
            if event["type"] == "system/message" and event["seq"] not in masked:
                latest_system = (event, {"role": "system", "content": event["content"]})

        entries: list[tuple[dict, dict]] = []
        if latest_system is not None:
            entries.append(latest_system)
        for event in self.events:
            for summary in anchors.get(event["seq"], ()):
                # 摘要落在被替换区间的起始位置（日志尾部 append，投影时归位）
                entries.append((event, {"role": "user", "content": f"[上下文已压缩] {summary}"}))
            if event["seq"] in masked:
                continue
            kind = event["type"]
            if kind == "system/message":
                continue
            if kind == "user/message":
                entries.append((event, {"role": "user", "content": event["content"]}))
            elif kind == "assistant/message":
                message = {"role": "assistant", "content": event.get("content", "")}
                if event.get("tool_calls"):
                    message["tool_calls"] = event["tool_calls"]
                entries.append((event, message))
            elif kind == "tool/result":
                entries.append((event, {"role": "tool", "content": event["content"],
                                        "tool_call_id": event.get("call_id", "")}))
            elif kind == "tool/denied":
                # 被拒的调用没有权威结果，但模型仍需看到这次观察（保持 tool_calls 配对完整）
                entries.append((event, {"role": "tool", "content": event["reason"],
                                        "tool_call_id": event.get("call_id", "")}))
        return entries

    def restore(self, messages: list[dict]) -> None:
        """把持久化的 messages 还原进日志（`session_from_messages` 的逆投影）。

        恢复的是「这些历史已经发生过」的那段日志：直接赋值（而非 append），
        以免把重放当成新事件通知日志消费者。
        """
        restored = self.session_from_messages(messages)
        self.events = restored.events
        self.seq = restored.seq

    @classmethod
    def session_from_messages(cls, messages: list[dict]) -> "Session":
        """`derive_messages` 的逆：把模型可见的 messages 还原成事件日志。

        用于恢复持久化的会话（`Persistence.load_session` 只存 messages）：
        `session_from_messages(msgs).derive_messages() == msgs`。
        """
        session = cls()
        for message in messages:
            role = message.get("role")
            if role == "system":
                session.append("system/message", content=message.get("content", ""))
            elif role == "user":
                session.append("user/message", content=message.get("content", ""))
            elif role == "assistant":
                session.append("assistant/message", content=message.get("content", ""),
                               tool_calls=list(message.get("tool_calls") or []))
            elif role == "tool":
                session.append("tool/result", content=message.get("content", ""),
                               call_id=message.get("tool_call_id", ""))
        return session
