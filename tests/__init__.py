"""tests —— 测试族：跨包集成测试集中于此；包内测试与实现同层放在各自包里。

- 跨包集成（驱动 `Loop.turn`、装配整个 app）：`tests/test_turn.py` / `tests/test_capabilities.py` / `tests/test_app.py`；
- 包内测试：`miniharness/session/test_projection.py`、`miniharness/tools/runtime/test_pipeline.py`、
  `capabilities/*/provider/test_*.py`；
- 共享装配 helper：`tests/support.py`（只服务集成测试）。

测试放置规范见 `tests/README.md` 与 `docs/packaging.md`。
"""