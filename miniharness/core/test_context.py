"""包内测试：`Context` 的可逆效应栈（`miniharness.core`）。

`provide` 返回的 disposer 被调用后必须**自己出栈**：长会话里「临时替换一个服务、用完立刻还原」
是常态（超时护栏每次工具调用都把 `process` 换成当次调用的登记代理），留在栈上的闭包会连同它
捕获的旧服务一起无界堆积，而只有 `dispose()` 才回收。

这条泄漏从公开面上看不见，`_effects` 的长度是唯一探针（(a) 只用它一次）；其余断言都走公开面
（`get` / `effect` / `dispose`），并守住「全量 dispose 仍逆序还原、且逐层还原到被替换前的值」。
"""
from __future__ import annotations

from miniharness.core import Context


def test_restoring_a_provided_service_leaves_no_disposer_behind():
    """(a) 「provide → 显式还原」若干次后效应栈回到初始长度，不随次数增长。

    这是长会话里真实的形状：基座服务先由装配提供，随后每次调用临时替换、用完立刻还原。
    """
    ctx = Context()
    ctx.provide("process", "real-seam")
    baseline = len(ctx._effects)

    for i in range(50):
        restore = ctx.provide("process", f"proxy-{i}")
        assert ctx.get("process") == f"proxy-{i}"
        restore()
        assert ctx.get("process") == "real-seam"       # 还原到被替换前的值

    assert len(ctx._effects) == baseline, "provide 的 disposer 没有出栈：每次调用都白留一份"


def test_dispose_unwinds_nested_replacements_in_reverse_order():
    """(b) `dispose()` 逆序 unwind：同一个键的嵌套替换逐层还原，且各步之间没有串位。

    两条观察者效应按「后进先出」夹在嵌套层次之间：它们各自看到的旧值就是还原顺序的证据——
    最后一次替换先被撤销，且撤销它的时候它下面那层还在。
    """
    ctx = Context()
    ctx.provide("a", "a0")
    ctx.provide("a", "a1")
    layers: list[object] = []
    ctx.effect(lambda: layers.append(ctx.get("a")))    # 夹在 a1 与 a2 之间
    ctx.provide("a", "a2")
    ctx.provide("b", "b0")
    top: list[object] = []
    ctx.effect(lambda: top.append((ctx.get("a"), ctx.get("b"))))

    ctx.dispose()

    assert top == [("a2", "b0")]                        # 最后注册的观察者先跑：还原还没开始
    assert layers == ["a1"]                            # 轮到它时 a2 已还原成 a1、a1 还没动
    assert ctx.get("a") is None and ctx.get("b") is None
    ctx.dispose()                                      # 再 dispose 一次：空栈上的幂等 no-op
    assert ctx.get("a") is None and ctx.get("b") is None


def test_a_disposer_called_twice_is_idempotent():
    """(c) 同一个 disposer 调两次：幂等、不抛错，后续的全量 dispose 照旧把余下都还原掉。"""
    ctx = Context()
    ctx.provide("a", "a0")
    restore_a1 = ctx.provide("a", "a1")
    ctx.provide("b", "b0")

    restore_a1()
    assert ctx.get("a") == "a0"
    restore_a1()                                       # 第二次：不抛错、不改状态
    assert ctx.get("a") == "a0"

    ctx.dispose()                                      # 混用之后余下的还原依旧干净
    assert ctx.get("a") is None and ctx.get("b") is None


def test_mixing_explicit_restores_with_dispose_keeps_the_stack_consistent():
    """(c) 显式还原与全量 dispose 混用、以及 dispose 之后再用旧 disposer：都幂等、不抛错。"""
    ctx = Context()
    restore_first = ctx.provide("k", "first")
    restore_second = ctx.provide("k", "second")

    restore_second()                                   # 后进先出地显式还原
    assert ctx.get("k") == "first"
    restore_second()                                   # 再调一次：状态不变
    assert ctx.get("k") == "first"

    ctx.dispose()
    assert ctx.get("k") is None

    restore_first()                                    # 已出栈的旧 disposer：不抛错
    settled = ctx.get("k")
    restore_first()                                    # 仍然幂等：状态稳定
    assert ctx.get("k") == settled

    ctx.provide("k", "fresh")                          # 栈没有被搞乱：仍然能正常提供
    assert ctx.get("k") == "fresh"
    ctx.dispose()
    assert ctx.get("k") is None
