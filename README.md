# MiniAgent

一个轻量级 Python Agent 框架，支持工具调用、会话管理、Skill 系统和上下文压缩。

## 环境要求

- Python 3.10+
- 依赖安装：

```bash
pip install openai
```

## 快速开始

### 1. 创建配置文件

在项目根目录创建 `config.json`（该文件已加入 `.gitignore`，不会被提交到 Git）：

```json
{
    "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
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

进入交互式对话循环，输入 `exit` 或 `quit` 退出。

## 项目结构

```
MiniAgent/
├── MiniAgent.py        # 主程序（Agent 核心循环）
├── SkillManager.py     # Skill 系统
├── AgentTrace.py       # 执行追踪
├── Compaction.py       # 上下文压缩
├── Persistence.py      # 会话持久化
├── RetryFunc.py        # LLM 重试机制
├── CallFunc.py         # 工具调用超时控制
└── config.json         # 配置文件（需自行创建）
```

## License

MIT
