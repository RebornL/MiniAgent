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
——`cancelled` 而非 `timed_out`。本仓目前**没有取消源**（用户中断 / 回合放弃还没接线），
这个入口是留给它的接线口。

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
from miniharness.process.contract import (
    DEFAULT_GRACE_MS,
    ManagedRange,
    ProcessSeam,
    TerminationError,
)
from miniharness.tools.contract import CANCELLED, TIMED_OUT, AbortOutcome

__all__ = ["ToolTimeoutPlugin"]


@dataclass(frozen=True)
class _Abort:
    """一次中止请求：超时与取消共用同一条终止路径，区别只在结局码与说明。"""

    code: str
    reason: str


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
        """
        deadline = time.monotonic() + max(timeout_s, 0.0)
        with self._cond:
            while True:
                if finished.is_set():
                    return None
                if self._abort is None:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._cond.wait(remaining)
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
    替换它的结果）。取消入口 `cancel()` 以 `ctx.get("abort")` 一并暴露。

    工具体一律跑在 **daemon** 线程里，因此卡死的工具不会拖住进程退出——受管范围能终止，而
    Python 线程杀不掉，纯 Python 的工具体仍只能停止等待。
    """

    inject = ("tools",)

    def __init__(self, default_ms: int = DEFAULT_TOOL_TIMEOUT * 1000,
                 grace_ms: int = DEFAULT_GRACE_MS) -> None:
        self.default_ms = default_ms
        self.grace_ms = grace_ms
        self._ctx: Context | None = None
        self._lock = threading.Lock()
        self._active: _Invocation | None = None     # 工具调用在 Loop 里是顺序的，至多一个在跑

    def apply(self, ctx: Context) -> None:
        self._ctx = ctx
        ctx.on("tools/execute", self._wrap)
        ctx.provide("abort", self)             # 取消请求的接线口（超时与取消同一路径）

    def cancel(self, reason: str = "调用方请求取消") -> bool:
        """请求取消**当前在跑**的工具调用：与超时同一条终止路径，结局是 `cancelled`。

        只登记请求并叫醒等待者，终止在工具调用那一侧完成——取消源不会被阻塞。返回是否有一个
        在跑的调用接下了请求（没有在跑的调用时是幂等的 no-op）。迟到的取消若正好撞上工具体
        已经跑完，结局仍由工具体决定（完成优先：重写一个已经做成的工作不是取消该有的语义）。

        本仓暂无取消源（用户中断 / 回合放弃尚未接线），这个入口就是留给它的。
        """
        with self._lock:
            invocation = self._active
        if invocation is None:
            return False
        invocation.request(CANCELLED, f"工具 {invocation.name} 被取消（{reason}）")
        return True

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
        try:
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
