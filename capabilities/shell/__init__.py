"""shell —— 命令执行能力（唯一真正执行外部命令的工具）。

- 契约：`definition`（argv-only 的调用语义、结果形状 `exit_code` / `stdout` / `stderr`、
  输出上限与截断标记）；
- 实现：`provider`（`ShellTool`：argv 先过沙箱 seam，再交给受管范围并等它退出）；
- 消费方：`app.assembly`（注册 `shell` 技能 + 为 `run_command` 装审批策略 + 装配两个 seam）。

它是两个进程边界 seam 的第一个**真实消费者**，而两者互不相干：

- **沙箱**（`miniharness.sandbox.contract`）：只包装 argv——给出「可执行的 argv + 完整性要求」，
  不可用即 `SandboxUnavailableError`（fail-closed，禁止静默透传无约束执行）；
- **受管范围**（`miniharness.process.contract`）：只负责这棵进程树的生死——等退出、被终止。

超时与「何时终止」都不在这里（那是策略层，见 T8）；工具只保证命令在沙箱里、跑在受管范围里。
"""
