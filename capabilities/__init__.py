"""capabilities —— 能力族：每个能力一个目录，内部按角色与变化速率拆包。

能力 = 三个具名角色，分属不同包：

- **Definition（契约）**：`<能力>/definition/` —— 稳定、低频的类型与语义；
- **Provider（实现）**：`<能力>/provider/` —— 具体策略，变动频繁；
- **Consumer（消费方）**：只经契约消费该能力的包（`<能力>/consumer/`，或骨架 / 装配层的包）。

为什么契约与实现不同包：让频繁变动的策略不牵连稳定接口。
每个能力的三个角色分别落在哪个包，见 `capabilities/README.md`（权威包地图）；
新代码落位规范见 `docs/packaging.md`。
"""