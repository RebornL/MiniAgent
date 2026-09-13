"""permission —— 工具审批能力。

- 契约：`miniharness.tools.runtime` 的 `tools/pre-execute` 决策词汇（`allow` / `ask` / `deny`，
  单调收紧）——它就是这条 seam 的稳定接口，因此留在骨架；
- 实现：`provider`（`PermissionPlugin`，拒绝名单策略）；
- 消费方：`miniharness.tools.runtime`（按决策决定是否执行工具体）。
"""