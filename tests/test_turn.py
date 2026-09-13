"""跨包集成（S2 主 seam）：turn 边界。

装配完整的 harness（Session + 工具 + provider + 策略插件）驱动 `Loop.turn`，
断言工具执行与落日志、deny 的配对完整性、终结工具收尾，以及
「只换插件就改变结局，而 Loop 零改动」。
"""
from __future__ import annotations

import json
import os
import time

import pytest

from capabilities.final_output.provider import FinalOutputPlugin
from capabilities.permission.provider import PermissionPlugin
from capabilities.persistence.definition import LOG_FORMAT, LOG_VERSION
from capabilities.persistence.provider import PersistenceConsumer, PersistenceManager, Store
from capabilities.timeout.provider import ToolTimeoutPlugin
from capabilities.tracing.provider import TraceConsumer
from miniharness.loop import (
    CHECKPOINT_MODEL,
    CHECKPOINT_STEP,
    CHECKPOINT_TOOL,
    Loop,
)
from miniharness.session import Session
from miniharness.session.test_projection import _event_types
from miniharness.tools.contract import ToolDefinition
from providers.mock import MockLLM
from tests.support import CALC_PARAMS, WRITE_PARAMS, _assemble, _scripted


# ═══════════════ S2 主 seam（集成）：turn 边界 ═══════════════
def test_turn_feeds_persistence_and_trace_consumers(tmp_path):
    llm = _scripted("double", {"n": 21}, "42")
    persistence = PersistenceConsumer(
        PersistenceManager(Store(str(tmp_path))), session_id="s1")
    tracer = TraceConsumer()
    _, session, loop, _ = _assemble(
        llm,
        plugins=[persistence, tracer],
        tools=[ToolDefinition("double", "翻倍", {"n": {"type": "integer"}},
                              lambda args: args["n"] * 2)],
    )

    answer = loop.turn("把 21 翻倍")

    assert answer == "42"
    # 持久化：磁盘上就是日志本身（同一个真相源），模型可见内容由它重放重建
    assert persistence.saves > 0
    stored = PersistenceManager(Store(str(tmp_path))).load_events("s1")
    assert stored == session.events
    assert Session(events=stored).derive_messages() == session.derive_messages()
    # 追踪：run → llm_call(工具调用) + tool_call 两个 span，内容取自日志
    assert tracer.run is not None and tracer.run.input == "把 21 翻倍"
    (llm_span, tool_span) = tracer.run.children
    assert llm_span.type == "llm_call"
    assert llm_span.output["tool_calls"] == [{"name": "double", "arguments": '{"n": 21}'}]
    assert tool_span.type == "tool_call"
    assert tool_span.input == {"tool": "double", "args": {"n": 21}} and tool_span.output == "42"


def test_turn_leaves_a_readable_event_log_with_monotonic_seq(tmp_path):
    """AC1：一次回合结束后，磁盘上是逐行可读的事件日志（带类型与单调序号），不是消息数组。"""
    persistence = PersistenceConsumer(
        PersistenceManager(Store(str(tmp_path))), session_id="s1")
    _, session, loop, _ = _assemble(MockLLM().then_text("在的"), plugins=[persistence])

    assert loop.turn("在吗") == "在的"

    header, *lines = Store(str(tmp_path)).log_path("s1") \
        .read_text(encoding="utf-8").splitlines()
    header = json.loads(header)
    assert header["format"] == LOG_FORMAT and header["version"] == LOG_VERSION
    assert header["session_id"] == "s1" and header["created"]
    records = [json.loads(line) for line in lines]
    assert records == session.events
    assert [r["seq"] for r in records] == sorted(r["seq"] for r in records)
    assert [r["type"] for r in records] == [
        "turn/start", "user/message", "assistant/message", "turn/end"]
    assert all("role" not in record for record in records)   # 没有并行的消息数组


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
    # 超时必须与成功可区分：权威结果带稳定的 `timed_out` 码，而不是把超时伪装成 ok 的字符串，
    # 也不再与「工具抛异常」混同一个 `error`（T5：超时 / 取消 / 被拒 / 失败四者各有其码）
    assert plain_result["status"] == "ok"
    assert timed_result["status"] == "timed_out"
    assert "超时" in timed_result["content"]
    assert timed_answer != plain_answer


# ═══════════════ T4：语义检查点（写盘屏障） ═══════════════
def _failing_fsync(fd: int) -> None:
    raise OSError("磁盘写失败")


def _persisted_harness(tmp_path, ran: list[dict]):
    """装配一个带持久化消费者与一个真实工具的 harness（检查点测试共用）。"""
    persistence = PersistenceConsumer(
        PersistenceManager(Store(str(tmp_path))), session_id="s1")
    llm = _scripted("double", {"n": 21}, "42")
    ctx, session, loop, _ = _assemble(
        llm, plugins=[persistence],
        tools=[ToolDefinition("double", "翻倍", {"n": {"type": "integer"}},
                              lambda args: ran.append(args) or args["n"] * 2)])
    return ctx, session, loop, persistence, llm


def test_each_semantic_checkpoint_commits_the_log_before_the_next_action(tmp_path):
    """三个检查点都在下游动作之前完成落盘：轮到策略时历史已在盘上。

    监听器按注册顺序在持久化之后收到检查点——轮到它时该检查点的屏障已经返回，
    `agent/checkpoint` 因此在每个语义点都可观察「已落盘」。
    """
    ctx, session, loop, _, _ = _persisted_harness(tmp_path, [])
    committed: list[str] = []

    def observe(payload: dict) -> None:
        durable = PersistenceManager(Store(str(tmp_path))).load_events("s1") == session.events
        committed.append(f"{payload['point']}:{'已落盘' if durable else '未落盘'}")

    ctx.on("agent/checkpoint", observe)

    loop.turn("把 21 翻倍")

    assert committed == [
        f"{CHECKPOINT_STEP}:已落盘",      # 第一步开始前：turn/start + user/message 已固定
        f"{CHECKPOINT_MODEL}:已落盘",     # 向模型发起请求前
        f"{CHECKPOINT_TOOL}:已落盘",      # 顶层工具派发前：带 tool_call 的 assistant 已固定
        f"{CHECKPOINT_STEP}:已落盘",      # 第二步开始前：tool/result 已固定
        f"{CHECKPOINT_MODEL}:已落盘",
    ]


def test_a_failed_checkpoint_blocks_the_model_request(tmp_path, monkeypatch):
    """检查点 fail-closed：屏障失败就不发起模型请求，也不派发工具。"""
    ran: list[dict] = []
    _, _, loop, _, llm = _persisted_harness(tmp_path, ran)

    monkeypatch.setattr(os, "fsync", _failing_fsync)
    with pytest.raises(OSError):
        loop.turn("把 21 翻倍")

    assert llm.calls == []            # 每步开始前的检查点失败：采样没发生
    assert ran == []                  # 工具体也没派发


def test_a_failed_tool_checkpoint_blocks_tool_dispatch(tmp_path, monkeypatch):
    """顶层工具派发前的检查点失败：模型请求已发生，但工具体不执行（副作用不发生）。"""
    ran: list[dict] = []
    _, _, loop, _, llm = _persisted_harness(tmp_path, ran)

    real_fsync = os.fsync
    seen = {"n": 0}

    def fail_after_first(fd: int) -> None:
        seen["n"] += 1
        if seen["n"] > 1:
            raise OSError("磁盘写失败")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_after_first)
    with pytest.raises(OSError):
        loop.turn("把 21 翻倍")

    assert len(llm.calls) == 1        # 前两个检查点已过，模型请求发生过
    assert ran == []                  # 工具派发前的检查点失败：工具体没执行
