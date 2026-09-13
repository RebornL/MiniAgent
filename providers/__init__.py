"""providers —— 后端族：骨架 seam 的实现（Provider）落在这里。

与 `capabilities/` 的分工：这里是「骨架 seam 的后端」（LLM 采样），
那里是「骨架之上的策略」（压缩 / 重试 / 超时 / 校验 / 持久化 / 追踪 / 技能 / 终结）。
两者都只依赖 `miniharness/` 的契约。

包地图见 `providers/README.md`。
"""