# app —— 装配族（把骨架、能力与后端装成可运行的应用）

装配族是依赖方向的最上层：它认识所有族，其它族都不认识它。

本文件是该族的**权威包地图**。落位、命名、依赖方向与测试放置的规范见
[`docs/packaging.md`](../docs/packaging.md)。

## 入口

```bash
python -m app                 # 交互式对话（需要根目录 config.json）
```

`app/__main__.py` 就是这条命令：读凭据 → `chat_loop()`。等价于包化之前的 `python MiniAgent.py`。

## 本族的模块

| 模块 | 职责 |
| --- | --- |
| `app.config` | `config.json` 的惰性读取（导入期不碰配置，全新 clone 上 `import app` 必须成功） |
| `app.tools` | 应用侧的工具定义与技能描述：`TOOLS` 是唯一来源，`make_final_output_tool` / `final_output_handler` 也在其中（工具契约的消费方） |
| `app.assembly` | `build_harness()` / `resume_session()` / `register_skills()`：把 Session + 工具 + provider + 策略插件 + 日志消费者装起来 |
| `app.cli` | `chat_loop()`：交互式多轮对话，每轮只调 `loop.turn(user_input)`（含 `/exit` `/help` `/history` `/switch` `/new`） |
| `app.__main__` | 入口：`python -m app` |
| `app.skeleton_demo` | 骨架 smoke run：`python -m app.skeleton_demo`（离线，依赖驱动激活顺序 + deny 分支） |
| `app.stack_demo` | 全栈 smoke run：`python -m app.stack_demo`（离线，压缩 / 重试 / 超时 / 持久化 / 追踪 / 技能） |
| `app.deepseek_demo` | 真实联调 demo：`python -m app.deepseek_demo`（联网，需根目录 config.json；真实 provider + 终结工具） |

装配侧的两条既有约定（包化前就是这样，未改）：

1. `build_harness()` **不装 `PermissionPlugin`** —— legacy 没有审批概念，装了就是凭空加行为；
2. 工具不预注册：只随技能装载经 `SkillRegistry` 可逆注册（`unload_skill` 即撤销）。

## 本族的测试

app 的测试天然是跨包的（装配所有族），因此**集中在**
[`tests/test_app.py`](../tests/test_app.py)，不在本族内单放测试文件。
