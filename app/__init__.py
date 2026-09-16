"""app —— 装配族：把骨架、能力与后端装成一个可运行的应用。

- `config`：后端知识的唯一 locus（`DEFAULT_MODEL` / `DEFAULT_STORE_DIR` / `_default_llm`）
  与 config.json 的惰性读取（导入期不碰配置）；
- `tools`：应用侧的工具定义与技能描述（工具契约的消费方）；
- `assembly`：`build_harness()` / `resume_session()`；
- `cli`：交互式 `chat_loop()`；
- `__main__`：入口 `python -m app`；
- `skeleton_demo` / `stack_demo`：两个离线 smoke run。

包地图见 `app/README.md`。
"""