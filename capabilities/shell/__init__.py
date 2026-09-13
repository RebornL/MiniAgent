"""shell —— 命令执行能力（唯一真正执行外部命令的工具）。

- 契约：`definition`（argv-only 的调用语义、结果形状 `exit_code` / `stdout` / `stderr`、
  输出上限与截断标记）；
- 实现：`provider`（`ShellTool`：把 argv 交给受管范围并等它退出，读回有上限的输出）；
- 消费方：`app.assembly`（注册 `shell` 技能 + 为 `run_command` 装审批策略）。

它是受管范围（`miniharness.process.contract`）的第一个**真实消费者**：工具只保证命令
跑在受管范围里，因而可被策略层终止；超时与终止本身不在这里（见 T8）。
"""
