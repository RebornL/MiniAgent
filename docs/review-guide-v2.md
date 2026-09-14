# review-guide-v2 —— miniharness v2 总结与审视指南

> 对应提交 `66ff665`。用途：① 说清 v2 改了哪些东西；② 说清每处改动是**怎么验的**（这是 v2 与 v1 最大的不同）；③ 给出一条能独立复核整个 v2 的路径（读什么、跑什么、怀疑什么）；④ **如实记下尚未闭合的地方**，让进展不随任何一次上下文丢失而丢。
> 相关文档：[`docs/miniharness.md`](miniharness.md)（现行设计）、[`docs/packaging.md`](packaging.md)（落位规范与 §9 已知偏差）、[`CONTEXT.md`](../CONTEXT.md)（词表）、[`docs/adr/0001-session-log-is-the-source-of-truth.md`](adr/0001-session-log-is-the-source-of-truth.md)（硬约束）、[`docs/review-guide-v1.md`](review-guide-v1.md)（上一轮）、[`miniharness-v2-spec.md`](../miniharness-v2-spec.md)（规格原文）。

## 1. 做了什么

v2 = 三条互相咬合的工作面，由 issue **#2** 统一，拆成 11 张票（#3–#13），**全部关闭**。

| 工作面 | 票 | 一句话 |
| --- | --- | --- |
| ① 权威源从**投影**换成**事件日志** | #4 #6 #5 | 落盘的是事件流；模型可见消息永远是 `derive_messages()` 派生出的、可丢弃的投影 |
| ② 超时不再是空承诺 | #7 #8 #9 #10 #11 #13 | 沙箱与受管范围拆成**两个独立 seam**；终止下沉到进程边界 |
| ③ 按能力族包化 | #3 | Definition / Provider / Consumer 三个具名角色，按变化速率拆包 |

### 提交（`eb04750` → `66ff665`，**100 文件、+9499 / −2875**）

| 提交 | 内容 |
| --- | --- |
| `dfea59f` | T1 按能力族包化（**纯搬移**：7 个 legacy 模块 100% git 重命名，断言前后等量） |
| `1155c49` | T2 事件日志落盘 + T6 受管范围（Windows Job Object） |
| `e7343b5` | T3 压缩/技能事件化 + T5 可区分的中止结局 |
| `f5845d9` | 把承重的工作约定写进 `AGENTS.md` |
| `1f8909a` | T4 有界写后缓冲 + 检查点 / T9 `run_command` |
| `6fc1864` | T7 沙箱 seam / T10 CLI 审批者 |
| `608a3e6` | T8 超时接线到真终止 |
| `66ff665` | T11 取消源（回合期间的 Ctrl-C） |

## 2. 三条工作面各自改了什么

### ① 权威源：事件日志

- 盘上只有 `events.v<N>.jsonl`（首行 header 带格式版本）+ `meta.json` / `traces.json`。**消息历史不是权威存储**——这一条与两个生产级 harness 的调研结论一致（权威 = 事件流）。
- `Session.replay(load_events(...))` 即恢复；压缩与技能装载/卸载是**一等事件**（`context/compacted` 带 `shadowed_seqs` / `shadowed_range` / `replacement`；`skill/loaded` / `skill/unloaded`）。v0→v1→v2 迁移链把旧旁路**折进日志**。
- 写盘：事件先入内存日志，再有界写后缓冲（容量 64）批量落盘；**显式 `flush()` 屏障，只有它返回才构成崩溃承诺**；写失败时未落盘项留在队列里可原地重试。
- 三个语义检查点 fail-closed 落盘：每步开始前 / 向模型请求前 / 顶层工具派发前（`agent/checkpoint`）。
- 文件没有完整 header ⇒ **视同从未落盘**（可自愈）；盘上出现更高代际 ⇒ **拒绝写入**。

### ② 超时：两个独立 seam，终止下沉到进程边界

- **沙箱 seam**（`miniharness/sandbox/contract`）：`wrap(意图, 策略) → 可执行 argv + 完整性要求`。契约里**没有任何终止动词**。后端 `EnvSandbox` 只强制它**真能强制**的事：环境收敛 + 裸 `argv[0]` 在收敛后 PATH 里的解析；策略要求而它强制不了的（`deny_network`）**拒绝服务**，不假装隔离生效。**沙箱不可用即 fail-closed，绝不静默透传无约束执行。**
- **受管范围 seam**（`miniharness/process/contract`）：`spawn` → `wait_for_exit` → `terminate(grace_ms)`。Windows 用 Job Object（`KILL_ON_JOB_CLOSE` + `TerminateJobObject`）——`taskkill /T /F` 需要组长还活着，组长先退就收不回后代。
- **超时/取消 → 同一条终止路径**：`terminate_all()` → 再 `poll()` **独立确认**（仍活着即抛 `TerminationError`，**绝不把「还活着」谎报成 `timed_out`**）→ 等工具体静止 → 结构化结局。结局码：`OK` / `TIMED_OUT` / `CANCELLED` / `DENIED` / `FAILED`（T5），重试按码（`{timed_out, failed}` 可重试）。
- **工具不拥有进程生命周期**：`run_command` 不设超时、不终止；spawn 在**调用时刻**经 `ctx.get("process")` 取 seam，策略层据此在那一层包装。
- 取消源：回合执行期间的 Ctrl-C → `cancel()`；取消**粘住本轮**（四个落点：`tools/execute` 入口复查 / `tools/guard` / `agent/post-tool` / llm 代理）。

### ③ 按能力族包化

`miniharness/{core,session,loop,process,sandbox,llm,tools}` · `capabilities/<能力>/{definition,provider,consumer}` · `providers/{process,sandbox,deepseek,mock}` · `app/{assembly,cli,config,tools,__main__,*demo}` · `tests/`。依赖方向 `app → capabilities / providers → miniharness`，绝对 import。规则成文在 [`docs/packaging.md`](packaging.md)。

## 3. 核心不变量（v2 之后）

1. **Model-visible means logged**：凡进模型的内容都必须能从事件日志重建；不得有并行存储的模型可见状态（ADR-0001，硬约束）。
2. **投影可丢弃**：`derive_messages()` 是纯函数、确定、幂等；缓存按版本号丢弃重建。
3. **`Loop` 零策略**：阈值、判定、具体工具名一律不得写回循环体——策略只以订阅者身份挂在事件 seam 上。v2 全程 11 张票，Loop 只在 T4 增加过 `agent/checkpoint` 这个**事件 seam** 与三个词汇常量，无任何判定分支。
4. **每个 `tool_call` 必有配对结果**；中止结局与成功**同构**，走同一条结果通道。
5. **沙箱与终止是两个 seam**，不得混做；工具只声明「做什么」。
6. **注册可逆**：`provide` / `on` 的 disposer 可调用且幂等；显式还原时把自己从 effect 账本摘掉。

## 4. 怎么审视

### 建议阅读顺序

1. [`docs/packaging.md`](packaging.md) §1–§4 —— 先拿到「放哪、叫什么、依赖朝哪」的规则，再读代码；§9 是**已知偏差**。
2. [`docs/miniharness.md`](miniharness.md) —— 现行架构（含 mermaid：架构 / 回合时序 / 日志投影 / 依赖激活 / 终止 / 沙箱）。
3. [`CONTEXT.md`](../CONTEXT.md) —— 词表。**先读它再读代码**，否则「投影」「受管范围」「中止结局」会被按日常语义理解。
4. `miniharness/loop/__init__.py`（很短）—— 确认循环里没有任何策略。
5. `capabilities/persistence/{definition,provider}` —— v2 最重的一块。
6. `capabilities/{timeout,shell}/provider` + `providers/{process,sandbox}` —— 终止与隔离。
7. `app/assembly.py` —— 全部接线在一处可见。

### 自证命令

```bash
# 全量（基线：128 passed, 1 skipped）
python -m pytest -q

# Loop 零策略：循环里不该出现任何工具名 / 策略阈值
git grep -nE 'run_command|timeout_ms|keep_last_n|is_retryable' -- miniharness/loop   # 应为空

# 不经 shell：全仓不该有 shell 执行面
git grep -nE 'shell=True|os\.system|os\.popen|shlex' -- '*.py'                        # 应为空

# 沙箱与终止是两个 seam：沙箱契约里不该有终止动词
grep -nE 'terminate|kill|signal' miniharness/sandbox/contract/__init__.py             # 应为空

# 权威源是日志：模型可见状态不该有旁路存储
git grep -n 'active_skills\|"summary"' -- capabilities/persistence                    # 应为空

# 离线跑通（不需要 config.json / 联网）
python -m app.skeleton_demo && python -m app.stack_demo

# POSIX 专属用例在本机是 skip（见「未闭合」第 4 条）
python -m pytest -q providers/process/test_managed_range.py -rs
```

### 逐项核对清单

- [ ] 事件日志是唯一权威源；`derive_messages` 纯函数可重放；无并行旁路。
- [ ] 写盘只有 `flush()` 返回才作崩溃承诺；写失败留队列可重试；无半条记录。
- [ ] 三个检查点在副作用**之前**落盘，订阅者抛错即中止本轮（fail-closed）。
- [ ] 沙箱缺失或强制不了策略时**拒绝执行**，绝无静默降级路径。
- [ ] 终止后进程树（含后代）真的没了，且用**系统侧**手段确认过，而不是信被测代码的返回值。
- [ ] 中止结局与成功同构、走同一条通道；`deny` / `timed_out` / `cancelled` / `failed` 可区分。
- [ ] `Loop` 零策略（见上「自证命令」第一条）。
- [ ] 落位与依赖方向符合 `docs/packaging.md`。

### 值得重点怀疑的地方（有意取舍，不是遗漏）

1. **`EnvSandbox` 不是完整沙箱**：只收敛**环境变量**。不隔离文件系统、不强制断网，白名单还留着 `HOME`/`USERPROFILE`，子进程 cwd 就是 agent 的 cwd——**一次被人工批准的 `run_command` 可以用 `python -c` 读走仓库根的 `config.json` 或 `~/.aws/credentials`**。已写进 `docs/miniharness.md` 与后端 docstring 的「它不做什么」。这是规格裁定的范围（「沙箱只包 argv」），不是漏做。
2. **审批的钥匙是字面工具名**：装配层写死 `approval_required={RUN_COMMAND_NAME}`，审批者按同一名字裁决。若某个技能把命令工具注册成**别的名字**，它 `pre-execute` 直接得 `allow`，根本不进审批。要堵得改成按「是否执行外部命令」这一**属性**裁决。
3. **`turn/end` 没有取消态**：取消结果只落在 `tool/result.status == cancelled` 与文本上；纯文本路径（取消后模型只回文本）用一条 assistant 消息承担「不静默」，`turn/end` 仍是 `done`。理由：`turn/end` 的词表归 Loop，策略不代写。
4. **Windows 没有「宽限 → 强杀」升级档**：`TerminateJobObject` 一次到底，`grace_ms` 在那里只是「等工具体静止」的上限。升级档只存在于 POSIX 后端。
5. **`timed_out` 默认可重试**（T5 裁定）→ 默认装配下超时工具会重试 4 次、退避约 7 秒。改 `RETRYABLE_OUTCOMES` 一行即可调整。
6. **每次工具调用会换一次 `process` 服务**（登记代理）：`ctx.provide` 的 disposer 现已自摘，所以 effect 账本不再随调用次数增长（可自证：跑 N 次工具调用，`len(ctx._effects)` 增量应为 0）。更干净的解是给 `Context` 加作用域化的临时覆盖，不在本轮范围。

### 踩过的坑（复核时会重新踩到）

1. **探针必须跑在全新进程里。** 长活的内核 / Python 进程会持有**改动前的模块**（实测：某内核里 `LOG_VERSION` 仍是 1、代码对象还指在旧行号上，而文件早已升到 2）。据它得出的「修复无效」是假警报，据它得出的「修复有效」同样是假证据。
2. **Windows 残行有 `\r` 陷阱。** 撕掉结尾 `\n` 会残留 `\r`，被 universal newlines 折成 `\n` → 旧 `read_log` 把残行当成已落盘，而写前的 `_drop_torn_tail` 又会删掉它，于是「重读水位」的重试路径会把那条事件**永久丢掉**且屏障照常返回。判定一律用**字节**（`endswith(b"\n")`）。
3. **洋葱瀑布会吃掉后来的守卫。** `tools/pre-execute` 是先注册者先跑、可提前 return 的瀑布：`PermissionPlugin._pre` 对审批名单里的工具直接 `return {"kind": "ask"}`、不调 `next_()` —— 注册在它之后的监听器**在它要守护的那个工具上恰好被整段跳过**。不得被绕过的守卫挂 `tools/guard`（在 pre-execute 决策之后、`_approve` 之前，且 `_guard` **逐个调用监听器、不走瀑布**）。
4. **`tasklist` 的输出是 GBK**：按字节比较，别 `text=True` 直接按 UTF-8 解。
5. **子进程的「独立存活确认」只该有一份实现**（`providers/process/probe.py`）。把这种探针抄成两份，等于一处漂移就让同一个进程在两处得出相反结论，依赖它的断言静默变成假证据。
6. **计划外的重试路径不经过 pre-execute / guard**：`RetryPlugin` 退避后的重试是**重新进 `tools/execute`**。凡是「本轮生效」的策略，必须在 `tools/execute` 入口也复查一次。

## 5. 未闭合的地方（如实记下，不当作已完成）

1. **【已闭合，T12】§9 已知偏差全部消化**（[\#14](https://github.com/RebornL/MiniAgent/issues/14)）：7 个 legacy `definition/` 包——`persistence`（492→42+563）、`compaction`（214→27）、`skills`（143→51）、`retry`（108→19）拆成**契约 + 实现**两包；`validation`（240）、`tracing`（98）、`timeout`（41）经通读确认**无稳定契约符号**（消费方要么缺位要么鸭子类型），整体并入 provider 撤销空壳（照 permission / final_output 先例）。user story 18「包按变化速率拆分」在全部能力上成立；细则与行数见 `docs/packaging.md` §9。
2. **skills/provider 的 8 个 legacy 死 import**（`os` / `shutil` / `time` / `Path` / `field` / `asdict` / `tiktoken` / `OpenAI`，HEAD 既有、T12 纪律下不清理）待非搬移切片统一删除。
3. **POSIX 分支在本机从未实跑**：`providers/process/test_managed_range.py` 里那条「宽限 → 强杀」升级用例带 `skipif(os.name == "nt")`，本机永远是 skip。它的失败注入只在逻辑上论证过（把强杀那步写坏会让 `terminate` 抛 `TerminationError` 而不是静默通过），**没有在 Linux 上跑过**。
4. **审批者的死路**：CLI 里的审批者已装（#12），但 `assembly.resume_session` 与直接用 `build_harness` 的调用方**不装审批者** → 那些路径上 `run_command` 仍「使能而无用」（`ask` → 无裁决者 → 默认拒绝）。安全，但不可用。
5. **`[y/N]` 批准框的残留**：取消落在 `tools/guard` 之后、而批准框已经显示时，框会留在屏上（既有行为，未改）；即使答 `y`，`tools/execute` 的入口复查也会把这次调用收成 `cancelled`（命令不跑、本轮以取消收场）。
6. **非 LIFO 的 disposer 调用**：`Context.provide` 的 disposer 现在会自摘且幂等，但**乱序**调用较早的 disposer（同一键上还有更晚的 `provide` 在栈上）时，后续 `dispose()` 会剩下更晚那次捕获的旧值（旧实现此时以键被移除收场）。仓库内所有调用都是 LIFO，该路径不可达。
7. **`agent_sessions/` 里的真实会话不入库**，`tests/fixtures/` 用的是**结构一致、内容合成**的样本。

## 6. 验收证据

- `python -m pytest -q` → **128 passed, 1 skipped**（1 skip 即第 5 节第 2 条的 POSIX 用例）。起点是 0，T1 时 26，T2/T6 后 43，T3/T5 后 58，T4/T9 后 80，T7/T10 后 100，T8 后 118，T11 后 128。
- **每一张票都过了两轴 `/code-review`**（Standards + Spec，并行子 agent，基点 = 前一提交），且**每条 P1/P2 都独立复现过**。审查在本轮抓到的真问题（都已在关闭前修掉）：
  - **T4**：`read_log` 与 `_drop_torn_tail` 对「写残尾行」的判定不一致 → 重试路径**永久丢一条事件**而屏障照常返回（第 4 节坑 2）。
  - **T11**：取消守卫被瀑布短路吃掉 → 本轮已取消时**批准框照弹、命令真执行**（第 4 节坑 3）。
  - **T8**：等待者只在「中止 / 时限」时被唤醒 → 工具体先完成时会**睡满时限**（默认 30 秒的工具延迟）。
  - **T8**：`_effects` 每次工具调用**永久遗留一条** disposer（实测 500 次调用 6→506）。
  - **T7**：文档把「凭据不外泄」当成整机保证（实为只保证**环境变量里**的凭据）。
  - **T2/T6**：首轮只测快乐路径，漏了「组长已自行退出、后代仍存活」与旧会话迁移路径。
- **独立核验**（主 session 亲自跑，不复用子 agent 的断言）：真实**祖孙两级**进程树 + 系统侧 `tasklist` 确认终止后两个 PID 均消失；真实管道下审批者拒绝且确实没读 stdin；沙箱收敛后子进程拿不到父进程的哨兵密钥（并附 `env=None` 继承的**非空洞对照**）；撕裂尾行的 5 个案例（含 Windows `\r`）；effect 账本零增量；8 块 mermaid 真 `parse()` 通过。
