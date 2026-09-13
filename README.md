# MiniAgent

一个轻量级 Python Agent 框架：可插拔的 harness 骨架（miniharness）+ 工具调用、Skill 系统、会话持久化、上下文压缩、执行追踪。

**改策略不改循环**：重试、超时、压缩、输出校验、终结、持久化、追踪都是挂在事件 seam 上的插件，循环体里没有任何策略分支。架构与扩展方式见 [`docs/miniharness.md`](docs/miniharness.md)。

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
python MiniAgent.py
```

进入交互式对话循环。

| 命令 | 作用 |
|------|------|
| `/exit` | 退出 |
| `/help` | 显示帮助 |
| `/history` | 列出历史会话 |
| `/switch <id>` | 切换到指定会话 |
| `/new` | 新建会话（放弃当前） |

## 项目结构

```
MiniAgent/
├── MiniAgent.py            # 应用装配：工具、技能、build_harness、chat_loop
├── miniharness.py          # harness 骨架：5 原语 + ToolRuntime + 零策略 Loop
├── miniharness_plugins.py  # 策略插件：压缩/重试/超时/校验/终结/持久化/追踪/技能
├── miniharness_deepseek.py # 真实 LLM provider（DeepSeek / OpenAI 兼容）
├── SkillManager.py         # Skill 系统
├── AgentTrace.py           # 执行追踪
├── Compaction.py           # 上下文压缩
├── Persistence.py          # 会话持久化
├── RetryFunc.py            # 重试机制
├── CallFunc.py             # 工具调用超时
├── Structure.py            # 结构化输出与 schema 校验
├── requirements.txt        # 依赖
└── config.json             # 配置文件（需自行创建）
```

## 测试

```bash
python -m pytest -q
```

测试集中在三个 seam 上：`Loop.turn`（集成）、`ToolRuntime.run`（工具管线契约）、`Session.derive_messages`（纯函数投影）。详见 [`docs/miniharness.md`](docs/miniharness.md) §6。

## License

MIT
