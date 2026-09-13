"""miniharness 的测试 —— 严格限定在 miniharness-spec.md §Testing Decisions 的三个 seam：

- **S2 主 seam（集成）**：`Loop.turn` 边界（mock provider 驱动的一次 turn）
- **S3 辅助 seam（契约）**：`ToolRuntime` 工具执行流水线
- **S5 辅助 seam（纯函数）**：`Session.derive_messages` 投影

事件总线原语与插件装载/disposer 是框架原语，按规格不在此设 seam。
运行：`python -m pytest test_miniharness.py -v`
"""
from __future__ import annotations

import json
import time

from miniharness import (
    Context,
    Loop,
    MockLLM,
    PermissionPlugin,
    Session,
    ToolDefinition,
    ToolRuntime,
)
from miniharness_plugins import FinalOutputPlugin, ToolTimeoutPlugin

CALC_PARAMS = {"expression": {"type": "string"}}
WRITE_PARAMS = {"path": {"type": "string"}, "content": {"type": "string"}}


def _assemble(llm: MockLLM, *, plugins=(), tools=(), session: Session | None = None):
    """装配一个最小 harness：Loop 先装载（依赖未就绪 → pending），依赖齐后自动激活。"""
    ctx = Context()
    session = session if session is not None else Session()
    ctx.provide("session", session)
    loop, runtime = Loop(), ToolRuntime()
    ctx.load(loop)
    ctx.load(runtime)
    ctx.load(llm)
    for plugin in plugins:
        ctx.load(plugin)
    for tool in tools:
        runtime.register(tool)
    return ctx, session, loop, runtime


def _event_types(session: Session) -> list[str]:
    return [event["type"] for event in session.events]


# ═══════════════ S2 主 seam（集成）：turn 边界 ═══════════════
def test_turn_executes_tool_and_logs_result_then_answers():
    calls: list[dict] = []

    def calculate(args: dict) -> str:
        calls.append(args)
        return str(eval(args["expression"]))  # noqa: S307 - 测试用固定表达式

    llm = (MockLLM()
           .then_tool_call("calculate", {"expression": "16 * 2"})
           .then_text("16 * 2 = 32"))
    _, session, loop, _ = _assemble(
        llm, tools=[ToolDefinition("calculate", "算数", CALC_PARAMS, calculate)])

    answer = loop.turn("16 * 2 是多少")

    assert answer == "16 * 2 = 32"
    assert calls == [{"expression": "16 * 2"}]          # 工具体被执行
    assert _event_types(session) == [                    # tool/result 按序入日志
        "turn/start", "user/message", "assistant/message",
        "tool/result", "assistant/message", "turn/end",
    ]
    (result,) = [e for e in session.events if e["type"] == "tool/result"]
    assert result["name"] == "calculate" and "32" in result["content"]
    # 第二跳采样时，模型确实看到了工具结果（Model-visible means logged）
    assert any(m["role"] == "tool" and "32" in m["content"] for m in llm.calls[1])


def test_permission_plugin_denies_and_tool_result_is_not_logged():
    ran: list[dict] = []
    llm = (MockLLM()
           .then_tool_call("write_file", {"path": "x.txt", "content": "y"})
           .then_text("不应走到这一步"))
    _, session, loop, _ = _assemble(
        llm,
        plugins=[PermissionPlugin(denied={"write_file"})],
        tools=[ToolDefinition("write_file", "写文件", WRITE_PARAMS,
                              lambda a: ran.append(a) or "written")],
    )

    answer = loop.turn("把 y 写进 x.txt")

    assert "write_file" in answer                        # turn 返回拒绝
    assert ran == []                                     # 工具体未执行
    assert "tool/result" not in _event_types(session)     # 权威结果未入日志
    assert len(llm.calls) == 1                           # 被拒后不再采样
    # 被拒的调用没有权威结果，但模型可见历史里 tool_calls 仍然配对完整
    assert session.derive_messages()[-1] == {
        "role": "tool", "content": answer, "tool_call_id": "call_1"}


def test_turn_finishes_the_whole_batch_before_a_terminal_tool_ends_it():
    """终结工具与普通工具同批：两个都执行且都配对，答复取终结工具的 content。"""
    ran: list[dict] = []

    def calculate(args: dict) -> str:
        ran.append(args)
        return "32"

    def final_output(args: dict) -> str:
        return json.dumps({"result": args["result"]}, ensure_ascii=False)

    llm = MockLLM()
    llm.script.append({"text": "", "tool_calls": [
        {"id": "c1", "name": "final_output", "args": {"result": {"n": 32}}},
        {"id": "c2", "name": "calculate", "args": {"expression": "16 * 2"}},
    ]})
    _, session, loop, _ = _assemble(
        llm,
        plugins=[FinalOutputPlugin({"final_output"})],
        tools=[ToolDefinition("calculate", "算数", CALC_PARAMS, calculate),
               ToolDefinition("final_output", "终结", {}, final_output)],
    )

    answer = loop.turn("算 16*2 并输出")

    assert answer == final_output({"result": {"n": 32}})   # 终结工具的 content 即本轮答复
    assert ran == [{"expression": "16 * 2"}]               # 排在终结工具之后的普通工具也没被丢弃
    results = [e for e in session.events if e["type"] == "tool/result"]
    assert sorted(e["call_id"] for e in results) == ["c1", "c2"]   # 同批 tool_calls 全部配对
    assert [m["tool_call_id"] for m in session.derive_messages()
            if m["role"] == "tool"] == ["c1", "c2"]
    assert len(llm.calls) == 1                             # 终结后不再采样


def test_turn_pairing_survives_a_denial_earlier_in_the_same_batch():
    """被拒的调用不能中止本批：后续 tool_call 照常执行且全部配对。"""
    ran: list[dict] = []
    llm = MockLLM()
    llm.script.append({"text": "", "tool_calls": [
        {"id": "c1", "name": "write_file", "args": {"path": "x.txt", "content": "y"}},
        {"id": "c2", "name": "calculate", "args": {"expression": "1 + 1"}},
    ]})
    _, session, loop, _ = _assemble(
        llm,
        plugins=[PermissionPlugin(denied={"write_file"})],
        tools=[ToolDefinition("write_file", "写文件", WRITE_PARAMS,
                              lambda a: ran.append(a) or "written"),
               ToolDefinition("calculate", "算数", CALC_PARAMS,
                              lambda a: ran.append(a) or "2")],
    )

    answer = loop.turn("写文件再算 1+1")

    assert "write_file" in answer                          # 本轮以首个拒绝原因收尾
    assert ran == [{"expression": "1 + 1"}]                # 被拒的工具体没执行，后面的执行了
    assert [(e["type"], e["call_id"]) for e in session.events
            if e["type"] in ("tool/result", "tool/denied")] == [
        ("tool/denied", "c1"), ("tool/result", "c2")]
    # 模型可见历史里两个 tool_call 都配上了 role=tool
    assert [m["tool_call_id"] for m in session.derive_messages() if m["role"] == "tool"] == ["c1", "c2"]
    assert len(llm.calls) == 1                             # 拒绝后不再采样


def test_policy_plugin_changes_turn_outcome_without_changing_loop():
    """同一个 Loop，只换装配的插件，turn 的可观察结局不同。"""

    def slow_tool(args: dict) -> str:
        time.sleep(0.3)
        return "慢工具完成"

    def run_turn(plugins) -> tuple[str, list[dict]]:
        llm = MockLLM().then_tool_call("slow", {})
        # 让 provider 把最后一次模型可见内容当作最终答复 → 结局可观察
        llm.script.append(lambda messages: {"text": f"最终观察: {messages[-1]['content']}"})
        tool = ToolDefinition("slow", "慢工具", {}, slow_tool)
        _, session, loop, _ = _assemble(llm, plugins=plugins, tools=[tool])
        assert isinstance(loop, Loop)                     # 两次跑的是同一个 Loop 实现
        return loop.turn("调一下慢工具"), session.events

    plain_answer, plain_log = run_turn([])
    timed_answer, timed_log = run_turn([ToolTimeoutPlugin(default_ms=50)])

    assert plain_answer == "最终观察: 慢工具完成"
    (plain_result,) = [e for e in plain_log if e["type"] == "tool/result"]
    (timed_result,) = [e for e in timed_log if e["type"] == "tool/result"]
    # 超时必须与成功可区分：权威结果是 error，而不是把超时伪装成 ok 的字符串
    assert plain_result["status"] == "ok"
    assert timed_result["status"] == "error"
    assert "超时" in timed_result["content"]
    assert timed_answer != plain_answer


# ═══════════════ S3 辅助 seam（契约）：工具执行流水线 ═══════════════
def _runtime_with(*tools: ToolDefinition) -> tuple[Context, ToolRuntime]:
    ctx = Context()
    ctx.provide("session", Session())
    runtime = ToolRuntime()
    ctx.load(runtime)
    for tool in tools:
        runtime.register(tool)
    return ctx, runtime


def test_tool_runtime_deny_skips_execute_body():
    ran: list[dict] = []
    ctx, runtime = _runtime_with(
        ToolDefinition("danger", "危险工具", {}, lambda a: ran.append(a) or "done"))
    ctx.on("tools/pre-execute",
           lambda payload, next_: {"kind": "deny", "reason": "危险工具被拒绝"})

    result = runtime.run({"id": "c1", "name": "danger", "args": {}})

    assert result == {"status": "denied", "name": "danger", "reason": "危险工具被拒绝"}
    assert ran == []


def test_tool_runtime_guard_cannot_loosen_a_deny():
    ran: list[dict] = []
    ctx, runtime = _runtime_with(
        ToolDefinition("danger", "危险工具", {}, lambda a: ran.append(a) or "done"))
    ctx.on("tools/pre-execute", lambda payload, next_: {"kind": "deny", "reason": "策略拒绝"})
    ctx.on("tools/guard", lambda payload, decision: {"kind": "allow"})   # 试图反向放行

    result = runtime.run({"id": "c1", "name": "danger", "args": {}})

    assert result["status"] == "denied"
    assert ran == []


def test_tool_runtime_ask_needs_approver_and_errors_are_structured():
    ran: list[dict] = []

    def boom(args: dict) -> str:
        raise ValueError("坏了")

    ctx, runtime = _runtime_with(
        ToolDefinition("moderate", "中等风险", {}, lambda a: ran.append(a) or "done"),
        ToolDefinition("boom", "会炸", {}, boom),
    )
    ctx.on("tools/pre-execute", lambda payload, next_: {"kind": "ask", "reason": "需要批准"})

    # 无审批者 → 默认拒绝（安全侧），工具体不执行
    ask_result = runtime.run({"id": "c1", "name": "moderate", "args": {}})
    assert ask_result["status"] == "denied" and ask_result["reason"] == "需要批准"
    assert ran == []

    # 有审批策略放行 → 工具体执行
    ctx.on("tools/approve", lambda payload, next_: {"kind": "allow"})
    ok_result = runtime.run({"id": "c2", "name": "moderate", "args": {}})
    assert ok_result["status"] == "ok" and ok_result["content"] == "done"
    assert len(ran) == 1

    # 工具抛异常 → 结构化失败结果，不崩整轮
    error_result = runtime.run({"id": "c3", "name": "boom", "args": {}})
    assert error_result["status"] == "error"
    assert "ValueError" in error_result["error"] and error_result["content"] == error_result["error"]


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

    session.compact("此前讨论了第一问与第一答", [first_ask["seq"], first_answer["seq"]])

    # 日志只增不改：原始事件原样保留
    assert session.events[:len(raw_log)] == raw_log
    # 投影：摘要落在被替换区间的位置，被替换内容不再进模型
    assert [m["content"] for m in session.derive_messages()] == [
        "你是助手", "[上下文已压缩] 此前讨论了第一问与第一答", "第二问",
    ]
    # 用压缩前的日志重放，仍能重建出同样的模型可见历史
    assert Session(events=raw_log).derive_messages() == before
