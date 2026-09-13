"""tools —— 工具能力：契约（`contract`）与运行时（`runtime`）分居两个包。

契约低频、实现高频，所以二者不同包：工具作者只依赖 `tools.contract` 的 `ToolDefinition`，
固定顺序的执行流水线（可变部分）在 `tools.runtime` 的 `ToolRuntime`。
"""