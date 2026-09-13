"""process —— 受管范围（managed range）的中间包：契约在骨架，平台后端在 `providers/`。

一次工具执行所拥有的整棵进程树就是一个**受管范围**：启动、等待退出、终止都以它为单位，
这样才不会留下孤儿。本层只承载归属：

- `contract`：形状与语义（`ManagedRange` / `ProcessSeam` / `TerminationError`），低频；
- 平台后端（POSIX 信号组升级 / Windows Job Object）在 `providers.process`，随平台变。

不放实现，也不重复下层代码。
"""
