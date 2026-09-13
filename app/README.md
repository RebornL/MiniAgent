# app —— 装配族（把骨架、能力与后端装成可运行的应用）

装配族是依赖方向的最上层：它认识所有族，其它族都不认识它。

本文件是该族的**权威包地图**。落位、命名、依赖方向与测试放置的规范见
[`docs/packaging.md`](../docs/packaging.md)。

## 入口

```bash
python -m app                 # 交互式对话（需要根目录 config.json）
```

`app/__main__.py` 就是这条命令：读凭据 → `chat_loop()`。等价于包化之前的 `python MiniAgent.py`。

## 本族的模块

| 模块 | 职责 |
| --- | --- |
| `app.config` | `config.json` 的惰性读取（导入期不碰配置，全新 clone 上 `import app` 必须成功） |
| `app.tools` | 应用侧的工具定义与技能描述：`TOOLS` 是唯一来源，`make_final_output_tool` / `final_output_handler` 也在其中（工具契约的消费方） |
| `app.assembly` | `build_harness()` / `resume_session()` / `register_skills()`：把 Session + 工具 + provider + 策略插件 + 日志消费者装起来 |
| `app.cli` | `chat_loop()`：交互式多轮对话，每轮只调 `loop.turn(user_input)`（含 `/exit` `/help` `/history` `/switch` `/new`）；`install_approver()` / `cli_approver()`：装 `tools/approve` 审批者，`run_command` 由人看着完整 argv 与 cwd 放行 / 拒绝；`InterruptSource`：**回合执行期间**的中断＝一次取消请求（`ctx.get("abort").cancel(...)`，与超时同一条终止路径）——**只做信号装配**（装 / 还原 SIGINT、划出窗口），取消之后的守卫与收尾归能力层（`capabilities.timeout.provider.TurnCancelPlugin`） |
| `app.__main__` | 入口：`python -m app` |
| `app.skeleton_demo` | 骨架 smoke run：`python -m app.skeleton_demo`（离线，依赖驱动激活顺序 + deny 分支） |
| `app.stack_demo` | 全栈 smoke run：`python -m app.stack_demo`（离线，压缩 / 重试 / 超时 / 持久化 / 追踪 / 技能） |
| `app.deepseek_demo` | 真实联调 demo：`python -m app.deepseek_demo`（联网，需根目录 config.json；真实 provider + 终结工具） |

装配侧的三条约定：

1. 工具不预注册：只随技能装载经 `SkillRegistry` 可逆注册（`unload_skill` 即撤销）——
   包化前就是这样，未改；
2. 审批策略装得**很窄**：只对 `run_command` 给出 `ask`（工具体不执行，直到有审批者放行）。
   审批者由 CLI 装（`app.cli.install_approver`）：交互终端里展示完整 argv 与 cwd 后由人放行 /
   拒绝；**非交互输入（无 TTY / 管道 / EOF）默认拒绝**，不放行。其余工具的行为与 legacy 一致
   ——legacy 没有审批概念，只有真正执行外部命令的工具需要这道门槛；
3. `shell` 技能默认**不装载**：模型要先 `load_skill('shell')` 才能看见 `run_command`；
   它的工具与结果形状来自能力族（`capabilities.shell`），先过沙箱 seam、再经受管范围执行，
   可被策略层终止。

中断语义（信号装配在 `app.cli`，取消之后的策略在能力层；`Loop` 零策略）：

- **回合执行期间**的 Ctrl-C 由 `app.cli.InterruptSource` 接管（`armed(ctx)` 窗口）：它把中断换成
  一次 `ctx.get("abort").cancel(...)`（与超时同一条终止路径）——正在跑的受管范围被终止、结局是
  结构化的 `cancelled`。**取消粘到本轮**（`capabilities.timeout.provider.TurnCancelPlugin`）：
  本轮余下的工具调用一律拒绝（`tools/guard`，跑在审批**之前**，所以不会再弹批准框）、重试重入
  `tools/execute` 也不再起进程树、取消之后模型只回的文本被换成取消说明——本轮不会以一次看起来
  正常的 `done` 蒙混收场。**聊天循环继续**。
- **没有命令在跑时**：取消请求无处可终止（`cancel()` 返回 False），本轮同样就此打住（见上一条）。
- **`input()` 提示处**的 Ctrl-C 不被接管，仍是 `KeyboardInterrupt` → 退出聊天循环（既有行为）。
- 中断时刻可注入（`InterruptSource.interrupt()`），测试不必起真 TTY；信号那一半用注入的
  `install` seam 验。

## 本族的测试

app 的测试天然是跨包的（装配所有族），因此**集中在** `tests/`：装配与恢复见
[`tests/test_app.py`](../tests/test_app.py)、CLI 审批者见
[`tests/test_cli_approval.py`](../tests/test_cli_approval.py)、CLI 取消源见
[`tests/test_cli_cancel.py`](../tests/test_cli_cancel.py)，不在本族内单放测试文件。
