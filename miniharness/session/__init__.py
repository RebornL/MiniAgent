"""session —— 会话事件日志与投影（原语 2）。

`Session` 是 append-only 的 typed 事件日志：`append()` 只增不改，`derive_messages()` 是
「模型可见历史」的投影，`replay()` 用已落盘的日志重放恢复，`session_from_messages()` 把
v0 的消息数组翻译成事件，`compact()` 把一次压缩写成可重放的 surface 替换记录。

本包不含任何执行逻辑，是能力契约里最稳定的一层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = ["Session", "COMPACTED_EVENT", "compaction_replacement"]

#: 压缩事件：被遮蔽的事件仍留在日志里，投影时以 `replacement` 代之。
COMPACTED_EVENT = "context/compacted"


def compaction_replacement(summary: str) -> list[dict]:
    """压缩事件的标准替换内容：把摘要渲染成一条模型可见的 user 消息。"""
    return [{"role": "user", "content": f"[上下文已压缩] {summary}"}]


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

    def compact(self, summary: str, shadowed_seqs: Iterable[int],
                replacement: Iterable[dict] | None = None) -> dict:
        """压缩 = surface 替换：被遮蔽的原始事件留在日志里，投影时以替换内容代之。

        事件记录**被遮蔽的事件范围**（`shadowed_seqs` + `shadowed_range`）与**替换内容**
        （`replacement`，模型可见消息），外加摘要本体（`summary`，下一次压缩的增量输入）。
        因此压缩后的历史可以只靠这条事件确定性地重建。
        """
        seqs = sorted(set(shadowed_seqs))
        return self.append(
            COMPACTED_EVENT,
            summary=summary,
            shadowed_seqs=seqs,
            shadowed_range={"start": seqs[0], "end": seqs[-1]} if seqs else None,
            replacement=[dict(message) for message in (
                replacement if replacement is not None else compaction_replacement(summary))],
        )

    def derive_messages(self) -> list[dict]:
        """把日志投影成模型可见的 messages（确定且幂等）。"""
        return [message for _, message in self.derive_entries()]

    def derive_entries(self) -> list[tuple[dict, dict]]:
        """带来源的投影：`(日志事件, 模型可见消息)` 对。

        压缩事件记录的 `shadowed_seqs` 决定遮蔽范围，`replacement` 决定该处投影出什么；
        被更晚的压缩重新遮蔽的旧替换内容不再投影（否则历次摘要会层层堆在投影里）。
        策略（如压缩）可据此定位要 surface 替换的 `seq`，而不用重新实现投影逻辑。
        """
        compactions = [event for event in self.events if event["type"] == COMPACTED_EVENT]
        masked: set[int] = set()
        replacements: dict[int, list[dict]] = {}
        # 逆序扫：`masked` 此时恰是「更晚的压缩」遮蔽过的 seq，据此判旧替换内容是否被取代
        for event in reversed(compactions):
            seqs = event.get("shadowed_seqs") or []
            if not seqs:
                continue
            anchor = min(seqs)
            if anchor not in masked:
                replacements[anchor] = list(event.get("replacement") or [])
            masked.update(seqs)

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
            for message in replacements.get(event["seq"], ()):
                # 替换内容落在被遮蔽区间的起始位置（日志尾部 append，投影时归位）
                entries.append((event, message))
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

    def replay(self, events: Iterable[dict]) -> None:
        """用已落盘的事件日志重放恢复会话。

        重放不是新事件：直接赋值（而非 append），因此不通知日志消费者。
        日志是权威源，恢复不做任何「逆投影」——逐字读回，逐字重现。
        """
        self.events = list(events)
        self.seq = self.events[-1]["seq"] if self.events else 0

    @classmethod
    def session_from_messages(cls, messages: list[dict]) -> "Session":
        """`derive_messages` 的逆：把模型可见的 messages 还原成事件日志。

        只用于把 v0 的历史格式（`messages.json` 的消息数组）翻译成事件日志，
        见 `capabilities.persistence.definition.translate`：
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
