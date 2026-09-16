"""loop —— 零策略的循环（消费方）。

`Loop` 只做「驱动 + 派发事件」：取输入 → `agent/pre-step` → 语义检查点 → llm seam
→ 语义检查点 → tools 管线 → 落日志。它是会话、工具、LLM 三个契约的**消费方**——
只依赖契约，不认识任何策略与后端。

权限 / 超时 / 重试 / 压缩 / 终结 / 持久化全部是订阅事件的插件（见 `capabilities/`）。
"""
from __future__ import annotations

from miniharness.core import Context, Plugin
from miniharness.llm.contract import LLM
from miniharness.session import Session
from miniharness.tools.contract import DENIED
from miniharness.tools.runtime import ToolRuntime

__all__ = [
    "Loop",
    "CHECKPOINT_STEP", "CHECKPOINT_MODEL", "CHECKPOINT_TOOL",
    "OUTCOME_DONE", "OUTCOME_TERMINAL", "OUTCOME_DENIED",
    "OUTCOME_REJECTED", "OUTCOME_MAX_STEPS", "OUTCOME_CANCELLED",
]

#: 语义检查点：`agent/checkpoint` 事件的 `point` 取值。
#:
#: 循环在**向外产生副作用或依赖历史之前**发出它——向外发起模型请求、派发工具、以及
#: 每步开始前。发出即让订阅者（检查点策略，见 `capabilities.persistence.provider`）
#: 把历史固定下来；订阅者抛错就让下游副作用不发生（fail-closed）。默认没有任何订阅者，
#: 检查点是空操作；`Loop` 自己不认识也不解释这些点。
CHECKPOINT_STEP = "step"      # 每步开始前
CHECKPOINT_MODEL = "model"    # 向模型发起请求前
CHECKPOINT_TOOL = "tool"      # 顶层工具派发前

#: 回合结局码：`turn()` 的返回值与 `turn/end` 事件的 `status` 取值，跨包契约
#: （`capabilities.final_output` / `capabilities.timeout` 等收尾方报告，`Loop` 忠实记录）。
OUTCOME_DONE = "done"            # 自然完成
OUTCOME_TERMINAL = "terminal"    # 终结工具收尾（由收尾方报告）
OUTCOME_DENIED = "denied"        # 工具调用被拒
OUTCOME_REJECTED = "rejected"    # pre-step 拒绝本轮
OUTCOME_MAX_STEPS = "max-steps"  # 步数用尽
OUTCOME_CANCELLED = "cancelled"  # 取消收尾（由收尾方报告）


class Loop(Plugin):
    """只做「驱动 + 派发事件」：取输入 → `agent/pre-step` → llm seam → tools 管线 → 落日志。

    每个工具结果入日志后派发 `agent/post-tool`（waterfall，默认 `{"continue": True}`）；
    监听者返回 `{"continue": False, "answer": ..., "status": ...}` 即以该 answer 收尾本轮，
    `status` 就是本轮结局码（缺省 `done`）。

    `Loop` 自主收尾（纯文本答复 / 工具调用被拒 / 步数用尽）之前派发 `agent/turn-stopping`
    （同样形如 post-tool 的 waterfall，默认 `{"continue": True, "text": <原样>}`，payload 的
    `ending` 是本要给出的结局码）——收尾策略在此改写收尾文本，或返回
    `{"continue": False, "status": ..., "text": ...}` 强制改判结局；`agent/post-tool` 与
    `agent/pre-step` 的收尾已是策略报告的结局，不再重复派发。纯文本路径采纳后的 `text`
    才落 `assistant/message` 与答复。

    `turn()` 返回结构化结局 `{"status": ..., "text": ...}`，`turn/end` 记录同一个 `status`
    （`OUTCOME_*` 常量）：`done`（自然完成）/ `terminal`（终结工具收尾，由收尾方报告）/
    `denied`（工具调用被拒）/ `rejected`（pre-step 拒绝本轮）/ `max-steps`（步数用尽）/
    `cancelled`（取消收尾，由收尾方报告）。谁收尾谁报告结局，`Loop` 只忠实记录——它不认识
    任何具体策略。

    中止结局与成功同走 `tool/result`（`status` 带稳定的结局码 `timed_out` / `cancelled` / `failed`）；
    只有 `denied` 另落 `tool/denied`——工具体没执行，没有权威结果，也不派发 `agent/post-tool`。

    循环体内没有任何权限、超时、重试、压缩判断——它们都是 `ctx.on(...)` 订阅者。

    在每步开始前、向模型发起请求前、顶层工具派发前各派发一次 `agent/checkpoint`
    （`point` 取 `CHECKPOINT_*`）：检查点策略在此把历史固定下来，订阅者抛错即中止本轮
    （fail-closed）——副作用不会发生在未固定的历史之上。
    """

    inject = ("session", "tools", "llm")

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        ctx.provide("loop", self)

    def _ending(self, session: Session, ending: str, text: str) -> dict[str, str]:
        """一次 `Loop` 自主收尾：派发 `agent/turn-stopping` 并解析结局。

        订阅者可改写文本放行（结局不变），或以 `{"continue": False, "status", "text"}` 强制
        改判；`ending` 是 `Loop` 本来要给的结局码，供订阅者辨认现场。只派发、不落日志——
        `assistant/message` 的落盘时机在调用方（采纳后的文本才进模型可见历史）。
        """
        stop = self._ctx.waterfall(
            "agent/turn-stopping",
            {"session": session, "ending": ending, "text": text},
            lambda p: {"continue": True, "text": p["text"]},
        )
        status = ending if stop.get("continue", True) else stop.get("status", ending)
        return {"status": status, "text": stop.get("text", text)}

    def turn(self, user_input: str, max_steps: int = 8) -> dict[str, str]:
        ctx = self._ctx
        session: Session = ctx.get("session")
        tools: ToolRuntime = ctx.get("tools")
        llm: LLM = ctx.get("llm")

        session.append("turn/start", input=user_input)

        # 派发 pre-step（插件可拒绝/改写输入），默认放行
        step = ctx.waterfall("agent/pre-step", {"input": user_input, "session": session},
                             lambda p: {"enter": True})
        if not step.get("enter", True):
            session.append("turn/end", status=OUTCOME_REJECTED)
            return {"status": OUTCOME_REJECTED, "text": step.get("reason", "(已拒绝)")}
        session.append("user/message", content=user_input)

        for step in range(max_steps):
            # 每步开始前 + 向模型发起请求前的检查点：请求依赖历史，先把历史固定下来
            ctx.emit("agent/checkpoint",
                     {"point": CHECKPOINT_STEP, "step": step, "session": session})
            ctx.emit("agent/checkpoint",
                     {"point": CHECKPOINT_MODEL, "step": step, "session": session})
            reply = llm.complete(session.derive_messages())   # 只依赖 llm seam
            text = reply.get("text") or ""
            calls = list(reply.get("tool_calls") or [])

            if not calls:
                # 收尾 seam：纯文本回复到达、回合收尾之前，收尾策略在此改写答复文本或强制
                # 改判结局（默认原样放行）。采纳后的文本才落日志与答复。
                outcome = self._ending(session, OUTCOME_DONE, text)
                session.append("assistant/message", content=outcome["text"], tool_calls=[])
                session.append("turn/end", status=outcome["status"])
                return outcome

            session.append("assistant/message", content=text, tool_calls=calls)

            # 先跑完整批（每个 tool_call 都必须落一条配对事件），再决定收尾。
            # 中途 return 会让后续调用永不落日志 → 下轮上行 tool_calls 配对不完整（400）。
            answer: str | None = None
            answer_status = OUTCOME_DONE
            denial: str | None = None
            for call in calls:
                # 顶层工具派发前的检查点：工具体会产生副作用，先把这次的调用固定下来
                ctx.emit("agent/checkpoint",
                         {"point": CHECKPOINT_TOOL, "step": step, "call": call,
                          "session": session})
                result = tools.run(call)                      # 只依赖 tools seam
                if result["status"] == DENIED:
                    # 被拒 → 工具体没执行、没有权威结果：不写 tool/result，但仍需落配对事件
                    session.append("tool/denied", name=result["name"],
                                   call_id=call.get("id", ""), reason=result["error"])
                    if denial is None:
                        denial = result["error"]
                    continue
                session.append("tool/result", name=result["name"], call_id=call.get("id", ""),
                               args=call.get("args") or {},
                               content=result["content"], status=result["status"])
                # 通用收尾 seam：终结策略在此短路本轮（Loop 不认识任何具体工具名）
                stop = ctx.waterfall(
                    "agent/post-tool",
                    {"session": session, "call": call, "result": result},
                    lambda p: {"continue": True},
                )
                if not stop.get("continue", True) and answer is None:
                    answer = stop.get("answer", "")
                    answer_status = stop.get("status", OUTCOME_DONE)

            if answer is not None:
                session.append("turn/end", status=answer_status)
                return {"status": answer_status, "text": answer}
            if denial is not None:
                # 被拒收尾同样过收尾 seam：取消策略据此把「取消后的全被拒」改判为 cancelled
                outcome = self._ending(session, OUTCOME_DENIED, denial)
                session.append("turn/end", status=outcome["status"])
                return outcome

        outcome = self._ending(session, OUTCOME_MAX_STEPS, "⚠️ 达到最大步数限制")
        session.append("turn/end", status=outcome["status"])
        return outcome
