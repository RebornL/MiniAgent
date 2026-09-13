## Agent skills

### Issue tracker

Issues live in GitHub Issues for `RebornL/MiniAgent`, operated via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles map one-to-one to `needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root plus `docs/adr/` for ADRs. See `docs/agents/domain.md`.

### 工程落位规范

新代码放哪个包、怎么命名、依赖方向怎么走、测试放哪，一律照 `docs/packaging.md`（权威规范）。
各族的包地图见 `miniharness/README.md`、`capabilities/README.md`、`providers/README.md`、
`app/README.md`、`tests/README.md`。

## 工作方式

以下约定不靠对话记忆——上下文压缩后它们必须仍然可用。

### 每张票的流程

子 agent 实现（**不 commit**）→ 主 session **独立核验** → 两轴 `/code-review`（Standards + Spec）
→ 修 → 提交 → 关闭该 issue 并解阻下游。

### 核验纪律：不采信自述

任何交付都要自己跑、自己复现。**默认必验四类，缺一不可**：

1. **新路径**——正常流程能跑通；
2. **旧数据 / 迁移**——仓库里真实的旧会话目录（`agent_sessions/`）能否照常恢复；
3. **边界态**——例如「组长已自行退出、后代仍在跑」「进程刚好在写完 header 之前崩」；
4. **失败注入**——故意破坏前置条件，确认断言会红（**非空洞性**）。

前三类不能只靠子 agent 的自述或合成数据；第 4 类是判断「这条测试到底有没有用」的唯一手段。

5. **核验要跑在全新进程里**——长活的内核 / Python 进程会持有**改动前的模块**（本次实测：`eval`
   内核里 `LOG_VERSION` 仍是 1、`read_log` 的代码对象还指在旧行号上，而文件早已升到 2），据它
   得出的「修复无效」是假警报，据它得出的「修复有效」同样是假证据。探针一律用新起的 `python`
   进程或临时脚本；断言红之前，先确认读到的代码对象版本与文件一致。

### 数据与文档的边界

- **真实会话数据不入库**：`agent_sessions/` 在 `.gitignore` 里，而仓库是公开的。测试 fixture 用
  **合成内容**（结构可逐字照搬真实落盘格式），并在注释里写明它是合成的。
- **历史文档逐字保留**：`docs/review-guide-v1.md`、`miniharness-spec.md`、`miniharness-v2-spec.md`、
  `MiniAgent-Harness-Design.md`、`Codex-Harness-*.md`、`DeepSeek-Harness-*.md` 记录的是当时的快照，
  **不得回改**；过时的标识符只在**现行**文件里清理。

### 票务

票与阻塞边都在 GitHub Issues。**frontier 判定**用 `issue_dependencies_summary.blocked_by`
（只计**未关闭**的阻塞者），不是 `dependencies/blocked_by` 列表端点（它返回全部、不论状态）。