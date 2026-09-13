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
| `providers.sandbox` | 沙箱 seam 的 **Provider** | `EnvSandbox`：环境收敛（白名单之外一个都不继承）+ 裸命令名在**收敛后的 PATH** 里解析成绝对路径；策略要求而它强制不了的（断网）**拒绝服务**；**不负责终止**——一个进程都不起 | `miniharness.sandbox.contract` |

Provider 只认识契约：`DeepSeekProvider` 与 `MockLLM` 都只实现 `complete(messages)`，
工具描述经 `ctx.get("tools").specs()` 自取——换后端不动循环；`SubprocessSeam` 只实现
`ProcessSeam.spawn`、`EnvSandbox` 只实现 `SandboxSeam.wrap`，消费方经 `ctx.get("process")` /
`ctx.get("sandbox")` 拿的是契约（见 [`miniharness/README.md`](../miniharness/README.md)），
装配由 `app/` 负责。

受管范围与沙箱目前唯一的真实消费方都是 `capabilities.shell.provider.ShellTool`（`run_command`），
它们互相独立：沙箱先给出「可执行的 argv + 完整性要求」，受管范围再负责这棵进程树的生死。
工具**在调用时刻**取这两个服务，因此策略层可以在这层包一个代理（记录当前范围 / 请求终止），
把「超时或取消 → 终止受管范围」接起来——终止动词只有 `terminate` / `release` 两个，工具自己不用，
沙箱也不认识它们。

`EnvSandbox` 只强制环境收敛与 argv 解析；**凡它强制不了的策略要求一律拒绝服务**
（`SandboxUnavailableError`），这就是「沙箱不可用必须 fail-closed」在实现侧的落点。
收敛会改变子进程看到的环境，也就可能改变它的默认文本编码——需要留住哪些变量由策略
（`SandboxPolicy.env_allowlist`，装配层的 `app.assembly.SHELL_ENV_ALLOWLIST`）决定。

## 本族的测试

包内测试与实现同层、独立文件：`providers/process/test_managed_range.py` 用真实子进程树
证明受管范围的语义——终止以整棵进程树为单位、组长先退出也不让范围变空、释放返回时不留孤儿；
`providers/sandbox/test_env_sandbox.py` 用真实环境变量与真实文件系统证明沙箱**真能强制**的
维度（白名单之外一个都不留、裸名字在收敛后的 PATH 里解析、强制不了的要求即拒绝服务），
并证明 `wrap` 本身不执行任何东西。

## 运行

真实联调 demo 归装配族（装配是唯一该认识所有族的地方）：

```bash
python -m app.deepseek_demo      # 真实联调（需要根目录 config.json，会联网）
```

`MockLLM` 不单独提供入口：它被 `python -m app.skeleton_demo` / `python -m app.stack_demo` 与测试使用。
