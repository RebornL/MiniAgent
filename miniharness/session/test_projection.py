"""包内测试（S5 辅助 seam，纯函数）：Session 事件日志与投影。

只依赖 `miniharness.session`：投影确定且幂等、只含 model-visible 事件、压缩是 surface 替换
且原日志可重放、`session_from_messages` 往返等价、订阅者按序观察日志且不改投影。
"""
from __future__ import annotations

from miniharness.session import Session


def _event_types(session: Session) -> list[str]:
    return [event["type"] for event in session.events]


# ═══════════════ S5 辅助 seam（纯函数）：Session 投影 ═══════════════
def test_derive_messages_is_deterministic_and_only_model_visible():
    session = Session()
    session.append("system/message", content="你是助手")
    session.append("turn/start", input="你好")
    session.append("user/message", content="你好")
    session.append("assistant/message", content="在的")
    session.append("turn/end", status="done")

    messages = session.derive_messages()

    assert messages == session.derive_messages()          # 确定且幂等
    assert messages == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在的"},
    ]
    assert all("turn/" not in str(m) for m in messages)   # 非 model-visible 事件不入投影

    session.append("assistant/message", content="",
                   tool_calls=[{"id": "c1", "name": "calculate", "args": {"expression": "1+1"}}])
    session.append("tool/result", name="calculate", call_id="c1", content="2", status="ok")
    tail = session.derive_messages()[-2:]

    assert tail[0]["tool_calls"][0]["name"] == "calculate"
    assert tail[1] == {"role": "tool", "content": "2", "tool_call_id": "c1"}


def test_derive_messages_keeps_only_the_latest_system_prompt():
    """system prompt 是状态不是历史：多条 system/message 只投影最后一条，且恒置最前。

    中位 system 消息会被部分 OpenAI 兼容接口拒绝；legacy 也恒为「开头单条 system」。
    """
    session = Session()
    session.append("system/message", content="旧提示")
    session.append("user/message", content="你好")
    session.append("assistant/message", content="在的")
    session.append("system/message", content="新提示（含技能）")   # 技能装载后刷新
    session.append("user/message", content="再问")

    messages = session.derive_messages()

    assert messages[0] == {"role": "system", "content": "新提示（含技能）"}
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert not any(m["content"] == "旧提示" for m in messages)


def test_session_from_messages_round_trips_model_visible_history():
    """`session_from_messages` 是 `derive_messages` 的逆（恢复持久化会话的路径）。"""
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "16 * 2 是多少"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "name": "calculate", "args": {"expression": "16 * 2"}}]},
        {"role": "tool", "content": "32", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "16 * 2 = 32"},
    ]

    session = Session.session_from_messages(messages)

    assert session.derive_messages() == messages          # 往返等价（含 tool_calls 与 role=tool）
    assert _event_types(session) == [
        "system/message", "user/message", "assistant/message", "tool/result", "assistant/message",
    ]


def test_compaction_is_surface_replacement_and_log_rebuilds_history():
    session = Session()
    session.append("system/message", content="你是助手")
    first_ask = session.append("user/message", content="第一问")
    first_answer = session.append("assistant/message", content="第一答")
    session.append("user/message", content="第二问")
    before = session.derive_messages()
    raw_log = list(session.events)

    compacted = session.compact("此前讨论了第一问与第一答",
                                [first_ask["seq"], first_answer["seq"]])

    # 压缩事件记下「被遮蔽的事件范围」与「替换内容」，摘要本体另存（下一次压缩的输入）
    assert compacted["shadowed_seqs"] == [first_ask["seq"], first_answer["seq"]]
    assert compacted["shadowed_range"] == {"start": first_ask["seq"], "end": first_answer["seq"]}
    assert compacted["replacement"] == [
        {"role": "user", "content": "[上下文已压缩] 此前讨论了第一问与第一答"}]
    assert compacted["summary"] == "此前讨论了第一问与第一答"
    # 日志只增不改：被遮蔽的原始事件原样保留
    assert session.events[:len(raw_log)] == raw_log
    # 投影：替换内容落在被遮蔽区间的起始位置，被遮蔽内容不再进模型
    assert [m["content"] for m in session.derive_messages()] == [
        "你是助手", "[上下文已压缩] 此前讨论了第一问与第一答", "第二问",
    ]
    # 投影只由日志算出：把它重放进一个新 Session 结果逐字相同（无内存旁路）
    assert Session(events=list(session.events)).derive_messages() == session.derive_messages()
    # 用压缩前的日志重放，仍能重建出同样的模型可见历史
    assert Session(events=raw_log).derive_messages() == before


def test_a_later_compaction_supersedes_the_replacement_it_shadows():
    """第二次压缩覆盖了上一次的替换内容：投影里只留最新的那份，不层层堆叠。"""
    session = Session()
    session.append("system/message", content="你是助手")
    ask = session.append("user/message", content="第一问")
    answer = session.append("assistant/message", content="第一答")
    second = session.append("user/message", content="第二问")
    session.compact("第一份摘要", [ask["seq"], answer["seq"]])
    third = session.append("user/message", content="第三问")

    # 第二次压缩连同上一次的替换内容一起遮蔽（锚点即第一条被遮蔽事件的 seq）
    session.compact("第二份摘要（已含第一份）",
                    [ask["seq"], answer["seq"], second["seq"], third["seq"]])

    assert [m["content"] for m in session.derive_messages()] == [
        "你是助手", "[上下文已压缩] 第二份摘要（已含第一份）",
    ]


# ═══════════════ S5 辅助 seam（纯函数）：日志与投影 ═══════════════
def test_session_subscribers_observe_log_in_order_without_mutating_projection():
    session = Session()
    seen: list[str] = []
    dispose = session.subscribe(lambda event: seen.append(event["type"]))

    session.append("turn/start", input="你好")
    session.append("user/message", content="你好")
    session.append("tool/denied", name="write_file", call_id="c1", reason="被拒绝")
    dispose()
    session.append("assistant/message", content="结束")

    assert seen == ["turn/start", "user/message", "tool/denied"]
    # 被拒的调用没有权威结果，但模型仍能看到这次观察，且 tool_calls 配对完整
    assert session.derive_messages() == [
        {"role": "user", "content": "你好"},
        {"role": "tool", "content": "被拒绝", "tool_call_id": "c1"},
        {"role": "assistant", "content": "结束"},
    ]
