# providers —— 后端族（骨架 seam 的实现）

与 `capabilities/` 的分工：这里是「骨架 seam 的后端」（LLM 采样、受管范围的平台后端），
那里是「骨架之上的策略」（压缩 / 重试 / 超时 / 校验 / 持久化 / 追踪 / 技能 / 终结）。
两者都只依赖 `miniharness/` 的契约。

本文件是该族的**权威包地图**。落位、命名、依赖方向与测试放置的规范见
[`docs/packaging.md`](../docs/packaging.md)。

## 本族的包

| 包 | 角色 | 职责 | 依赖 |
| --- | --- | --- | --- |
| `providers.deepseek` | `LLM` seam 的真实 **Provider** | `DeepSeekProvider`：流式累积 content 与 tool_calls 分片；出站线格式与入站归一化都留在这里 | `miniharness.core`、`miniharness.llm.contract`（`openai` SDK） |
| `providers.mock` | `LLM` seam 的离线 **Provider** | `MockLLM`：按序回放剧本（`then_tool_call` / `then_text`），并记录收到的 messages | `miniharness.llm.contract` |
| `providers.process` | 受管范围 seam 的平台 **Provider** | `SubprocessSeam` / `SubprocessRange`：POSIX 信号组升级（TERM → 宽限 → KILL）、Windows Job Object（`KILL_ON_JOB_CLOSE` + `TerminateJobObject`，范围空没空看 job 的活跃进程数） | `miniharness.core`、`miniharness.process.contract` |

Provider 只认识契约：`DeepSeekProvider` 与 `MockLLM` 都只实现 `complete(messages)`，
工具描述经 `ctx.get("tools").specs()` 自取——换后端不动循环；`SubprocessSeam` 只实现
`ProcessSeam.spawn`，消费方经 `ctx.get("process")` 拿的是契约（见
[`miniharness/README.md`](../miniharness/README.md)），装配由 `app/` 负责。

受管范围目前唯一的真实消费方是 `capabilities.shell.provider.ShellTool`（`run_command`）。
它**在调用时刻**取 `process` 服务，因此策略层可以在这层包一个代理（记录当前范围 / 请求终止），
把「超时或取消 → 终止受管范围」接起来——终止动词只有 `terminate` / `release` 两个，工具自己不用。

## 本族的测试

包内测试与实现同层、独立文件：`providers/process/test_managed_range.py` 用真实子进程树
证明受管范围的语义——终止以整棵进程树为单位、组长先退出也不让范围变空、释放返回时不留孤儿。

## 运行

真实联调 demo 归装配族（装配是唯一该认识所有族的地方）：

```bash
python -m app.deepseek_demo      # 真实联调（需要根目录 config.json，会联网）
```

`MockLLM` 不单独提供入口：它被 `python -m app.skeleton_demo` / `python -m app.stack_demo` 与测试使用。
