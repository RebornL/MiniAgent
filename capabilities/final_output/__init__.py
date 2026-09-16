"""final_output —— 终结工具能力（某工具执行成功即作为本轮最终答复）。

- 契约：`miniharness.loop` 的 `agent/post-tool` 收尾协议（`{"continue": False, "answer": ...,
  "status": ...}`，`status` 是本轮结局码，缺省 `done`）
  ——循环只认协议、不认工具名，因此契约留在骨架；
- 实现：`provider`（`FinalOutputPlugin`，声明哪些工具名是终结工具）；
- 消费方：`miniharness.loop`（按收尾协议结束本轮）。
"""