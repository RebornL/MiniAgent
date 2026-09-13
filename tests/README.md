# tests —— 测试族（跨包集成集中一处）

测试与实现**同层但分离**：实现文件里不放测试代码，测试放独立文件。按「断言在哪个层才成立」分两处：

| 放哪 | 是什么 | 在哪 |
| --- | --- | --- |
| **包内测试** | 只依赖本包（或更下层）就能成立的断言 | 与实现同一个包、独立文件：`miniharness/session/test_projection.py`、`miniharness/tools/runtime/test_pipeline.py`、`capabilities/*/{definition,provider}/test_*.py` |
| **跨包集成** | 装配多个包之后才成立的断言（驱动 `Loop.turn`、装配整个 app） | 本目录：`tests/test_turn.py`、`tests/test_capabilities.py`、`tests/test_app.py` |

本文件是该族的**权威包地图**。规范见 [`docs/packaging.md`](../docs/packaging.md)。

## 本族的文件

| 文件 | seam | 覆盖 |
| --- | --- | --- |
| `tests/support.py` | —— | 集成测试共享的装配 helper（`_assemble` / `_event_types` / `CALC_PARAMS` / `WRITE_PARAMS`），只服务本目录；**进程存活探针**（`_alive` / `_assert_gone`）不在本文件——它按「下层 helper 留下层」放在 `providers/process/probe.py`，由包内测试与集成测试共用**同一份实现** |
| `tests/fixtures/legacy-v0-session/` | —— | v0 落盘**形态**的样本（`messages.json` + `meta.json` 旁路）：结构与真实旧会话一致，**内容是合成的**（真实会话数据不入库，`agent_sessions/` 在 .gitignore 里、仓库公开） |
| `tests/test_turn.py` | S2（turn 边界，集成） | 工具体执行与 `tool/result` 按序入日志、deny 的配对完整性、终结工具收尾本轮、持久化 / 追踪消费者接上日志（回合结束后磁盘上就是逐行可读的事件日志）、三个语义检查点在下一步之前已落盘且失败即 fail-closed、**只换插件就改变结局而 Loop 零改动** |
| `tests/test_capabilities.py` | S2 + 契约等价 | 压缩达同样阈值才触发且摘要语义与契约包逐字一致；终结工具不再采样 |
| `tests/test_shell.py` | 进程边界（真实子进程）+ S2（装配） | `run_command`：真实子进程的输出与退出码、参数不经 shell 原样送达、`cwd` 生效、真实洪泛输出被截断、命令跑在受管范围里且可被外部终止、被拒时工具体未执行、装配层默认不装载 `shell` 技能且对 `run_command` 默认拒绝 |
| `tests/test_timeout.py` | 进程边界（真实子进程） | 超时 / 取消 → **终止受管范围**：超时后命令（含后代）真的没了——用操作系统独立确认、取消走同一条路径并给 `cancelled`、已启动的工具体跑到静止才让结局替换它的结果、终止后下一次调用照常；边界态：组长已自行退出而后代仍在跑、时限到的时候实体已自己正常退出（终止是 no-op）、正常退出与时限的竞态。**Windows 后端没有「宽限 → 强杀」升级档**（Job Object 一次强杀到底），升级档的真实触发用例带 `skipif(os.name == "nt")`，在 `providers/process/test_managed_range.py` |
| `tests/test_app.py` | S2（装配） | `build_harness` 的工具懒注册与落盘、技能装载、`resume_session` 只靠重放日志恢复（压缩后的投影逐字一致、技能状态与领域提示仍在、`meta.json` 无 summary/active_skills 旁路）、真实 v0 旧会话迁移后技能工具已注册且摘要链非空、`meta.json` 不存模型可见内容的拷贝、全新 clone 上 `import app` 不读 config.json |
| `tests/test_sandbox.py` | 沙箱 seam（装配 + 真实子进程） | `run_command` 先过沙箱：命令在收敛后的环境里跑（子进程拿到沙箱标记、拿不到父进程凭据）、退出与输出走同一条结果通道；**失败注入**——沙箱没装 / 装配后被卸载 / 拒绝服务时 `failed` 且命令真的没跑（marker 文件不存在），每处都附「装上可用沙箱后同一条命令 `ok` 且 marker 写入」的非空洞对照；装配层的 `build_harness` 装的确实是这个沙箱 |
| `tests/test_cli_approval.py` | 审批闸门（装配 + CLI） | CLI 审批者裁决 `run_command`：请求展示完整 argv 与 cwd、放行才执行、拒绝沿用 `denied` 且工具体未执行、非交互输入（非 TTY / 管道 / EOF）默认拒绝、其它工具不经审批 |

## 运行

```bash
python -m pytest -q
```

包内测试会随这条命令一并收集（`test_*.py` 在包目录里）。collect 顺序无关紧要：
`tests/` 下的共享 helper 走 `from tests.support import ...`，靠 pytest 的 rootdir 入 `sys.path`。
