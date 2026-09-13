"""miniharness —— 骨架族：低频的契约与运行时。

本族只装「不随策略变动」的东西：插件注册与事件总线（`core`）、会话事件日志与投影
（`session`）、工具能力的契约与运行时（`tools.contract` / `tools.runtime`）、LLM 能力的
契约（`llm.contract`）、以及零策略的循环（`loop`）。

包地图见 `miniharness/README.md`；新代码落位规范见 `docs/packaging.md`。
"""