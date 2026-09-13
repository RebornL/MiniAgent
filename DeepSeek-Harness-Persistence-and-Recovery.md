# DeepSeek Harness（dsh）调研：持久化 / 恢复 / 工具副作用 / 工程结构

调研对象：`D:/Project/Ai/deepseek-harness`（TypeScript monorepo + Cordis 插件运行时，pnpm workspace）。
所有引用路径均相对该仓库根；行号为调研当日 checkout 的实际行号。未验证到的一律标「未找到 / 不确定」，不做推测。

## 结论速览

1. **持久化的唯一权威是 append-only 事件日志**，LLM 消息历史只是从日志派生的视图，从不单独存储（§1.1）。
2. **物理形态是「首行 header + 每行一条事件」的 JSONL，默认逐批 Zstandard 压缩帧**，文件名按格式代际不可变（`session.v3.jsonl.zstd`），写盘走 200ms 写后缓冲 + `session/flush` 强同步屏障（§1.2–1.3）。
3. **恢复 = 读全量日志 → 合成「中断轮次闭合事件」写回 → 用整份日志做 seed 重建 Session**；崩溃日志不截断，只丢弃未落盘的物理残尾（§2.1）。
4. **中止不杀同进程代码，只保证「结果替换 + 成对补齐」**：取消/超时都被规范化为 `isError` 工具结果（`ABORTED` / `ABORTED_BEFORE_DISPATCH` / `TOOL_TIMEOUT`），进程级终止由沙箱与 subprocess 提供方用 TERM→KILL 完成（§3）。
5. **工程上「能力 seam」有三个具名角色（Service Definition / Service Provider / Consumer）**，按变化速率拆包；`packages/AGENTS.md` 与 `packages/README.md` 是成文包规范，测试由 `packages/test-support` 提供无密钥基础设施（§4）。

---

## 1. 持久化

### 1.1 存「事件」，不存「投影后的消息」

- `Session` 是内存中的 append-only 事件日志，是整个交互历史的唯一真源；LLM 消息历史是**派生**的，`replay = 重新派生`。
  - `docs/subsystems/session.md::Summary (行 5)`
  - `packages/core/session/src/index.ts::Session.deriveMessages (行 832-860)`：按 surface 节点逐个 fold `deriveEventMessage`，只缓存节点投影，`replaceGeneration` 变化即整体重建。
- 持久化 seam 直接复用同一 `SessionEvent` 类型，**没有并行的「持久化事件类型」**。
  - `docs/subsystems/persistence.md::The seam (行 7)`
  - `packages/session/session-persistence/src/index.ts::SessionPersistence (行 135-198)`：抽象服务只暴露 `create` / `open` / `stat` / `list`，`create`/`open` 返回 per-session 的 `SessionHandle`（`read`/`append`/`flush`/`close`），所有读写都过 handle，单写者所有权由 handle 承载。
  - `packages/session/session-persistence/src/handle.ts::SessionHandle (行 59-...)`
- 投影（projection）另有**可选的持久化缓存**，不是真源：`(sessionId, key, ver, seq, val)` 行，`ver` 不匹配即丢弃、从不迁移。
  - `packages/session/session-projection-cache/src/spec.ts::checkpointRow (行 26-31)`、`::checkpointRecord (行 66-69)`、`::projectionCacheDomainSpec (行 98-101)`

### 1.2 物理格式与存放位置

- JSONL：每个事件一行，`JSON.stringify` 编码（`encodeCurrentEvent` 做格式代际转换）；首行是独立 header 记录。
  - `packages/session/session-persistence-jsonl/src/format.ts::eventLine (行 321-323)`、`::eventLines (行 312-314)`、`::toHeaderLine (行 118-...)`、`::scanLog (行 530-534)`
- header 行字段：必填 `type/version/id/createdAt/isSeeded/delegationDepth`，可选 `cwd/parentSession/origin/agentPreset`。
  - `packages/session/session-persistence-jsonl/src/format.ts::HEADER_REQUIRED_KEYS / HEADER_OPTIONAL_KEYS (行 95-97)`
  - 逻辑 `SessionHeader` 定义（含 `parentSession` / `isSeeded` / `delegationDepth` / `agentPreset`）：`packages/core/session/src/types.ts::SessionHeader (行 93-...)`
- 目录：`<root>/<projectDir(cwd)>/<encodeSegment(sessionId)>/session.v<N>.jsonl[.zstd]`；无 cwd 落到 `_no-cwd`；session id 做单段路径转义。
  - `packages/session/session-persistence-jsonl/src/format.ts::projectDir (行 253-256)`、`::sessionDir (行 266-268)`、`::generationLogPath (行 279-...)`、`::logPath (行 297-...)`
  - 文件名规则（v0 = `session.jsonl`，之后 `session.vN.jsonl`）：`packages/session/session-format/src/filename.ts::sessionFormatLogFilename (行 14-17)`
- 压缩：默认 `zstd`（带校验和的 Zstandard 帧），可配 `none`；header 与首批事件**分成两个独立帧**写入。
  - `packages/session/session-persistence-jsonl/src/index.ts::DEFAULT_COMPRESSION (行 66)`、`::Config (行 88-99)`
  - `packages/session/session-persistence-jsonl/src/index.ts::encodeMaterialization (行 1207-1220)`、`::encodeEventBatch (行 1224-1227)`、`::appendLines (行 1246-...)`
- root 是**必填配置**（无默认值），理由是 `process.cwd()` 默认会把会话文件散落到各处。
  - `packages/session/session-persistence-jsonl/src/index.ts::Config.root (行 88-96)`、构造里 `resolve(config.root)` 固定一次（行 269-271）
  - 实际部署值：`packages/bundle/base/cordis.patch.yml` 中 `session-persistence-jsonl.config.root = dshHomePath('sessions')`（行 110-113）；`sdk-minimal` 用 `compression: none`（`packages/bundle/sdk-minimal/cordis.patch.yml` 行 151-155）

### 1.3 写盘时机：写后缓冲 + 显式 flush 屏障

- `session/event` 是**提交之后的同步通知**（事件先 push 进内存日志，再派发回调）。
  - `packages/core/session/src/index.ts::'session/event' (行 72)`、append 的 push→invoke 顺序 (行 746-753)
- 后端把事件按 session id 路由进活动写 handle 的**有界写后缓冲**：首个待写事件启动固定批量窗口（常量 200ms），后续事件加入但**不重置** deadline；到期才开始一次 `append`。
  - `packages/session/session-persistence-jsonl/src/storage.ts::LIVE_WRITE_BATCH_MAX_DELAY_MS (行 36)`、`::enqueueLive 定时器 (行 276-281)`、`::drainLive (行 288-...)`
  - `packages/session/session-persistence-jsonl/src/storage.ts::install (行 534-547)`：安装 `session/event`、`session/flush`、`session/disposed` 三个监听。
- `session/flush` 是**唯一的持久性屏障**：drain 缓冲到静止，保证已确认的 append 通过崩溃、且会话对其他进程可见。`SessionStore.flush` 是唯一入口（内部走 `ctx.parallel('session/flush')`）。
  - `packages/session/session-persistence-jsonl/src/storage.ts::flush (行 203-209)`、`::flushAll (行 508-523)`
  - `packages/core/session/src/index.ts::SessionStore.flush (行 1131-1150)`
- 语义检查点策略（`session-checkpoint-policy`）在三个边界前 flush，且**失败即 fail-closed**（不调用下游）：
  - 模型请求前（`llm/stream` 下游 stream 的第一次拉取前）
  - 顶层工具派发前（`tools/execute`，嵌套 PTC 子调用复用外层已持久化的 call）
  - 每个 step 前（`agent/pre-step`）
  - `packages/session/session-checkpoint-policy/src/index.ts::apply (行 40-...)`、`::afterCheckpoint (行 20-25)`
- append 只承诺「被接受、有序、对同一后端实例可见」；**只有 resolved 的 flush 才承诺崩溃存活**。
  - `packages/session/session-persistence/src/handle.ts::SessionHandle.append (行 97)`、`::flush (行 109)`

### 1.4 版本化与迁移

- 当前写者版本是**代码里的单一手维护常量**：`SESSION_FORMAT_VERSION = 3`；最新已发布格式记录在文档（`latestReleasedVersion: 3`）。
  - `packages/core/session/src/types.ts::SESSION_FORMAT_VERSION (行 88)`
  - `docs/session-format-status.md::Sources of truth (行 19-21)`、`::Release record (行 28-31)`
- header 携带 `version`，历史物理 header 在进入逻辑接口**之前**被翻译；`stat`/`list` 只分类最高代际、不读或改 body。
  - `packages/core/session/src/types.ts::SessionHeader.version (行 98)`
  - `docs/subsystems/persistence.md::Format refusal — logs a build cannot faithfully read (行 187-189)`
- 迁移是**相邻代际链**，每代一个包：`packages/session/session-format-v0-to-v1`、`session-format-v1-to-v2`、`session-format-v2-to-v3`；catalog 由生成器派生并校验「相邻迁移能到达当前版本」。
  - `docs/session-format-status.md::Sources of truth (行 19)`
  - `packages/session/session-format-catalog/src/current.ts::validateInstalledCurrentSessionArtifact (行 37-45)`
- 代际文件名不可变：历史文件不被改写；**写打开只发布当前格式的后继代**（临时文件 + fsync + 独占发布）。
  - `packages/session/session-persistence-jsonl/src/generation.ts::prepareJsonlMigration (行 1000-1003)`、`::publishCurrentExclusive (行 812)`、`::writeSyncedTemp (行 710)`、`::readStableJsonlFile (行 251)`
  - `packages/core/session/README.md::Known Limitations (行 184)`
- 事件信封带 `ignorable?: true`：读到**不认识的、非 ignorable 的事件必须拒绝重建**，而不是静默丢弃（默认「过度拒绝」优于「静默恢复被掏空的会话」）。
  - `packages/core/session/src/types.ts::SessionEvent.ignorable (行 483)`
- 未知事件类型用 `default` 兜底，**不得 `assertNever`**（`SessionEventMap` 可被插件合并扩展）。
  - `docs/subsystems/session.md::(行 273)`

### 1.5 不确定 / 未找到

- 未见「压缩/加密 at-rest 策略」的成文规定；JSONL 只有 zstd 帧可选，未见 key 管理或静态加密（未找到）。
- 未见对「单条事件行大小上限」的显式约束（`isJsonValue` 只保证无损 JSON 可序列化）。
  - `packages/session/session-persistence/src/storage-contract.ts::materializeAppendBatch (行 131-136)`

---

## 2. 恢复

### 2.1 会话恢复：读全量日志 → 合成闭合事件 → seed 重建

`ctx.agents.resume({ resumeSessionId, ... })` 的完整路径（agent-loop）：

1. **先拿写所有权**：`persistence.open(id, 'write')`，用于排除并发 resume（同进程内活 handle 已持有声明）。
2. **读全量日志**：`handle.read(0, undefined, { signal })`。
3. **语义修复**：`interruptedTurnClosers(persisted)` 计算缺失的闭合事件，若有则 `handle.append(closers)` 用同一 handle 写回。
4. **重建**：`SessionPreparation.create(sessions.prepare(id, { seed: [...persisted, ...closers], meta, inheritedEventCount, eventState }))`。

- `packages/core/agent-loop/src/index.ts::AgentLoop.resumeWith (行 853-925)`（写所有权 `open(id,'write')` 行 877-880，read 行 889，closers 行 892-893，prepare 行 894-896，publish 行 909）
- `packages/core/agent-loop/src/index.ts::AgentLoop.resume (行 844-850)`

`interruptedTurnClosers` 的语义（这一条是「崩溃恢复质量」的核心）：

- 只为**真正开着的尾轮**产出合成事件；平衡日志返回空数组。
- 顺序：未配对的 `tool/call` 先补 `tool/result` 错误结果（`TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN`），再补开着的 `step/end`，最后补 `turn/end { reason: 'interrupted' }`。
- seq 从最后一个真实事件 +1 连续推进，`time` 复用最后一个真实事件的时间戳（确定性，不发明「未来时间」）。
- `packages/core/session/src/repair.ts::interruptedTurnClosers (行 29-...)`、`::TOOL_NOT_STARTED (行 15)`、`::TOOL_OUTCOME_UNKNOWN (行 18)`

**崩溃日志不被截断**：持久化层只丢弃「从未 resolved 的那次 append」的物理残尾；从残尾中能完整解出的记录会被写路径重写。恢复的「修 log」是**读者（agent-loop）的责任**，且在写所有权下进行（并发 `open(id,'write')` 会以 `SessionAlreadyOwnedError` 拒绝，避免修复与活轮次赛跑）。只读观察者（session-query）只在内存里做同样的 balance，不回写。

- `docs/subsystems/persistence.md::Crash recovery preserves an interrupted turn (行 108-110)`
- `packages/session/session-persistence-jsonl/src/storage.ts::truncateTornTail 相关提交顺序 (行 323-327)`、`packages/session/session-persistence-jsonl/src/index.ts::repair (行 1287-1291)`

**不是「先重建投影再恢复」**：seed 是整份事件日志，Session 的内存日志本身就是重建结果；`deriveMessages()` 只是它的视图。

### 2.2 fork / branch / rewind

- **fork 有一等 API**：`SessionStore.fork(source, boundary?, childSessionId?)`，选取截至 `boundary`（含）的前缀，要求前缀**结束在开放轮次之外**（否则抛错，不静默裁剪），子会话带 lineage 元数据。
  - `packages/core/session/src/index.ts::SessionStore.fork (行 1203-...)`
  - `packages/core/session/README.md::fork() (行 64)`、错误码 `SESSION_NOT_FOUND / SESSION_NOT_LIVE / SESSION_ALREADY_EXISTS / INVALID_BOUNDARY / OPEN_TURN`（`packages/core/session/src/index.ts::SessionForkErrorCode (行 877-883)`）
- 切点用一条 log-only 的 `session/end-seed { inherited: true }` **持久化标记**，header 只保留 `isSeeded` 布尔，精确前缀长度是 Session 状态 `inheritedEventCount`。
  - `docs/subsystems/session.md::The end-seed boundary (行 661-667)`
  - `packages/core/session/src/index.ts::ownEvents (行 654-657)`、`::isOwnSeq (行 664-666)`
- 远程/UI 面：`session.fork` RPC 支持 `atSeq`（API seam 行 202-205、行 335-338）；Web UI 的 `forkAt(seq)` 就是按 seq fork 后打开子会话。
  - `packages/api/session-controller/src/commands.ts::fork (行 202-205)`
  - `packages/client/ui-chat/src/client/apply.ts::forkAt (行 158-162)`
- **rewind：未找到服务端一等操作**。`ConversationContextOriginKind` 里有 `'rewind'`，但它是**客户端**从 surface replacement 重建「模型上下文代际」时给某代起的名字，不是服务端命令。
  - `packages/client/ui-chat/src/client/model/conversation-context.ts::ConversationContextOriginKind (行 5-13)`
- 另有成文限制：**fork 之外没有会话树**（pi 风格 entry tree 被推迟）。
  - `packages/core/session/README.md::Known Limitations (行 186)`

### 2.3 压缩（compaction）后的历史如何持久化与重建

- 压缩向 `SessionEventMap` 合并三个 **log-only** 事件：`compaction/start {turn}`（锁）、`compaction/summary {...shadowedRange, shadowedSeqs, shadowedTokenCount, provider, model, usage?}`、`compaction/end {turn, error?}`；另有 `compaction/prune`（无模型剪枝的影子价格）。
  - `packages/compaction/compaction/src/types.ts::'compaction/start' (行 24)`、`::'compaction/summary' (行 34-71)`、`::'compaction/end' (行 72)`、`::'compaction/prune' (行 82-85)`
  - `docs/subsystems/compaction.md::The compaction/* session events (行 11-21)`
- **摘要本体不走这几个事件**：它作为一条普通的 `user/message`，带 `surfaceOp: { op: 'replace', startSeq, endSeq }` 落到日志里，是唯一的面（surface）变更。`SurfaceEventType` 刻意不扩展。
  - `docs/subsystems/compaction.md (行 11)`
- **被遮蔽的原始事件留在日志中**（不删除），`deriveMessages()` 只是不再把它们投影出来，因此回放确定性成立。
  - `packages/compaction/compaction/README.md::(行 95)`
- 锁的语义：`compaction/start` 先写、`compaction/end` 最后写；崩溃留下「孤儿锁」（可检测），而不是假的「已完成」。旧的、位于更新 `session/end-seed` 之前的未配对 start 视为上一生命周期的陈旧证据而忽略。
  - `docs/subsystems/compaction.md (行 19-21)`
- 压缩是**能力 seam**（Service Definition `dsh-compaction` + Provider `dsh-compaction-basic` + Consumer `dsh-command-compact`），不属于 agent-loop 主干。
  - `docs/subsystems/compaction.md::Overview (行 3-5)`
  - `packages/compaction/compaction/src/index.ts::CompactionEngine (行 96)`、`::compactIfNeeded (行 113)`

### 2.4 投影与持久化形态的关系（`deriveMessages` 之外）

- 派生视图有两层：
  1. **临时派生**：`Session.deriveMessages()`（消息历史）、`requestContext()` 的增量折叠。
     - `packages/core/session/src/index.ts::deriveMessages (行 832)`、`::requestContext (行 800-807)`
  2. **可选持久化缓存**：`ctx.sessionProjectionCache` 把每个 projection 单元的 whole-value 状态存成 `(sessionId, key, ver, seq, val)` 行；`stateVersion` 不匹配就丢弃，**从不迁移**；恢复时用 `restoreFloor` 只回读 watermark 以下的日志尾部，检测「日志被崩溃修复截短到 watermark 之下」并退化为全量重折。
     - `packages/session/session-projection-cache/src/spec.ts::checkpointRow (行 26-31)`
     - `docs/subsystems/session-projection.md::ctx.sessionProjectionCache (行 8-9 之后；`restoreFloor`/`hydratePrepared`/`coldSnapshot` 的生成目录段)`
     - 源码：`packages/session/session-projection-cache/src/index.ts::coldSnapshot (行 277)`
- 规则：状态承载事件**携带变更后的完整状态**（whole value），不是 delta —— 放大了每次投影的简陋性，换来「最后一个写者胜」的自描述值。
  - `docs/subsystems/session-projection.md::The unit (行 9-11)`

### 2.5 不确定 / 未找到

- 未见「resume 时按 header 里的 `agentPreset` 重新组合工具/提示词」的强制校验实现（只在 header 注释里声明了「为什么必须持久化」）：`packages/core/session/src/types.ts::SessionHeader.agentPreset (行 124-127)` → 校验实现未找到。
- 未见跨进程 resume 的租约失效检测之外的并发保护细节（`SessionWriteLease.acquire` 存在，文件系统级语义未展开）。
  - `packages/session/session-persistence-jsonl/src/index.ts::acquireLease (行 872-874)`

---

## 3. 工具副作用与中止

### 3.1 取消与超时的语义：结果替换，不抛错

- 工具注册表**融合调用方信号与 around-wrapper 信号**，且**从不放弃已启动的 body promise**：已开始的 body 必须跑到静止（quiescence），其结果才被替换为取消结果；未开始的 body 不启动。
  - `packages/core/tools/src/index.ts::fuseToolSignals (行 1879-1906)`、`::dispatchToolBody (行 1522-1549)`
  - `packages/core/tools/src/index.ts::ToolDefinition.execute 契约注释 (行 218-222)`：明确写「the registry … does not abandon this promise, but it cannot hard-kill same-process code」——**同进程代码无法硬杀**。
- 取消结果是**结构化返回值**（`ToolExecutionResult`，`isError: true`），不是异常：
  - 已调用 body 后被取消 → code `ABORTED`，模型可见文本 `Error: tool call aborted`。
  - 未调用 body → code `ABORTED_BEFORE_DISPATCH`，文本 `Error: tool call aborted before dispatch`。
  - `packages/core/tools/src/index.ts::TOOL_ABORTED (行 462)`、`::TOOL_ABORTED_BEFORE_DISPATCH (行 465)`、`::toolAbortedResult (行 1909-1919)`、`::toolAbortedBeforeDispatchResult (行 1923-1933)`
  - `packages/core/tools/src/index.ts::callerCancelled (行 1500-1505)`：判定依据**始终是原始调用方信号**（不因 wrapper 换过信号而丢失）。
- **超时同样只是「返回标记」**：`timeout-policy` 是 `tools/execute` 上的 around wrapper，仅为声明了 `timeoutMs` 的工具装 deadline；自己的定时器胜出时，把（工具自己产生的）abort 结果替换为带 `TOOL_TIMEOUT` 的结构化错误结果；用 code 作用域区分内层超时与外层先触发的 deadline。
  - `packages/guard/timeout-policy/src/index.ts::TOOL_TIMEOUT (行 25)`、`::toolTimeoutResult (行 41-48)`、`::apply (行 55-80)`
- 于是「超时是否杀进程」的答案是：**timeout-policy 不杀**，它只声明 deadline 并要求工具转发 `exec.signal`；真正的终止由工具自己的能力实现（如 bash 通过 subprocess 的 managed range 做 TERM→KILL）。

### 3.2 沙箱 / 进程隔离 / 杀进程

- **沙箱 seam 只做 argv 包装**：`ctx.sandbox.confine(argv, policy)` 返回 `ConfinedArgv`（替换 argv + 强制完整性 + 两种 stderr 分类方言），调用方自己去 spawn。无法执行即 fail-closed 抛 `SANDBOX_UNAVAILABLE`，**禁止静默无约束透传**。
  - `packages/sandbox/sandbox/src/index.ts::SandboxProvider (行 158-...)`、`::SANDBOX_UNAVAILABLE (行 124)`、`::SandboxUnavailableError (行 131-...)`
  - 模式仅约束文件系统效果：`SandboxMode = 'read-only' | 'workspace-write' | 'danger-full-access'`；`SandboxEnforcement = 'full' | 'partial'` 是**上报事实**而非承诺（`packages/sandbox/sandbox/src/index.ts` 行 29 / 行 59）。
  - 策略**per call** 携带（并发会话可有不同边界，升级重试是新的一次调用）：`docs/subsystems/sandbox.md::Per-call policy`
  - 后端：bwrap / Landlock / Seatbelt / Windows ACL 受限令牌（`packages/sandbox/sandbox-local`, `packages/sandbox/sandbox-windows-acl`）；消费者是 `bash-sandbox` / `pwsh-sandbox`。
- **进程树终止是 subprocess seam 的责任**，语义写得很硬：
  - `SubprocessHandle.terminate()` 是「唯一终止动词」：幂等、range 空后为 no-op、abort 信号也会触发；`waitForExit()` 观察**同一个 managed range**（连后代一起），因此消费者可以逐级等待真实静止。
    - `packages/subprocess/subprocess/src/types.ts::SubprocessHandle.terminate (行 180-183)`、`::waitForExit (行 186-191)`
  - POSIX：先 TERM 后 KILL；Windows：直接终止（`taskkill /PID <pid> /T /F`），且「任何信号值都强杀」。
    - `packages/subprocess/subprocess-local/src/spawn.ts::killGroup (行 296-303)`、`::taskkillProcessTree (行 312-321)`、`::signalDetachedProcessTree (行 323-328)`
  - 生命周期兜底：composition teardown 调 `disposeManagedProcesses()`，等的是 **managed-range 退出**而不是直接子进程退出（防孤儿后代）；进程级 `exit` 钩子做同步最终终止。
    - `packages/subprocess/subprocess-local/src/index.ts::disposeManagedProcesses (行 94-...)`、`::teardown 注册 (行 70-74)`
  - 终止完成后留下的临时残留（如被 SIGKILL 的进程来不及清理）明确声明交给操作系统临时目录卫生处理，不做补偿。
    - `packages/subprocess/subprocess-local/src/spawn.ts::注释 (行 110-114)`
- bash 工具侧：`timeoutMs` 是模型可见参数（"executor applies its configured default and cap, and kills the command on expiry"），默认 120_000、上限 600_000、TERM→KILL 宽限 3_000ms。
  - `packages/shell/tool-bash/src/index.ts::timeoutMs schema (行 253)`
  - `packages/shell/bash-local/src/index.ts::Config (行 106-111)`、`::resolve (行 148-154)`
  - 超时/信号/非零退出在结果文本里各占一行标记，且**命令若 trap 了 SIGTERM 并 exit 0，仍报告为超时**：`packages/shell/tool-bash/src/render.ts::renderResult (行 52-57)`

### 3.3 `tool_call` 与结果如何保证成对

三层保证，逐层收紧：

1. **引用关系**：`tool/result` 用 `surfaceOp: 'append'` + `sourceEventSeqs: [callSeq]` 显式指向那条 `tool/call` 事件（并携带 `error.info`、可选工具私有 `meta`）。
   - `packages/core/agent-loop/src/tool-calls.ts::appendToolResult (行 269-289)`、`::appendToolCall (行 263-266)`
2. **取消时补齐**：调度器发现 abort 后，为**每个未开始的模型调用**补写一组合成的 `tool/call` + `tool/result`（`ABORTED_BEFORE_DISPATCH`），保证模型发出的每个 call 都有 result。
   - `packages/core/agent-loop/src/tool-calls.ts::appendSkippedToolCall (行 250-...)`、`executeToolCalls` 的 abort 分支 (行 100-104)、`runGroup` 的 abort 分支 (行 228-233)
   - 注意语义细节：调度器**内部失败**时不伪造工具结果（drain 后直接抛出），只有取消才合成。
3. **崩溃后补齐**：恢复时 `interruptedTurnClosers` 再补一次（§2.1）；核心不变式插件也声明「同一 step 的 tool call/result 配对」由 core 拥有。
   - `docs/subsystems/session.md::(行 659)`

结果提交顺序：结果与附加上下文**按模型顺序**提交（`committed` 只在连续槽位上推进），并行执行不改变提交顺序。
  - `packages/core/agent-loop/src/tool-calls.ts::runGroup (行 122)`、`::commitReady (行 147-160)`

### 3.4 副作用补偿 / 回滚

- **未找到**任何跨工具的补偿事务或回滚机制（无 saga / undo log / 两阶段提交）。设计取向是「在做出决定的那个操作内强制」+「发布状态只在提交点」。
  - `packages/AGENTS.md::Enforce a decision in the operation that makes it / Publish state only at its commit point (行 14-15)`
- 文件系统侧的实际保护是**原子性 + 新鲜度门禁**，不是回滚：
  - `fs` 的写/编辑是原子发布：写临时文件 → fsync → 原子 rename；`createIfAbsent` 用硬链接发布，绝不替换并发创建者（`FS_NOT_OBSERVED`）；`replaceIfVersion` 做 compare-and-swap（`FS_STALE_VERSION`）。
    - `packages/fs/src/fsio.ts::writeFileAtomic (行 571-...)`、`packages/fs/src/index.ts::(行 187-198)`
  - read-before-edit 是**单槽 decision waterfall**：`fs/write-intent` / `fs/edit-intent` 的第一个监听者直接决定且不调 `next()`；`fs/observed` 是同步、不可抛、发后即忘的记录事件。
    - `packages/fs/src/index.ts::'fs/write-intent' (行 58)`、`::'fs/edit-intent' (行 66)`、`::'fs/observed' (行 76)`
    - `packages/fs/fs-observation-policy/src/index.ts::ObservedStateGate (行 21-...)`、`::apply (行 106-126)`
  - **观察状态不跨 resume 持久化**：恢复的会话必须重新读文件才能通过门禁 —— 明确的已知限制。
    - `packages/fs/fs-observation-policy/README.md::Known Limitations (行 126-129)`
- 结果：dsh 对「工具已产生的副作用」采取**不去纠正**的态度，只保证「模型永远能看到一个与 call 配对的明确结局（成功 / 失败 / 取消 / 超时）」。这一点对 MiniAgent 是最值得抄的设计判断。

### 3.5 不确定 / 未找到

- **未找到** sandbox 的「强制杀进程」能力：seam 只包装 argv，终止靠 subprocess provider；`danger-full-access` 下没有任何隔离。
- **未找到**工具级「预算耗尽后保证终止」的通用实现：timeout 是协作式的（工具必须转发 `exec.signal`），声明 `timeoutMs` 即等于承诺可达静止。
  - `packages/core/tools/src/index.ts::ToolDefinition.timeoutMs 契约 (行 243-247)`

---

## 4. 工程目录结构

### 4.1 packages 如何划分与命名

- 顶层按**能力族分组**，每个包**恰好属于一个组**；组 README 是该族「权威包地图」；新包优先加入已有组。
  - `packages/README.md::Package groups (行 26-95)`、`packages/README.md::(行 27)`
  - `docs/cookbook/adding-a-package.md::Choose an existing group (行 23-25)`
- 全部 npm 包作用域为 `@deepseek-ai/dsh-*`；分组示例：`core/`（会话/提示词/工具/agent 主干）、`session/`（会话数据面 + 持久化 seam + 投影）、`storage/`（非会话存储）、`sandbox/`、`subprocess/`、`shell/`、`terminal/`、`fs/`、`llm/`、`compaction/`、`subagent/`、`jobs/`、`test-support/`、`util/` 等。
  - `packages/README.md::Package groups 表 (行 29-93)`
- 命名规则成文且强制：**按「当前存在的角色」命名**，不要用首个实现、未来扩展或 Cordis 基类命名；接口包命名能力，实现包加机制/协议/环境/厂商限定词；只有「同主机执行是契约的一部分」时才用 `local`。
  - `docs/cookbook/adding-a-package.md::Name the role that exists (行 44-46)`
- 依赖方向：**扩展插件只依赖 Service Definition，绝不依赖具体 Provider**；`dsh-agent-loop` 可替换。
  - `packages/README.md::Dependencies (行 95)`
- 包级硬约束（由 `pnpm run constraints` 强制）：`private: true`、版本跟根 `package.json` 一致、`type: module`、`main: lib/index.js`、`types: lib/types/index.d.ts`、`@deepseek-ai/cordis` 同时进 peer 与 dev 依赖等。
  - `docs/cookbook/adding-a-package.md::package.json invariants (行 25)`

### 4.2 「能力 seam」如何落位

三个**具名角色**（文档要求首字母大写）：

1. **Service Definition** — 拥有 `ctx.<key>` 的 Cordis `Service` 与词汇类型；只依赖契约需要的词汇；可以是抽象类或具体注册表服务，**永远不是 TS interface**。
2. **Service Provider** — 提供/注册实现的插件。
3. **Consumer** — 模型与插件编程所对的面（工具 schema 等）；注入 service key，绝不 import provider 类型。

- `.agents/notes/implemented/architecture/2026-06-13-capability-seams.md::Decision (行 15-25)`
- 「seam」指这三者的整体，不是接口；`packages/shell`（`dsh-shell` / `dsh-bash-local`+`dsh-bash-sandbox` / `dsh-tool-bash`）是模板。
  - `.agents/notes/implemented/architecture/2026-06-13-capability-seams.md::Terminology (行 29)`
- 拆分原则是**变化速率**而非教条：角色独立演进才分包；只有一个可能的 provider 和一个 Consumer 就先合一个包，不预防性拆分。
  - `.agents/notes/implemented/architecture/2026-06-13-capability-seams.md::Decision (行 25)`
  - `packages/AGENTS.md::Design Service Definitions for all current Consumers (行 10)`
- 落位实例（本次调研覆盖的）：`dsh-session-persistence`（Definition）+ `dsh-session-persistence-jsonl`（Provider）；`dsh-storage`（hub）+ `dsh-storage-json`/`-sqlite`（Provider）+ `dsh-storage-domain`（Consumer 数据形态）；`dsh-sandbox`（Definition）+ `dsh-sandbox-local`/`-windows-acl`；`dsh-subprocess` + `dsh-subprocess-local`；`dsh-compaction` + `-basic` + `command-compact`。
  - `docs/subsystems/persistence.md::The seam (行 7)`、`docs/subsystems/storage.md::引言段 (行 3)`、`docs/subsystems/sandbox.md::引言段 (行 3)`

### 4.3 测试如何组织

- 测试**放在包级 `tests/` 目录**，不用 `src/__tests__/`；`src/types.ts` 只放类型，无运行时代码。
  - `packages/AGENTS.md::Naming rules (行 24-25)`
- 面向产品可见行为的插件**必须有一个非单测的真实组合测试（REAL-composition）**：把测试用 `cordis.yml` 走 Loader 与应用/进程启动，只 mock 外部服务或非确定性输入，断言模型可见 / 持久 / 用户可见的输出；`ctx.plugin(...)` 手搭套件不算。
  - `packages/AGENTS.md::Product-visible plugins require a non-unit REAL-composition test (行 7)`
- 专门的支持组 `packages/test-support/`：`session-snapshot`（会话日志快照与协议适配器）、`agent-loop-testkit`（跑真实 AgentLoop 的共享前置服务）、`client-runtime`（jsdom slot 测试台）、`remote-mock`、`loader-smoke`（启动 Loader 组合应用并跑 fixture turn）、`llm-mock-server`（可编排的 OpenAI 兼容故障服务器）、`llm-replay`（回放录制流，无密钥）。该组是 support 层，获得产品契约与产品消费者后才迁出。
  - `packages/test-support/README.md::Summary (行 10-12)`、`::Packages (行 23-32)`
- 每个包可发布 `./invariant` 伙伴做运行时不变式检查，但**只有存在分歧观测时才发布**，否则在 README 说明理由；空的 companion 会 fail 校验。
  - `packages/AGENTS.md::Publish ./invariant only for diverging observations (行 19)`
- 快照层级：模型/协议/人可见的非平凡变更必须在同一 PR 补一个**无密钥录制会话场景**，产物在 `snapshots/session|sdk|acp|web/`。
  - `docs/testing.md::(行 55)`
- 并发执行纪律：spec 在 fork worker 里与其他 gate 进程并发跑，端口/路径/子进程必须自持并 teardown，「单独跑才过」的 spec 视为缺陷。
  - `packages/AGENTS.md::Specs run concurrently (行 18)`

### 4.4 成文的包规范

- `packages/README.md`：组划分、发布期望（多数组是产品 = 稳定 API；`e2b/` 是 POC、`experimental/` 未发布、`test-support`/`runtime-diagnostics`/`util` 是 support = 低兼容承诺）、依赖规则、README 契约。
  - `packages/README.md::Release expectations (行 84-86)`、`::Package README contracts (行 100-102)`
- `packages/AGENTS.md`：插件导出形态（service 包 default-export 服务类；function 插件 named-export `name`/`inject`/`Config`/`apply` 且无 default）、可选服务用 `ctx.get(name)`、initiator 私有链、一个异步操作一个生命周期控制器、模型面向契约用模型视角书写、边界施加于完整结果、tsconfig 布局、README 与 JSDoc 同 commit 更新。
  - `packages/AGENTS.md::(行 5-28)`
- `docs/cookbook/adding-a-package.md`：分组选择、package.json 不变式、读写包 README 的 Model Experience 格式、命名规则。
  - `docs/cookbook/adding-a-package.md::(行 23-46)`
- 每个包 README 必须覆盖 purpose / configuration / extension points / Model Experience，并带 `## Known Limitations and Deferred Work`（或有豁免条目）。
  - `packages/README.md::Package README contracts (行 100-102)`

### 4.5 不确定 / 未找到

- `runtime-diagnostics/` 与 `util/` 的具体内容未逐包展开（本任务范围内不需要）；只确认其 support 定位。
- `packages/*/AGENTS.md` 是否存在包级补充规范：只在 `packages/AGENTS.md` 与各处 `README.md` 见到；**未找到**包级 `AGENTS.md`（不确定是否有例外）。

---

## 5. 对 MiniAgent 的可借鉴点（按代价排序）

> 代价为「在 MiniAgent 现有代码上落地」的粗估：小 ≈ 单文件/单模块改造；中 ≈ 需要新增或重构一层；大 ≈ 牵动核心数据模型或多进程协作。

1. **【代价：小】把「工具被取消/超时」规范化成与成功同构的结果值，而不是抛异常。**
   dsh 用 `isError: true` + 稳定 code（`ABORTED` / `ABORTED_BEFORE_DISPATCH` / `TOOL_TIMEOUT`）替换结果，注册表**从不放弃已启动的 promise**。
   证据：`packages/core/tools/src/index.ts::toolAbortedResult (行 1909)`、`::toolAbortedBeforeDispatchResult (行 1923)`、`packages/guard/timeout-policy/src/index.ts::toolTimeoutResult (行 41)`。
   MiniAgent 可直接抄：给工具结果加 `error.code`，超时/取消走同一渲染路径。

2. **【代价：小】为「模型发出的每个 tool_call 必有 result」补两条兜底路径。**
   取消时为未启动的调用补合成 call+result；崩溃恢复时 `interruptedTurnClosers` 再补一次；`tool/result` 用 `sourceEventSeqs` 指回 `tool/call` 形成可校验的引用。
   证据：`packages/core/agent-loop/src/tool-calls.ts::appendSkippedToolCall (行 250)`、`packages/core/session/src/repair.ts::interruptedTurnClosers (行 29)`、`packages/core/agent-loop/src/tool-calls.ts::appendToolResult (行 269)`。
   这是「恢复后 provider 不拒绝 transcript」的最小充分条件。

3. **【代价：中】把持久化做成「事件日志 + 派生视图」而不是「存消息数组」，并给写盘加一个显式 flush 屏障。**
   日志是唯一真源，消息永远派生；`session/event` 走有界写后缓冲（200ms），`flush` 才是崩溃承诺；在「模型请求前 / 顶层工具派发前 / 每步前」三个语义点做 fail-closed 检查点。
   证据：`docs/subsystems/session.md (行 5)`、`packages/session/session-persistence-jsonl/src/storage.ts::LIVE_WRITE_BATCH_MAX_DELAY_MS (行 36)`、`packages/session/session-checkpoint-policy/src/index.ts::apply (行 40)`。
   代价中是因为它要求先决定「事件词汇表」；但一旦落地，持久化、恢复、UI 回放、压缩全部变成同一份数据的消费者。

4. **【代价：中】恢复只做「补边界」，绝不截断/改写已落盘的完整事件。**
   崩溃日志保留完整尾轮，只丢弃未 resolved 的物理残尾；修复在写所有权下进行，且必须重放整份日志重建视图。
   证据：`docs/subsystems/persistence.md::Crash recovery preserves an interrupted turn (行 108-110)`、`packages/core/agent-loop/src/index.ts::resumeWith (行 853-896)`。
   配套纪律：`open(id, 'write')` 先取独占所有权，再读、再修。

5. **【代价：中】把「只读观察者」和「写者」分开，派生缓存用版本号丢弃而不是迁移。**
   投影缓存存 `(key, ver, seq, val)`，`ver` 不匹配直接丢；观察者用 revision token 决定是否重读；只用内存 balance 不写回。
   证据：`packages/session/session-projection-cache/src/spec.ts::checkpointRow (行 26-31)`、`docs/subsystems/persistence.md::Lightweight source revisions (行 294-296)`。
   这条能把 MiniAgent 的「恢复很重」变成「恢复只是读 + 折」。

6. **【代价：大】把沙箱与进程终止做成两个独立 seam：沙箱只包装 argv（fail-closed），终止归 subprocess（managed range + TERM→KILL + 幂等 terminate/waitForExit）。**
   证据：`packages/sandbox/sandbox/src/index.ts::SandboxProvider (行 158)`、`::SANDBOX_UNAVAILABLE (行 124)`、`packages/subprocess/subprocess/src/types.ts::SubprocessHandle.terminate (行 180-191)`、`packages/subprocess/subprocess-local/src/spawn.ts::taskkillProcessTree (行 312)`。
   代价大是因为它要引入「managed range（连后代一起等）」这个概念；但它是唯一能真正保证「超时 = 进程真的没了」的结构。若不打算做，至少应把超时明确降级为「协作式取消」，不要对外宣称已终止。
