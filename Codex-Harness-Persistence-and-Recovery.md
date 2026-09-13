# Codex Harness：持久化 / 恢复 / 副作用中止 / 工程结构（源码级调研）

- **调研对象**：`D:/Project/Ai/codex`，HEAD `a592c38c16cdd7623dacc9168926ebccedfb67d3`（2026-09-13），Rust 主体 `codex-rs/`。
- **方法**：只读 `grep` / `read`；**每条结论后的 `路径::符号 (行号)` 均为实际观察到的行**，未观察到的写「未找到 / 不确定」。
- **引用约定**：路径相对 `D:/Project/Ai/codex`（即 `codex-rs/...` 或根级 `AGENTS.md`）。行号指该符号/语句所在行。

---

## 一、持久化：事件流 JSONL（权威）+ SQLite（派生投影）

### 1.1 两层存储，权威源是追加式 JSONL

Codex 把会话轨迹写成**每线程一个追加式 JSONL 文件**（"rollout"），SQLite 只存**元数据与分页投影**，不存权威历史。

- 两个根子目录常量：`codex-rs/rollout/src/lib.rs::SESSIONS_SUBDIR (84)`、`codex-rs/rollout/src/lib.rs::ARCHIVED_SESSIONS_SUBDIR (85)`（值分别为 `"sessions"`、`"archived_sessions"`）。
- 存放位置：`<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<时间戳>-<线程UUID>.jsonl`，由 `codex-rs/rollout/src/recorder.rs::precompute_new_rollout_path (1700)` 组装，目录拼接见 `(1708-1712)`。
- 文件名格式：`codex-rs/rollout/src/rollout_file_name.rs::render (62)`，格式串在 `(66-73)`；同一 thread 若产生新 rollout（revert/fork）则加后缀 `_{rollout_id}`。
- `CODEX_HOME` 解析：`codex-rs/utils/home-dir/src/lib.rs::find_codex_home (13)`，默认回落到 `$HOME/.codex`（`(53-59)`，`p.push(".codex")` 在 `(59)`）。
- 冷数据压缩：后台把冷 rollout 压成 `.jsonl.zst`，`codex-rs/rollout/src/compression.rs::COMPRESSED_SUFFIX (18)`、`spawn_rollout_compression_worker (29)`；读取侧透明（`open_rollout_line_reader (45)` 同时支持 `.jsonl` 与 `.jsonl.zst`），写入前 materialize（`materialize_rollout_for_append (65)`）。

SQLite 侧：`codex-rs/state/src/sqlite.rs` 定义多个库文件常量——`STATE_DB_FILENAME = "state_5.sqlite" (33)`、`LOGS_DB_FILENAME (29)`、`THREAD_HISTORY_DB_FILENAME = "thread_history_1.sqlite" (34)`；路径 getter 见 `state_db_path (141)`、`thread_history_db_path (179)`。init 入口 `codex-rs/rollout/src/state_db.rs::init (45)` / `try_init (60)`。

### 1.2 记录形态：带类型标签的事件流，不是消息快照

- 一行一条记录：`codex-rs/history/src/lib.rs::RolloutLine (262)`，字段 `timestamp`（RFC3339 字符串）、可选 `ordinal`、以及 `#[serde(flatten)] item: RolloutItem`（`(263-267)`）。
- `RolloutItem` 是**多种事件的联合**：`codex-rs/history/src/lib.rs::RolloutItem (122)`，变体含 `SessionMeta / ResponseItem / InterAgentCommunication / Compacted / TurnContext / TokenUsageRecord / WorldState / SecurityRiskScore / RetainedContext / EventMsg / RealtimeItem`（`(123-140)`）。
- 落盘 wire 格式：内部标签 `#[serde(tag = "type", rename_all = "snake_case")]`，见 `codex-rs/history/src/rollout_payload.rs::RolloutItemWire (24)`（derive 与 serde 属性在 `(22-23)`）；序列化实现挂在类型上：`codex-rs/history/src/lib.rs::impl Serialize for RolloutItem (140)`。
- 反序列化**必须走 canonical parser**（绕开 serde flatten 的浮点缓冲问题）：`codex-rs/rollout/src/lib.rs::decode_rollout_line (50)`、`parse_rollout_line (75)`、`parse_rollout_line_bytes (80)`；`RolloutLine` 故意不实现 `Deserialize`（注释见 `codex-rs/history/src/lib.rs (257-260)`）。
- 事件流中包含**压缩检查点**（`RolloutItem::Compacted`）、世界状态、token 用量等"executive markers"，见 `codex-rs/rollout/src/policy.rs::is_persisted_rollout_item (10)` 的注释与 true 分支 `(17-24)`。

### 1.3 写盘时机：事件驱动入队 → 后台 writer task → 屏障式 flush

- 每次产生 rollout item 就 append：`codex-rs/rollout/src/recorder.rs::record_canonical_items (1013)`，实际只是把 `RolloutCmd::AddItems(items)` 丢进 mpsc（`(1017-1019)`）——**非阻塞、不保证已落盘**。
- 后台 writer 单线程消费：`codex-rs/rollout/src/recorder.rs::rollout_writer (1913)`，`AddItems` 分支先入 `pending_items` 再 `flush_if_materialized()`（`(1920-1922)`）。
- `add_items` 仅入队：`codex-rs/rollout/src/recorder.rs::RolloutWriterState::add_items (1764)`；`flush_if_materialized (1768)` 在 `is_deferred()`（延迟创建、尚未物化）时直接返回。
- 真正写盘：`write_pending_once (1869)` → 先补写 `SessionMeta`（`write_session_meta_if_needed (1854)`），逐条 `write_rollout_item`（`write_pending_items_once (1881)`），最后 `file.flush()`（`(1876)`）。单行写出 `write_line (2068)`（`serde_json::to_string` + `\n`，`(2069-2071)`）。
- 持久化屏障 API：`persist (1031)`、`flush (1052)`、`shutdown (1154)`、`discard (1179)`；注释明确语义 `(1027-1030)`、`(1046-1050)`。
- **失败恢复策略**：`write_pending_with_recovery (1795)` 失败后 `enter_recovery_mode`（丢弃文件句柄、**保留未写入的 pending 后缀**）并重试一次；`RolloutWriterState` 的文档注释 `(1748-1750)`、`enter_recovery_mode (1826)`。这保证「崩溃后重开文件续写，不丢已入队项」。
- 调用点（谁在写）：`codex-rs/core/src/session/mod.rs::persist_rollout_items (4348)`（失败只 `error!` 不中断回合，`(4351)`）、`codex-rs/core/src/codex_thread.rs::append_rollout_items (746)`（实际下沉 `append_items` 在 `(753)`）。
- **写入过滤策略**：不是所有事件都落盘。`codex-rs/rollout/src/policy.rs::is_persisted_rollout_item (10)`、`persisted_rollout_items (29)`、`should_persist_response_item (44)`、`should_persist_event_msg (94)`。例如 `ResponseItem::AdditionalTools / CompactionTrigger / Other` 明确**不持久化**（`(61-63)`）。
- **SQLite 投影**：每次 write 后同步把新数据物化进 SQLite 历史表，`codex-rs/thread-store/src/local/live_writer.rs::write_and_project (317)`，三种写操作语义（AppendItems/Persist/Flush）见 `RolloutWriteOp` 的文档注释 `(290-299)`；投影游标 `next_rollout_byte_offset / next_rollout_ordinal` 见 `codex-rs/thread-store/src/local/thread_history.rs::projection_state (54)` 与 SQL `(69-72)`。

### 1.4 版本化与 schema 演进：**两套机制并存**

**SQLite：sqlx 迁移，向前兼容由 `ignore_missing` 兜底。**
- 六个独立 `sqlx::migrate!` 迁移集：`codex-rs/state/src/migrations.rs::STATE_MIGRATOR (6)`、`LOGS_MIGRATOR (7)`、`GOALS_MIGRATOR (8)`、`MEMORIES_MIGRATOR (9)`、`QUEUE_MIGRATOR (10)`、`THREAD_HISTORY_MIGRATOR (11)`。
- 关键兼容设计：`runtime_migrator (19)` 设 `ignore_missing: true`，注释说明「允许旧二进制打开已被新二进制迁移过的库」`(13-18)`。
- 规模：`codex-rs/state/migrations/` 共 **55** 个迁移文件（末号 `0055_thread_attachments.sql`）；`codex-rs/state/thread_history_migrations/` 共 **6** 个（末号 `0006_thread_turn_ends.sql`）。
- 曾有迁移版本号修复逻辑：`repair_legacy_recency_migration_version (56)`，直接 `UPDATE _sqlx_migrations`（`(100-115)`）。

**JSONL：无显式版本号，用「可选字段 + untagged 兼容枚举」演进。**
- 例子：旧 rollout 把数字 window number 存在 `window_id` 里，用 `WindowIdWire` untagged 枚举兼容，见 `codex-rs/history/src/rollout_payload.rs (65-71)` 注释「Older rollouts stored the numeric window number in `window_id`」。
- 新旧历史模式并存：`ThreadHistoryMode::{Legacy, Paginated}`，`codex-rs/protocol/src/protocol.rs::ThreadHistoryMode (778)`（`#[default] Legacy`，`(779-781)`）。
- 分页模式引入行序号 `ordinal`，靠**反向扫描 JSONL 末尾**恢复计数：`codex-rs/rollout/src/ordinal.rs::RolloutOrdinalState (17)`、`ordinal_state_for_rollout (31)`，并用 `subagent_history_start_ordinal` 防御「初始化中途死掉」的半拷贝前缀（注释 `(52-56)`）。
- **未找到** JSONL 层的显式 schema/version 字段（如 `schema_version`）。**不确定**：是否有独立的 rollout 迁移版本表——存在 `codex-rs/state/src/model/rollout_migration_state.rs`，但本次未展开其语义。

---

## 二、恢复：全量重放事件流（非读快照），fork/revert 均为"新文件 + 切指针"

### 2.1 resume = 读 JSONL → 反向找检查点 → 正向重放

- 入口分层：`codex-rs/core/src/thread_manager.rs::resume_thread_from_rollout (1118)`（按路径）、`resume_thread_with_history (1171)`（按已加载历史）。
- 读盘：`codex-rs/rollout/src/recorder.rs::load_rollout_items (1069)` → `get_rollout_history (1134)`，返回 `InitialHistory`。
- 类型：`codex-rs/history/src/lib.rs::InitialHistory (278)` = `New | Cleared | Resumed(ResumedHistory) | Forked(Vec<RolloutItem>)`（`(279-282)`）；`ResumedHistory (271)` 携带 `conversation_id / history: Arc<Vec<RolloutItem>> / rollout_path`。
- 重建算法：`codex-rs/core/src/session/rollout_reconstruction.rs::reconstruct_history_from_rollout (134)`——**反向扫描找最新幸存的 `replacement_history` 检查点作为 base，再正向重放尾部**（`(139)` 起）。
- 装入：`codex-rs/core/src/session/mod.rs::record_initial_history (1455)`，`Resumed` 分支 `(1483)`、`Forked` 分支 `(1541)`；经 `apply_rollout_reconstruction (1615)` 以 `HistoryReplacement::Reset` 替换内存历史，见 `codex-rs/core/src/context_manager/history.rs::HistoryReplacement (112)`（`Compaction | Reset`）。
- **分页线程另有懒加载路径**：`codex-rs/thread-store/src/local/model_context.rs::load_latest_model_context (37)`（反向扫描取最新 model context），重开写句柄 `codex-rs/thread-store/src/local/live_writer.rs::resume_thread (40)`。
- **未找到** `Session::resume` / `resume_from_rollout` 之类符号——恢复入口只在 `ThreadManager` 层。
- **不确定**：`load_latest_model_context`（thread-store）与 `reconstruct_history_from_rollout`（core）两路在什么条件下互斥/并存，本次未查清。

### 2.2 fork：复制式与引用式两套

- 复制式：`codex-rs/core/src/thread_manager.rs::fork_thread (1342)`、`fork_thread_from_history (1376)`、`fork_prepared_thread (1397)`；切分语义 `ForkSnapshot (178)`（按第 n 条 user 消息）。
- 引用式（paginated）：`codex-rs/thread-store/src/types.rs::PreparedFork (221)`、`ForkBoundary (189)`；准备 `codex-rs/thread-store/src/local/paginated_fork.rs::prepare (15)`、边界计算 `history_base_at_boundary (87)`；启动时按冻结前缀加载 `codex-rs/thread-store/src/local/model_context.rs::load_for_fork (83)`。

### 2.3 revert / rollback：不可变新 rollout + SQLite 指针 CAS

- `thread/revert`（回退到某回合之前）：`codex-rs/thread-store/src/local/revert_thread.rs::revert (19)`。
- 语义（源码注释 `(14-18)`）：**旧 rollout 文件原样保留**，新建一个不可变 rollout 文件引用保留前缀，唯一可变点是 SQLite 里的 rollout-path 指针。
- 建新 recorder：`create_replacement_recorder (144)`；指针切换用 CAS：`codex-rs/state/src/runtime/threads.rs::replace_rollout_path_if_current (403)`。
- 旧 `thread/rollback` API 已废弃，历史中只留 `EventMsg::ThreadRolledBack { num_turns }` 标记；重放时累加 `pending_rollback_turns` 并跳过：`codex-rs/core/src/session/rollout_reconstruction.rs (208)`。

### 2.4 压缩（compaction）后历史的持久化与重建

- 压缩产物是一个持久化事件：`codex-rs/history/src/lib.rs::CompactedItem (190)`，字段含 `message`、`replacement_history: Option<Vec<ResponseItemEnvelope>>`（`(192)`）、`world_state`/`retained_context`/`window_number`/`window_id`/`latest_token_usage_record` 等（`(193-215)`）。
- **单一写入点**：`codex-rs/core/src/session/mod.rs::replace_compacted_history (3943)`，构造 `vec![RolloutItem::Compacted(compacted_item)]` 落盘（`(4004)`）。
- 本地压缩入口：`codex-rs/core/src/compact.rs::run_compact_task_inner_impl (236)`；重组逻辑 `build_compacted_history (665)`；是否注入初始上下文由 `InitialContextInjection (72)` 控制。
- 远程 v2 压缩同样汇入该写入点：`codex-rs/core/src/compact_remote_v2.rs::… (338)` 调用 `sess.replace_compacted_history`。
- 重建时即走 2.1 的反向扫描：找到最后一条 `Compacted` 的 `replacement_history` 作为新 base。

### 2.5 模型可见历史 vs 持久化形态：**基本同源，但有内存专属裁剪**

- 同源：`replace_compacted_history (3943)` 是唯一写点，同一份 items 既进内存 annotated history 又落盘（`HistoryReplacement::Compaction`）。
- **不同源之处**：为塞进 context window 的裁剪**只改内存、不回写**——`codex-rs/core/src/compact_remote_history.rs::trim_function_call_history_to_fit_context_window (68)`，调用点 `codex-rs/core/src/compact_remote_v2_attempt.rs (43)`。
- `rollout-trace/` 是**独立诊断 bundle**，不在在线 resume 路径：CLI 离线重放 `codex-rs/rollout-trace/src/reducer/mod.rs::replay_bundle (44)`，压缩 trace 载荷 `codex-rs/rollout-trace/src/compaction.rs::CompactionCheckpointTracePayload (84)`。本次**未找到**在线 resume 调用 `replay_bundle` 的位置。

---

## 三、工具副作用与中止：隔离 + 拒绝检测 + 杀进程树，**无补偿/回滚**

### 3.1 执行链路

`ExecCommandHandler`（`codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs:75`）→ runtime `codex-rs/core/src/tools/runtimes/unified_exec.rs` → 统一入口 `codex-rs/core/src/exec.rs::process_exec_tool_call (297)` → `build_exec_request (321)` → 沙箱统一下沉点 `codex-rs/core/src/sandboxing/mod.rs::execute_env (210)` → `execute_exec_request (407)` → `get_raw_output_result (479)` → `exec (888)` → 真正 spawn：`codex-rs/core/src/spawn.rs::spawn_child_async (52)`。

### 3.2 超时：**返回带 `timed_out` 标记的结构体，包成 `Err(SandboxErr::Timeout)`**，而非独立事件

- 超时用类型表达：`codex-rs/core/src/exec.rs::ExecExpiration (151)` = `Timeout(Duration) | DefaultTimeout | Cancellation(CancellationToken) | TimeoutOrCancellation{…}`；判定结果 `ExecExpirationOutcome (163)` = `TimedOut | Cancelled`。
- 默认超时 **10 秒**：`DEFAULT_EXEC_COMMAND_TIMEOUT_MS (61)`。
- 超时退出码沿用约定 **124**：`EXEC_TIMEOUT_EXIT_CODE (68)`，赋值点在 `finalize_exec_result (749)` 内 `(773)`。
- 超时错误类型：`codex-rs/protocol/src/error.rs::SandboxErr (37)`，含 `Denied{output, network_policy_decision} (43)`、`Timeout { output } (60)`、`Signal(i32) (64)`。成功输出结构 `ExecToolCallOutput` 自身带 `timed_out` 标记（`codex-rs/protocol/src/exec_output.rs`）。
- 长驻/PTY 路径同样置标记并终止：`codex-rs/core/src/unified_exec/process_manager.rs (625-628)`。

### 3.3 取消/中断传播与杀进程树

- 取消源：`codex-rs/core/src/tasks/mod.rs::handle_task_abort (878)` —— 先 `cancellation_token.cancel()`，等待 grace **100ms**，再 `handle.abort()`（AbortOnDrop），最后发 `EventMsg::TurnAborted(TurnAbortedEvent)` `(963)`。
- 输出消费侧裁决：`codex-rs/core/src/exec.rs::consume_output (949)` —— `TimedOut` → 直接 `kill_child_process_group`；`Cancelled` → 先 `terminate_process_group`(SIGTERM) 并等 `CANCELLATION_TERMINATION_GRACE_PERIOD`（**50ms**，`(69)`，使用点 `(1019-1023)`），未退出再 SIGKILL。
- Unix 杀进程树：`codex-rs/utils/pty/src/process_group.rs::kill_child_process_group (289)` → `kill_process_group_by_pid (90)`（`libc::killpg(SIGKILL)`）；另有 `terminate_process_group (230)`、`interrupt_process_group (253)`。
- 进程隔离前置：`spawn_child_async (52)` 在 `pre_exec` 里 `setsid`（detach_from_tty）+ `PR_SET_PDEATHSIG`（父死子死），并 `kill_on_drop(true)`。
- Windows 杀进程树：Job Object，`codex-rs/utils/pty/src/win/job.rs::JobObject (34)`，配 `KILL_ON_JOB_CLOSE`。
- 远端 exec-server：`codex-rs/exec-server/src/connection.rs::kill_process_tree (217)`。
- **未找到** SIGSTOP/挂起-恢复式暂停；取消一律是 token + 杀树。

### 3.4 沙箱隔离与拒绝上报

- 类型与平台选择：`codex-rs/sandboxing/src/manager.rs::SandboxType (42)` = `None | MacosSeatbelt | LinuxSeccomp | WindowsRestrictedToken`；`get_platform_sandbox (67)` 按 OS 选。
- 门面：`codex-rs/sandboxing/src/manager.rs::SandboxManager (284)`（**注意：不在 crate 根 `lib.rs`**）。
- macOS：Seatbelt（`codex-rs/sandboxing/src/seatbelt.rs`，走 `/usr/bin/sandbox-exec` + sbpl）。
- Linux：`codex-rs/linux-sandbox/`（bubblewrap + Landlock/seccomp）；参数由 `codex-rs/sandboxing/src/landlock.rs::create_linux_sandbox_command_args_for_permission_profile (23)` 生成。
- Windows：`codex-rs/windows-sandbox-rs/`（受限令牌 + ACL allow/deny ACE）；另有默认未启用的 `codex-rs/mxc-sandbox/`（独立 Windows 容器后端）。
- 拒绝检测：`codex-rs/sandboxing/src/denial.rs::is_likely_sandbox_denied (13)`（关键字 + LinuxSeccomp `SIGSYS`）。
- 拒绝上报：`codex-rs/sandboxing/src/violation.rs::record_filesystem_sandbox_violation (186)`，按 backend 分类（Seatbelt/LinuxSandbox/WindowsSandbox/ManagedNetworkProxy）。
- 拒绝后的动作：**审批 → 二次「无沙箱」attempt**，`codex-rs/core/src/tools/orchestrator.rs (467-520)`（`SandboxAttempt` 重试在 `(467)`，「Second attempt」在 `(484)`），前提是工具 `escalate_on_failure()`（默认 true，`codex-rs/core/src/tools/sandboxing.rs (344)`）。

### 3.5 「调用—结果成对」如何保证

- 审批需求分级：`codex-rs/core/src/tools/sandboxing.rs::ExecApprovalRequirement (152)` = `Skip | NeedsApproval | Forbidden`；工具错误 `ToolError (358)` = `Rejected(String) | Codex(CodexErr)`。
- 审批决议 → 结果：`codex-rs/core/src/tools/approvals.rs::ApprovalResolution::into_tool_result (435)`：
  - `ReviewDecision::Denied` → `Err(ToolError::Rejected)` `(456)`
  - `ReviewDecision::TimedOut` → `Err(ToolError::Rejected)` `(457)`
  - `ReviewDecision::Abort` → `Err(ToolError::Codex(CodexErr::TurnAborted))` `(460)`
- **中断必补一个 tool result**：`codex-rs/core/src/tools/parallel.rs::aborted_response (273)`、`abort_message (284)`（如 `"Wall time: {secs:.1} seconds\naborted by user"` `(286)`）；结果载荷类型 `codex-rs/core/src/tools/context.rs::AbortedToolOutput (322)`。这是「call_id ↔ result 一一对应」的兜底。
- 反例（**已知的不成对路径**）：`codex-rs/core/src/stream_events_utils.rs` 中只有 `FunctionCallError::RespondToModel` 会转成 `FunctionCallOutput`（`(383-403)`）；`FunctionCallError::Fatal` 直接 `return Err(CodexErr::Fatal(message))`（`(406-407)`），**不补结果项**。**不确定**：这条路径是否会在持久化 rollout 里留下孤儿 tool call（未实际构造该场景验证）。

### 3.6 有没有补偿/回滚副作用？**没有**

- 安全模型是「隔离 + 拒绝检测 + 杀进程树」，**未找到**任何文件系统级的 undo/事务/自动 git revert 机制。
- 仅有的"rollback"都是**会话/上下文层**：`ThreadRolledBack`（见 2.3）。
- 唯一接近"补偿"的是沙箱自身的 ACL 一致性清理：`codex-rs/windows-sandbox-rs/src/deny_read_acl.rs::apply_deny_read_acls (66)`——本调用内施加的 deny-read ACE 在返回错误前撤销。这是沙箱内部卫生，不是业务回滚。

---

## 四、工程目录结构：147-crate 单一工作区，能力按"薄门面 + 平台后端"落位

### 4.1 工作区组织

- **单 Cargo 工作区**，`codex-rs/Cargo.toml` `[workspace] (1)`，`members` 列表 `(2-150)` 共 **147** 个成员（实测计数 147），`resolver = "2" (151)`。
- 统一 package 约定：`[workspace.package] (153)`，`edition = "2024" (159)`，注释说明新 crate 用 `cargo new -w` 自动继承 edition。
- **命名规则（硬性）**：目录名多数是裸名（`core/`、`sandboxing/`、`rollout/`），但 **crate 名一律 `codex-` 前缀**，见 `AGENTS.md (5)`：「Crate names are prefixed with `codex-`」。
- members 列表**非字母序**，是人工簇状维护；**未找到**自动化排序规则。
- 分层（代表，非全量）：
  - **protocol 层**：`protocol/`、`app-server-protocol/`、`exec-server-protocol/`、`code-mode-protocol/`（不依赖 core）。
  - **client/transport 层**：`codex-client` → `codex-http-client`；`codex-api` 依赖二者。
  - **领域/持久化层**：`history/`（纯领域类型）→ `state/`（SQLite）→ `rollout/`（JSONL 持久化与发现）→ `thread-store/`（存储中立接口，`codex-rs/thread-store/src/lib.rs (1)`）。
  - **沙箱层**：`sandboxing/`（门面）→ `linux-sandbox/`、`windows-sandbox-rs/`、`mxc-sandbox/`、`bwrap/`、`process-hardening/`。
  - **表面层**：`tui/`、`cli/`、`exec/`（headless CLI）、`app-server/`。
  - 扩展 `ext/*`（14 个）、工具 `utils/*`（30+）。
- **Bazel 与 Cargo 的关系有明文**：`codex-rs/docs/bazel.md (4-5)`「Cargo remains the source of truth for crates and features, while Bazel provides hermetic builds」；crate 宏 `defs.bzl::codex_rust_crate (184)`。

### 4.2 「能力」落在哪个 crate

| 能力 | 主 crate / 位置 | 一句话职责（来源） |
|---|---|---|
| 工具定义（可脱离 core） | `tools/` | `codex-rs/tools/src/lib.rs (1)`「Shared tool definitions and Responses API tool primitives」 |
| 工具运行 / 编排 / 审批 | `core/` | `codex-rs/core/src/tools/mod.rs (1)` `mod approvals;`；审批无独立 crate |
| 沙箱门面 | `sandboxing/` | `codex-rs/sandboxing/src/manager.rs::SandboxManager (284)` |
| 平台沙箱实现 | `linux-sandbox/`、`windows-sandbox-rs/`、`mxc-sandbox/` | 各平台强制机制 |
| 命令策略 | `execpolicy/` + core | `codex-rs/execpolicy/Cargo.toml (6)`「prefix-based Starlark rules for command decisions」 |
| rollout 持久化 | `rollout/` | `codex-rs/rollout/src/lib.rs (1)`「Rollout persistence and discovery for Codex session files」 |
| 存储中立接口 | `thread-store/` | `codex-rs/thread-store/src/lib.rs (1)`「Storage-neutral thread persistence interfaces」 |
| SQLite 状态 | `state/` | `codex-rs/state/src/lib.rs (1)`「SQLite-backed state for rollout metadata」 |
| 历史领域类型 | `history/` | `codex-rs/history/src/lib.rs (1)`「Model-history and persisted-rollout domain types」 |
| 压缩 / 上下文 | `core/` | `codex-rs/core/src/compact.rs::build_compacted_history (665)` |

### 4.3 测试组织

- **单测**：同 crate 内的独立 sibling 文件（不是 inline `mod tests`），用显式 `#[path]`——硬性规则见 `AGENTS.md (169-170)`：「define its contents in a separate sibling file」「Use an explicit `#[path = "..._tests.rs"]`」；`AGENTS.md (120-121)` 禁止在主实现里塞 test-only 函数。
- 命名规律：被测模块名 + `_tests.rs`（`compact.rs` → `compact_tests.rs`，`exec_policy.rs` → `exec_policy_tests.rs`）。
- **集成测试**：`AGENTS.md (114)`「Integration tests are under `core/suite`」，实际路径为 `codex-rs/core/tests/suite/`（**文档与实际不一致**）；同类还有 `codex-rs/app-server/tests/suite/`、`codex-rs/exec/tests/suite/`。
- 测试辅助 crate：`codex-rs/test-binary-support/`、`codex-rs/core/tests/common/`（`core_test_support`）、`codex-rs/exec-server/tests/support/`。

### 4.4 成文规范

- **权威规范**：根 `AGENTS.md`（320 行）。关键硬性条款（均已核对原文）：
  - crate 名 `codex-` 前缀 `(5)`。
  - 模块 <500 LoC；文件超 ~800 LoC 应新建模块 `(51-53)`。
  - **「resist adding code to codex-core」** `(76)`。
  - 注入模型上下文 6 条铁律：不改写历史 / 避免 cache miss / 必须有界硬上限 / 单条 ≤10K token / 大片段定级 P0 / 必须是 `core/context` 里的 struct `(95-98)`。
  - 测试：agent 逻辑优先集成测试 `(114-116)`；单测放 `*_tests.rs` `(120-121)`。
  - 变更规模：机械变更 ≤800 行，复杂逻辑 ≤500 行 `(127-128)`。
- **风格/静态检查**：`codex-rs/rustfmt.toml`（`edition = "2024"` `(1)`、`imports_granularity = "Item"`）；`codex-rs/clippy.toml`（`allow-expect-in-tests`/`allow-unwrap-in-tests` `(1-2)`、`await-holding-invalid-types` `(3-7)`、`large-error-threshold = 256` `(32)`）；`codex-rs/deny.toml`（cargo-deny：advisories/licenses/bans；`async-trait` 与 `reqwest` 均只允许白名单 wrapper，`(226-228)`、`(237-242)`）。
- `codex-rs/docs/` 只有 2 个文件（`bazel.md`、`protocol_v1.md`）；**未找到**架构总览或 `codex-rs/CONTRIBUTING.md`。
- 仓库级 `docs/contributing.md (5)` 明确「**We do not accept external code contributions or pull requests.**」。

---

## 五、不确定 / 未找到

- **未找到** JSONL rollout 的显式 schema 版本字段；其演进靠可选字段 + untagged 兼容枚举（`codex-rs/history/src/rollout_payload.rs (65-71)`）。`codex-rs/state/src/model/rollout_migration_state.rs` 的迁移语义本次未展开。
- **未找到** `Session::resume` / `resume_from_rollout` 符号（恢复入口在 `ThreadManager`）。
- **不确定**：分页线程 `load_latest_model_context` 与 core `reconstruct_history_from_rollout` 两路何时互斥/并存。
- **不确定**：`Fatal` 类工具错误不产生结果项时，是否会在流式协议层留下孤儿 tool call（未验证）。
- **未找到**任何文件系统级副作用补偿/事务/自动 git revert 机制；只有会话历史层面的回退。
- **未找到** SIGSTOP 式「暂停—恢复」进程控制。
- `codex-rs/mxc-sandbox` 的进程树取消仅在 README 描述，本次未逐行核实。
- `codex-rs/shell-command/` 定位为命令解析 / 安全判定 / shell 快照，**未发现**执行、超时或杀进程逻辑。
- `AGENTS.md (114)` 称集成测试在 `core/suite`，实际为 `codex-rs/core/tests/suite/`（文档与实现不一致）。
- 本次未系统排查 `state/`、`memories/`、`context-fragments/`、`core-api/` 是否存在其他恢复入口。

---

## 六、对 MiniAgent 的可借鉴点（按代价排序）

1. **【代价：小】把「持久化」显式拆成权威事件流 + 派生索引两层，并让派生层可丢弃重建。**
   Codex 的权威源永远是 `sessions/**.jsonl`（`codex-rs/rollout/src/lib.rs::SESSIONS_SUBDIR (84)`），SQLite 只做列表/搜索/分页投影（`codex-rs/state/src/sqlite.rs::STATE_DB_FILENAME (33)`）。MiniAgent 目前只有 `Persistence.py` + `agent_sessions/`：收益是索引损坏时可直接重建，代价只是多写一个「从 JSONL 重建」的函数。

2. **【代价：小】写入走「入队 → 后台 writer → 显式屏障」，并把失败项留在队列里重试。**
   `RolloutCmd::AddItems` 非阻塞（`codex-rs/rollout/src/recorder.rs (1017-1019)`），`write_pending_with_recovery (1795)` 失败后丢句柄、保留 `pending_items` 再重试。MiniAgent 直接同步写文件，崩溃即丢半条记录。改造只需一个写队列 + `flush()` 屏障。

3. **【代价：小】中断/取消时必须为每个未完成的 tool call 补一条「已中止」结果。**
   `aborted_response (273)` / `AbortedToolOutput (context.rs:322)` 保证 call_id ↔ result 一一对应，否则重放历史时协议层会出现孤儿调用。MiniAgent 的 `AgentTrace`/`CallFunc` 若靠消息序列重建，这一条是正确性硬需求。

4. **【代价：中】把「超时」做成返回值上的显式标记 + 约定退出码，而不是抛裸异常。**
   `ExecExpirationOutcome::{TimedOut, Cancelled}` (`codex-rs/core/src/exec.rs (163)`) 区分「超时」与「用户取消」，exit code 124 (`(68)`)，错误类型 `SandboxErr::Timeout` (`codex-rs/protocol/src/error.rs (60)`)。MiniAgent 可照抄这个三元组：区分 timed_out / cancelled / failed，才能做出不同的重试与呈现策略。

5. **【代价：中】恢复用「反向找检查点 + 正向重放」，并让压缩产物本身成为可重放的持久化事件。**
   `reconstruct_history_from_rollout (134)` 先反向找最后一条 `Compacted.replacement_history` 再正向补齐；`replace_compacted_history (3943)` 是内存与磁盘的唯一交汇点。MiniAgent 的 `Compaction.py` 已产出摘要，只需把摘要连同 replacement history 作为一条带类型的记录落盘，即可获得可恢复性。

6. **【代价：大】回退（undo）用「不可变新文件 + 指针 CAS」实现，而不是原地改写历史。**
   `revert_thread::revert (19)` 保留旧 rollout、新建引用式文件，唯一可变点是 SQLite 指针，用 `replace_rollout_path_if_current (403)` 做 CAS。这需要先有 1/2 两层存储与稳定 thread id 才能落地，故列为大代价；但它把「回退」从"危险的历史改写"变成"廉价的指针切换"。
