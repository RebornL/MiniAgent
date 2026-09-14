# MiniAgent

一个轻量级 Python Agent 框架：可插拔的 harness 骨架（miniharness）+ 工具调用、Skill 系统、会话持久化、上下文压缩、执行追踪、命令沙箱与审批、进程级终止与取消。

**改策略不改循环**：重试、超时、压缩、输出校验、终结、持久化、追踪都是挂在事件 seam 上的插件，循环体里没有任何策略分支。架构与扩展方式见 [`docs/miniharness.md`](docs/miniharness.md)。

工程按能力族包化：能力落成**契约（Definition）/ 实现（Provider）/ 消费方（Consumer）**三个具名角色、分属不同包，契约与高频变动的实现按变化速率分开。各族包地图见 `miniharness/`、`capabilities/`、`providers/`、`app/`、`tests/` 下的 `README.md`；新代码落位、命名、依赖方向与测试放置的规范见 [`docs/packaging.md`](docs/packaging.md)。

## 环境要求

- Python 3.10+
- 安装依赖：

```bash
pip install -r requirements.txt
```

## 快速开始

### 1. 创建配置文件

在项目根目录创建 `config.json`（该文件已加入 `.gitignore`，不会被提交到 Git）：

```json
{
    "base_url": "https://api.deepseek.com",
    "api_key": "你的API密钥"
}
```

| 字段 | 说明 |
|------|------|
| `base_url` | LLM API 地址（兼容 OpenAI 接口即可） |
| `api_key` | API 密钥 |

### 2. 运行

```bash
python -m app
```

进入交互式对话循环（等价于包化之前的 `python MiniAgent.py`）。

| 命令 | 作用 |
|------|------|
| `/exit` | 退出 |
| `/help` | 显示帮助 |
| `/history` | 列出历史会话 |
| `/switch <id>` | 切换到指定会话 |
| `/new` | 新建会话（放弃当前） |

## 项目结构

五个族，每个族一份权威包地图（`README.md`）：

```
MiniAgent/
├── miniharness/            # 骨架：低频契约与运行时
│   ├── README.md           #   本族包地图
│   ├── core/               #   Context（服务注册表 + 事件总线）、Plugin
│   ├── session/            #   Session：事件日志 + 投影/逆投影（+ test_projection.py）
│   ├── tools/
│   │   ├── contract/       #   ToolDefinition（工具契约）
│   │   └── runtime/        #   ToolRuntime（执行流水线；+ test_pipeline.py）
│   ├── llm/contract/       #   LLM（LLM 契约）
│   ├── process/contract/   #   ManagedRange / ProcessSeam（受管范围契约）
│   └── loop/               #   Loop：驱动 + 派发事件（零策略）
├── capabilities/           # 能力族：契约 / 实现 / 消费方三分
│   ├── README.md           #   包地图：每个能力的三个角色落在哪个包
│   ├── compaction/         #   definition/ + provider/
│   ├── persistence/        #   definition/ + provider/
│   ├── retry/              #   definition/ + provider/（+ test_retry.py）
│   ├── timeout/            #   provider/（无稳定契约符号，整体并入——T12）（+ test_timeout.py）
│   ├── validation/         #   provider/（无稳定契约符号，整体并入——T12）
│   ├── tracing/            #   provider/（无稳定契约符号，整体并入——T12）
│   ├── skills/             #   definition/ + provider/（+ test_skills.py）+ consumer/
│   ├── permission/         #   provider/
│   └── final_output/       #   provider/
├── providers/              # 后端族：骨架 seam 的实现
│   ├── README.md           #   本族包地图
│   ├── deepseek/           #   真实 LLM provider（DeepSeek / OpenAI 兼容）
│   ├── mock/               #   MockLLM（离线 provider）
│   └── process/            #   受管范围的平台后端（POSIX 信号组 / Windows Job Object）
├── app/                    # 装配族：装配 + 入口 + CLI
│   ├── README.md           #   本族包地图
│   ├── config.py           #   config.json 的惰性读取
│   ├── tools.py            #   应用侧工具定义与技能描述
│   ├── assembly.py         #   build_harness() / open_harness() / resume_session()
│   ├── cli.py              #   chat_loop()
│   ├── skeleton_demo.py    #   骨架 smoke run
│   ├── stack_demo.py       #   全栈 smoke run
│   ├── deepseek_demo.py    #   真实联调 demo（联网）
│   └── __main__.py         #   python -m app
├── tests/                  # 测试族：跨包集成集中一处
│   ├── README.md           #   测试放置说明
│   ├── support.py          #   集成测试共享 helper
│   ├── fixtures/legacy-v0-session/  #  v0 落盘形态样本（合成内容）
│   ├── test_turn.py        #   S2：turn 边界
│   ├── test_capabilities.py #  能力协作与契约等价
│   ├── test_app.py         #   app 装配与旧会话迁移
│   ├── test_shell.py       #   run_command（真实子进程）
│   ├── test_sandbox.py     #   沙箱 seam
│   ├── test_timeout.py     #   超时/取消 → 进程终止
│   ├── test_cli_approval.py #  CLI 审批者
│   └── test_cli_cancel.py  #   取消源
├── CONTEXT.md              # 词表：投影 / 受管范围 / 中止结局 等术语
├── docs/
│   ├── packaging.md        # 落位 / 命名 / 依赖方向 / 测试放置规范
│   ├── miniharness.md      # 骨架架构与扩展方式
│   ├── adr/                # 架构决策记录（0001：事件日志是唯一权威源）
│   ├── agents/             # agent 工作方式（issue tracker / triage 标签 / domain 文档）
│   ├── review-guide-v1.md  # v1 迁移总结（历史快照，记录 v1 平铺布局）
│   └── review-guide-v2.md  # v2 总结、审视路径与未闭合台账
├── requirements.txt
└── config.json             # 配置文件（需自行创建）
```

## 测试

```bash
python -m pytest -q
```

测试集中在四类 seam 上：`Loop.turn`（集成）、`ToolRuntime.run`（工具管线契约）、`Session.derive_messages`（纯函数投影）、**进程边界**（真实子进程 + 系统侧进程确认）。详见 [`docs/miniharness.md`](docs/miniharness.md) §6。

测试与实现同层但分离：包内测试与实现放同一个包（`miniharness/session/test_projection.py`、`capabilities/*/provider/test_*.py`），跨包集成集中在 [`tests/`](tests/README.md)。放置规范见 [`docs/packaging.md`](docs/packaging.md) §6。

## License

MIT
