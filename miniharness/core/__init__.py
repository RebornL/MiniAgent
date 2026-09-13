"""core —— 插件与事件治理（原语 1 / 4 / 5）。

- `Context`：唯一的服务注册表 + 事件总线（`emit` 观察 / `waterfall` 洋葱 / `serial` 按序）
  + 可逆效应栈（`effect` / `dispose` / `load` / `unload`）；
- `Plugin`：声明 `inject` 依赖，在 `apply(ctx)` 里注册能力（注册皆可逆）。

本包是骨架里最稳定的一层：策略与实现都只依赖它，它不依赖任何上层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

__all__ = ["Context", "Plugin"]


_MISSING = object()


# ═══════════════ 原语 1 + 5：Context（注册表 + 事件总线 + 可逆效应栈） ═══════════════
class Context:
    """唯一的注册表与事件总线，所有扩展点汇聚于此。

    - 服务：`provide(key, service)` / `get(key)`；
    - 事件三语义：`emit`（观察）/ `waterfall`（洋葱，可 short-circuit）/ `serial`（按序，返回假则停）；
    - 效应：`effect(disposer)` 入栈，`dispose()` 逆序 unwind；插件 `apply` 期间的注册
      自动归属该插件的作用域，`unload(plugin)` 可单独逆序撤销。
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._effects: list[Callable[[], None]] = []      # 全局 disposer 栈
        self._scopes: list[list[Callable[[], None]]] = []  # 插件 apply 期间的子作用域
        self._activations: dict[int, _Activation] = {}
        self._pending: list[Any] = []                      # 依赖未就绪的插件
        self._activating = False

    # ── 服务 ───────────────────────────────────────
    def provide(self, key: str, service: Any) -> Callable[[], None]:
        """注册服务，返回撤销它的 disposer；注册后重试激活挂起的插件。

        disposer 被调用时**把自己从效应栈上摘掉**（幂等）：长会话里「临时替换一个服务、用完立刻
        还原」是常态（超时护栏每次工具调用都把 `process` 换成当次调用的登记代理），留在栈上的
        闭包会连同它捕获的旧服务一起无界堆积——只有 `dispose()` 才回收。
        """
        previous = self._services.get(key, _MISSING)
        self._services[key] = service

        def dispose() -> None:
            if dispose in self._effects:          # 幂等：已出栈（或根本没入过栈）就不再摘
                self._effects.remove(dispose)
            if previous is _MISSING:
                self._services.pop(key, None)
            else:
                self._services[key] = previous

        self.effect(dispose)
        self._activate_pending()
        return dispose

    def get(self, key: str, default: Any = None) -> Any:
        return self._services.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._services

    # ── 事件三语义 ─────────────────────────────────
    def on(self, event: str, fn: Callable[..., Any]) -> Callable[[], None]:
        """订阅事件，返回退订 disposer（在插件作用域内自动随插件卸载撤销）。"""
        handlers = self._listeners.setdefault(event, [])
        handlers.append(fn)

        def dispose() -> None:
            if fn in handlers:
                handlers.remove(fn)

        return self.effect(dispose)

    def listeners(self, event: str) -> tuple[Callable[..., Any], ...]:
        return tuple(self._listeners.get(event, ()))

    def emit(self, event: str, payload: dict) -> None:
        """观察：逐个通知，忽略返回值。"""
        for fn in self.listeners(event):
            fn(payload)

    def waterfall(self, event: str, payload: dict, default: Any) -> Any:
        """洋葱：监听器拿到 `next` 委托后继；不调 `next` 即 short-circuit。"""
        handlers = self.listeners(event)

        def run(i: int) -> Any:
            if i == len(handlers):
                return default(payload) if callable(default) else default
            return handlers[i](payload, lambda: run(i + 1))

        return run(0)

    def serial(self, event: str, payload: dict) -> None:
        """按序：任一监听器返回假（False）即停止后续。"""
        for fn in self.listeners(event):
            if fn(payload) is False:
                break

    # ── 可逆效应（原语 5）───────────────────────────
    def effect(self, disposer: Callable[[], None]) -> Callable[[], None]:
        if self._scopes:
            self._scopes[-1].append(disposer)
        self._effects.append(disposer)
        return disposer

    def dispose(self) -> None:
        """逆序 unwind 全部注册。"""
        self._pending.clear()
        while self._effects:
            self._effects.pop()()
        self._activations.clear()

    # ── 插件装载（原语 4）───────────────────────────
    def load(self, plugin: Any) -> str:
        """装载插件：依赖（`inject`）未就绪则挂起，就绪后自动激活。"""
        if id(plugin) in self._activations:
            return "active"
        if plugin not in self._pending:
            self._pending.append(plugin)
        self._activate_pending()
        return "active" if id(plugin) in self._activations else self._pending_reason(plugin)

    def unload(self, plugin: Any) -> bool:
        """卸载插件：其全部注册逆序撤销，不留悬空引用。"""
        if plugin in self._pending:
            self._pending.remove(plugin)
            return True
        activation = self._activations.pop(id(plugin), None)
        if activation is None:
            return False
        owned = list(activation.disposers)
        activation.unwind()
        self._effects = [d for d in self._effects if d not in owned]
        return True

    def _pending_reason(self, plugin: Any) -> str:
        missing = [d for d in getattr(plugin, "inject", ()) if not self.has(d)]
        return f"pending，等待服务: {missing}"

    def _deps_ready(self, plugin: Any) -> bool:
        return all(self.has(dep) for dep in getattr(plugin, "inject", ()))

    def _activate_pending(self) -> None:
        if self._activating:      # 激活过程中又 provide 时由外层循环兜住
            return
        self._activating = True
        try:
            while True:
                ready = [p for p in self._pending if self._deps_ready(p)]
                if not ready:
                    return
                for plugin in ready:
                    self._pending.remove(plugin)
                    self._apply(plugin)
        finally:
            self._activating = False

    def _apply(self, plugin: Any) -> None:
        scope: list[Callable[[], None]] = []
        self._scopes.append(scope)
        try:
            plugin.apply(self)
        except Exception:
            rolled_back = list(scope)
            while scope:          # 激活失败：回滚本次注册
                scope.pop()()
            self._effects = [d for d in self._effects if d not in rolled_back]
            raise
        finally:
            self._scopes.pop()
        self._activations[id(plugin)] = _Activation(scope)


@dataclass
class _Activation:
    """一次插件激活及其注册的 disposer（逆序 unwind）。"""

    disposers: list[Callable[[], None]] = field(default_factory=list)

    def unwind(self) -> None:
        while self.disposers:
            self.disposers.pop()()


# ═══════════════ 原语 4：Plugin（inject 声明依赖，按需激活） ═══════════════
class Plugin:
    """插件契约：声明 `inject` 依赖，在 `apply(ctx)` 里注册能力（注册皆可逆）。"""

    inject: Sequence[str] = ()

    def apply(self, ctx: Context) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError
