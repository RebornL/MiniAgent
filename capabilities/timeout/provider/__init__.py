"""timeout.provider —— 工具超时 / 取消的实现（Provider）。

`ToolTimeoutPlugin` 订阅 `tools/execute`（around），在**本次工具调用期间**把 `process` seam
换成一层登记代理：工具体经 `ctx.get("process")` 起的每个受管范围都记在册上。超时到来时，它按
受管范围契约终止整棵进程树（`terminate` 返回即范围已退出），等工具体跑到静止，然后返回结构化的
`AbortOutcome(TIMED_OUT)`——与成功同构，不靠异常表达，由 `ToolRuntime` 规范化进同一条结果通道。
默认超时沿用契约包 `capabilities.timeout.definition` 的 `DEFAULT_TOOL_TIMEOUT`；宽限期沿用受管
范围契约的 `DEFAULT_GRACE_MS`。

**终止手段随平台，宽限档不是普适承诺**：POSIX 后端先 `SIGTERM` 全组、宽限期满仍不退再
`SIGKILL`；**Windows 后端没有升级档**（Job Object 的 `TerminateJobObject` 一次强杀到底），
`grace_ms` 在那里只剩「等工具体静止的上限」这一层含义。

**取消走同一条路径**（`cancel()`，同时以 `ctx.get("abort")` 暴露给取消源）：区别只在结局码
——`cancelled` 而非 `timed_out`。取消源在装配层：`app.cli.InterruptSource` 把**回合执行期间**
的 Ctrl-C 换成一次 `cancel()`（见 `app/cli.py`、`docs/miniharness.md`）。取消源是主线程上的
信号处理，因此等待者**按片**检查中止请求（`_WAIT_SLICE_S`）、本插件的锁**可重入**。

**一次取消粘到本轮**，不只作用在终止那一刻在跑的调用上：`cancel()` 登记的本轮取消由
`TurnCancelPlugin`（同包）消费——本轮余下的工具调用一律拒绝、本轮不得以一次看起来正常的
`done` 蒙混收场；本插件 `_wrap` 的**入口**复查让重试重入 `tools/execute` 也不再起进程树。
回合边界由 `agent/pre-step` 划出（新回合不背上一回合的取消），`Loop` 因此仍是零改动。

**接线口为什么在这一层**：工具体在调用时刻经 `ctx.get("process")` 取 seam（见
`providers/README.md`），工具自己不认识终止；于是策略层能在服务上包一层，把「记录本次调用起
的范围 / 请求终止」接起来，而不动工具、也不动沙箱。

**边界**：没有受管范围可终止的工具体（纯 Python 的慢工具）仍是旧边界——它跑在 **daemon**
线程里，被放弃的工具体仍会跑到底，Python 杀不掉线程；等它静止的上限是 `grace_ms`，等满仍未
静止就带着「已尽力」返回（见 `docs/miniharness.md`）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from capabilities.timeout.definition import DEFAULT_TOOL_TIMEOUT
from miniharness.core import Context, Plugin
from miniharness.llm.contract import LLM
from miniharness.process.contract import (
    DEFAULT_GRACE_MS,
    ManagedRange,
    ProcessSeam,
    TerminationError,
)
from miniharness.tools.contract import CANCELLED, TIMED_OUT, AbortOutcome

__all__ = ["ToolTimeoutPlugin", "TurnCancelPlugin"]


@dataclass(frozen=True)
class _Abort:
    """一次中止请求：超时与取消共用同一条终止路径，区别只在结局码与说明。"""

    code: str
    reason: str


#: 等待者的唤醒粒度（秒）。取消源是**主线程上的信号处理**（`app.cli.InterruptSource`），
#: 而信号可能被递到别的线程上——那时 Python 级的处理函数只在主线程回到字节码时才跑。等待者
#: 若拿着整个时限一段睡满，取消请求要等时限到才看得见（实测：Windows 上 30 秒上限、0.5 秒
#: 递到的信号，整段睡满时在 10 秒后才被处理）。按片检查让主线程每片回到一次字节码，挂起的
#: 处理函数随即执行、随即 `notify`；代价是长命令期间每秒二十次空转的谓词检查，可忽略。
_WAIT_SLICE_S = 0.05


class _Invocation(ProcessSeam):
    """一次工具调用的中止册：`process` 服务的代理 + 本轮受管范围的登记 + 中止请求。

    - `spawn` 只多记一笔就把范围原样交给工具体，工具察觉不到策略层在观察它；
    - `request` 登记中止请求（超时 / 取消，先到者胜）并叫醒等待者；
    - `wait` 等这次调用收场：工具体先跑完 → `None`；中止请求先到（或到了时限）→ 该请求；
    - `terminate_all` 按受管范围契约终止整棵树，并独立确认范围真的空了。

    中止请求先到之后才冒出来的范围在**当场**被终止：否则一个后知后觉的工具体能在终止之后
    又拉起一棵树，留成孤儿。
    """

    def __init__(self, seam: ProcessSeam | None, grace_ms: int, name: str | None) -> None:
        self._seam = seam                       # `None` 即这一次调用没有受管范围可终止
        self._grace_ms = grace_ms
        self.name = name
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._ranges: list[ManagedRange] = []
        self._abort: _Abort | None = None
        self._terminating = False

    # ── `process` 服务的代理面 ─────────────────────
    def spawn(self, argv: Sequence[str], **kwargs: Any) -> ManagedRange:
        assert self._seam is not None, "没有受管范围 seam 时本对象不会被装成 process 服务"
        range_ = self._seam.spawn(argv, **kwargs)
        with self._cond:
            late = self._terminating
            if not late:
                self._ranges.append(range_)
        if late:                                 # 迟到者不留活口（terminate 幂等）
            range_.terminate(self._grace_ms)
        return range_

    # ── 中止：超时与取消的公共路径 ─────────────────
    def request(self, code: str, reason: str) -> None:
        """登记一次中止请求并叫醒等待者；先到者胜，重复请求不覆盖。"""
        with self._cond:
            if self._abort is None:
                self._abort = _Abort(code, reason)
            self._cond.notify_all()

    def wake(self) -> None:
        """工具体收场：叫醒等待者——它等的是「工具体完成」与「中止请求」两者之一。

        少了这一下，赶上「工具体先完成、等待者已经睡下」的时序，等待者会一直睡到时限：
        快速工具明明已经跑完，调用方却要等满整个超时才拿到它的结果。
        """
        with self._cond:
            self._cond.notify_all()

    def wait(self, finished: threading.Event, timeout_s: float,
             on_deadline: _Abort) -> _Abort | None:
        """等这次调用收场：工具体先跑完 → `None`；中止请求先到（或到了时限）→ 该请求。

        **完成优先**：工具体已经跑完就返回它的结局，迟到的取消不再改写——重写一个已经做成的
        工作不是取消该有的语义。时限一到则与取消同路（`on_deadline`），两者不会互相覆盖。

        **按片等**（`_WAIT_SLICE_S`）：取消源是主线程上的信号处理，而信号未必递到主线程——
        整段睡满会让取消请求等到时限才可见（见 `_WAIT_SLICE_S`）。分片不改变判定顺序：
        每片只重新看一眼「工具体完成了吗 / 中止请求到了吗 / 时限到了吗」。
        """
        deadline = time.monotonic() + max(timeout_s, 0.0)
        with self._cond:
            while True:
                if finished.is_set():
                    return None
                if self._abort is None:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._cond.wait(min(remaining, _WAIT_SLICE_S))
                        continue
                    self._abort = on_deadline    # 时限已到：与取消同一条路径
                return self._abort

    def terminate_all(self) -> None:
        """终止本次调用登记的全部受管范围，并独立确认它们真的已经退出。

        `terminate` 的契约是「返回时范围已退出」；这里再 `poll()` 一遍独立确认：终止没达成
        目的必须让调用方看见（抛 `TerminationError`），不能把「其实还活着」报成中止结局。
        """
        with self._cond:
            self._terminating = True             # 与快照同一次加锁：此后的 spawn 自会当场终止
            batch = list(self._ranges)
        for range_ in batch:
            range_.terminate(self._grace_ms)     # 终止（POSIX：宽限 → 必要时强杀）；返回即已退出
        for range_ in batch:
            if range_.poll() is None:
                raise TerminationError(
                    f"受管范围 {range_.pid} 在终止后仍在跑：不能把「还活着」报成中止结局")


# ═══════════════ 耐用性：超时 / 取消 → tools/execute ═══════════════
class ToolTimeoutPlugin(Plugin):
    """超时 / 取消护栏：订阅 `tools/execute`（around），终止本次调用的受管范围并给出结构化结局。

    与 legacy `CallFunc.call_with_timeout` 的**有意差别**（用户裁定）：超时不再伪装成一个 `ok`
    的字符串结果，也不再是与其他错误混同的 `error`——权威结果带稳定的 `timed_out` 码。取消走
    同一条终止路径、给 `cancelled` 码；下游（重试、呈现）按码决策：`timed_out` 可重试，
    `cancelled` 不重试。默认超时沿用 `DEFAULT_TOOL_TIMEOUT`；工具可用 `timeout_ms` 覆盖。

    `grace_ms` 有两处用途：传给 `terminate` 的宽限期（POSIX 后端据此升级到强杀；Windows 后端
    一次强杀到底，终止这一步用不到它），以及终止之后等工具体收尾的上限（等它静止才让中止结局
    替换它的结果）。取消入口 `cancel()` 以 `ctx.get("abort")` 一并暴露——它同时是**本轮取消
    状态**的登记处（`turn_cancel_reason`），`TurnCancelPlugin` 与 `_wrap` 的入口复查都读它。

    工具体一律跑在 **daemon** 线程里，因此卡死的工具不会拖住进程退出——受管范围能终止，而
    Python 线程杀不掉，纯 Python 的工具体仍只能停止等待。
    """

    inject = ("tools",)

    def __init__(self, default_ms: int = DEFAULT_TOOL_TIMEOUT * 1000,
                 grace_ms: int = DEFAULT_GRACE_MS) -> None:
        self.default_ms = default_ms
        self.grace_ms = grace_ms
        self._ctx: Context | None = None
        # 可重入：取消源可能是**主线程上的信号处理**，它会落在主线程任意一条字节码上——
        # 包括 `_wrap` 持着这把锁的那两处临界区；非重入锁会在那里自锁，进程再也回不来。
        self._lock = threading.RLock()
        self._active: _Invocation | None = None     # 工具调用在 Loop 里是顺序的，至多一个在跑
        self._turn_cancel: _Abort | None = None     # 本轮的取消请求：粘到本轮，`pre-step` 清除

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        ctx.on("tools/execute", self._wrap)
        ctx.on("agent/pre-step", self._new_turn)   # 回合边界：新回合不背上一回合的取消
        ctx.provide("abort", self)             # 取消请求的接线口（超时与取消同一路径）

    def cancel(self, reason: str = "调用方请求取消") -> bool:
        """请求取消：终止**当前在跑**的调用，并把这次取消**粘到本轮**。

        返回值只回答「有没有在跑的调用接下了请求」（没有就是幂等的 no-op）；粘住本轮的那一半
        与它无关——没有在跑的调用也照样粘（返回 `False`），于是本轮余下部分不再执行新的工具
        调用（`TurnCancelPlugin` 的 `tools/guard`），也不再起新的进程树（本插件 `_wrap` 的
        入口复查，重试重入 `tools/execute` 走的正是这条路）。回合边界由 `agent/pre-step` 划出：
        下一个回合开始即清除。

        只登记请求并叫醒等待者，终止在工具调用那一侧完成——取消源不会被阻塞（它可能是主线程上
        的信号处理）。迟到的取消若正好撞上工具体已经跑完，结局仍由工具体决定（完成优先：重写
        一个已经做成的工作不是取消该有的语义）。

        取消源在装配层：`app.cli.InterruptSource` 把回合执行期间的 Ctrl-C 换成这里的调用。
        """
        with self._lock:
            if self._turn_cancel is None:
                self._turn_cancel = _Abort(CANCELLED, reason)
            invocation = self._active
        if invocation is None:
            return False
        invocation.request(CANCELLED, f"工具 {invocation.name} 被取消（{reason}）")
        return True

    @property
    def turn_cancel_reason(self) -> str | None:
        """本轮（自 `agent/pre-step` 以来）的取消说明；本轮没有被取消就是 `None`。"""
        with self._lock:
            return None if self._turn_cancel is None else self._turn_cancel.reason

    def _new_turn(self, payload: dict, next_: Callable[[], Any]) -> Any:
        """新的回合开始：上一回合的取消不延续。"""
        with self._lock:
            self._turn_cancel = None
        return next_()

    def _wrap(self, payload: dict, next_: Callable[[], Any]) -> Any:
        name = payload["call"].get("name")
        timeout_ms = payload.get("timeout_ms") or self.default_ms
        seam = self._ctx.get("process")
        invocation = _Invocation(seam, self.grace_ms, name)
        restore = self._ctx.provide("process", invocation) if seam is not None else None
        outcome: list[Any] = []
        failure: list[BaseException] = []
        finished = threading.Event()

        def invoke() -> None:
            try:
                outcome.append(next_())
            except BaseException as exc:       # 工具体异常照常上抛，由 ToolRuntime 收敛
                failure.append(exc)
            finally:
                finished.set()
                invocation.wake()              # 工具体收场 → 等待者立刻可判：不必等满时限

        abort = None
        with self._lock:
            self._active = invocation
            # 取消粘到本轮：**入口**复查，而且与「登记在册」同一次加锁——取消因此只有两种落地
            # 方式：在本行之前到 → 这里接住（工具体一次都不启动，一个进程都不起）；在本行之后
            # 到 → `cancel()` 看得见 invocation，落在这次调用上（终止、结局 `cancelled`）。
            # 这一层是重试重入 `tools/execute` 的必经之路——重试不重跑 pre-execute / guard。
            reason = self._turn_cancel.reason if self._turn_cancel is not None else None
        try:
            if reason is not None:
                return AbortOutcome(CANCELLED, f"工具 {name} 被取消（{reason}）")
            threading.Thread(target=invoke, daemon=True).start()
            abort = invocation.wait(
                finished, timeout_ms / 1000,
                _Abort(TIMED_OUT, f"工具 {name} 执行超时（{timeout_ms}ms 未返回）"))
            if abort is not None:
                invocation.terminate_all()     # 终止 → 独立确认真静止；没达成即上抛
                if seam is not None:
                    # 等工具体静止才让中止结局替换它的结果（上限 grace_ms：Python 线程杀不掉，
                    # 等不满就只能带着「已尽力」返回）。等待窗口里它再起的范围会被当场终止。
                    # 没有受管范围 seam 的装配里没有可起的进程，也就没有要等的实体。
                    finished.wait(self.grace_ms / 1000)
        finally:
            with self._lock:
                if self._active is invocation:
                    self._active = None
            if restore is not None:
                restore()                      # 撤销代理：此后起的范围不在本册管辖内
        if abort is None:
            if failure:
                raise failure[0]
            return outcome[0]
        return AbortOutcome(abort.code, abort.reason)


# ═══════════════ 取消：一次请求粘到本轮 ═══════════════
def _tool_refused(reason: str) -> str:
    """已取消回合里「工具未执行」的说明：结局沿既有的 `denied` 通道交给 `ToolRuntime`。"""
    return f"回合已取消（{reason}）：工具未执行"


def _turn_cancelled(reason: str) -> str:
    """已取消回合的收尾文字：既是对用户说的那句话，也是日志里模型可见的那条消息。"""
    return f"⏹️ 已取消本轮：{reason}"


class TurnCancelPlugin(Plugin):
    """取消粘到本轮：本轮余下的工具调用一律拒绝，本轮不得以一次看起来正常的 `done` 蒙混收场。

    状态读自 `ctx.get("abort")`（`ToolTimeoutPlugin` 登记的**本轮取消**），由 `agent/pre-step`
    在回合边界清除——所以本插件装一次就够，不必随回合装卸；没有取消时它全程不动声色。

    三条接线：

    - `tools/guard`：本轮已取消 → 新的工具调用一律 `deny`（工具体不启动，也就没有新进程；审批
      也不会被询问）。**为什么不是 `tools/pre-execute`**：那是洋葱瀑布，审批策略
      （`PermissionPlugin`）对 `run_command` 直接给出 `ask` 而不调 `next_`，挂在那里的守卫会被
      它短路、永不被调用——审批照样弹、命令照样跑。guard 在 pre-execute 决策**之后**、审批
      **之前**运行，且监听器**不参与瀑布短路**；`deny` 比 `ask` 更严，单调收紧的语义不变。
    - `agent/post-tool`：本次调用的权威结果是 `cancelled` → 本轮就此收尾、不再采样。
    - `llm` seam：取消之后模型若只回文本，那条文本换成取消说明（见 `_CancelledTurnReply`）。
      重试重入 `tools/execute` 那条路不走这里，由 `ToolTimeoutPlugin._wrap` 的入口复查收住。
    """

    inject = ("tools", "abort", "llm")

    def apply(self, ctx: Context) -> None:
        self._abort: ToolTimeoutPlugin = ctx.get("abort")
        ctx.on("tools/guard", self._refuse_new_calls)
        ctx.on("agent/post-tool", self._end_turn_on_cancelled_result)
        ctx.provide("llm", _CancelledTurnReply(ctx.get("llm"), self._abort))

    def _refuse_new_calls(self, payload: dict, decision: dict) -> dict | None:
        """本轮已取消：工具体不启动（没有新进程，也就不会有新的孤儿）。

        guard 的协议与瀑布不同：返回 `None` 即不动决策，返回更严的决策才生效
        （`ToolRuntime._guard` 只沿 `allow < ask < deny` 收紧）。
        """
        reason = self._abort.turn_cancel_reason
        if reason is None:
            return None
        return {"kind": "deny", "reason": _tool_refused(reason)}

    def _end_turn_on_cancelled_result(self, payload: dict, next_: Callable[[], Any]) -> dict:
        """取消结局就是本轮结局：权威结果为 `cancelled` 的调用让本轮就此结束、不再采样。"""
        decision = next_()
        result = payload["result"]
        if decision.get("continue", True) and result.get("status") == CANCELLED:
            return {"continue": False, "answer": _turn_cancelled(result.get("content", ""))}
        return decision


class _CancelledTurnReply(LLM):
    """`llm` seam 的代理：本轮已取消时，模型只回的文本换成取消说明。

    取消可能落在**采样期间**（人看着模型往外吐字，按下 Ctrl-C）。那一刻本轮还没有任何工具结果，
    `agent/post-tool` 这条收尾 seam 不会被派发，而 `Loop` 收到纯文本就自己写 `turn/end=done`
    并把它当答案返回——取消会被静默丢弃。装在 llm seam 上，是因为它是**模型回复之后、回合收尾
    之前**唯一的接线口：拿到回复的同一刻按取消状态改写，不必给 `Loop` 加一条收尾 seam。

    只改写「没有工具调用的回复」：带工具调用的回复照常交给 `Loop`——每个调用都由 guard 逐条
    拒绝并落配对事件，本轮以 `denied` 收场，模型可见历史与日志始终一致。
    """

    def __init__(self, inner: LLM, abort: ToolTimeoutPlugin) -> None:
        self._inner = inner
        self._abort = abort

    def complete(self, messages: list[dict]) -> dict:
        reply = self._inner.complete(messages)
        reason = self._abort.turn_cancel_reason
        if reason is not None and not reply.get("tool_calls"):
            return {"text": _turn_cancelled(reason)}
        return reply
