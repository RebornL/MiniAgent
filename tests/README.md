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
| `tests/support.py` | —— | 集成测试共享的装配 helper（`_assemble` / `_event_types` / `CALC_PARAMS` / `WRITE_PARAMS`），只服务本目录 |
| `tests/fixtures/legacy-v0-session/` | —— | 仓里一份**真实旧会话的逐字拷贝**（v0 的 `messages.json` + `meta.json` 旁路），供旧会话迁移的回归测试使用；不是用当代 API 现造的新会话 |
| `tests/test_turn.py` | S2（turn 边界，集成） | 工具体执行与 `tool/result` 按序入日志、deny 的配对完整性、终结工具收尾本轮、持久化 / 追踪消费者接上日志（回合结束后磁盘上就是逐行可读的事件日志）、**只换插件就改变结局而 Loop 零改动** |
| `tests/test_capabilities.py` | S2 + 契约等价 | 压缩达同样阈值才触发且摘要语义与契约包逐字一致；终结工具不再采样 |
| `tests/test_app.py` | S2（装配） | `build_harness` 的工具懒注册与落盘、技能装载、`resume_session` 只靠重放日志恢复（压缩后的投影逐字一致、技能状态与领域提示仍在、`meta.json` 无 summary/active_skills 旁路）、真实 v0 旧会话迁移后技能工具已注册且摘要链非空、`meta.json` 不存模型可见内容的拷贝、全新 clone 上 `import app` 不读 config.json |

## 运行

```bash
python -m pytest -q
```

包内测试会随这条命令一并收集（`test_*.py` 在包目录里）。collect 顺序无关紧要：
`tests/` 下的共享 helper 走 `from tests.support import ...`，靠 pytest 的 rootdir 入 `sys.path`。
