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