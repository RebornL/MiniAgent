"""app.tools —— 应用侧的工具定义与技能描述（工具契约的消费方）。

每个工具就是一个函数 + 一段描述：`TOOLS` 是唯一的来源，`ToolRuntime` 的注册与 Skill 的
tool 定义都由它派生。`final_output` 是交付结构化最终答案的终结工具。
"""
import json
from pathlib import Path
from typing import Callable

from capabilities.validation.definition import sanitize_output, sanitize_string


# ─── 1. 定义工具 ─────────────────────────────────
# 工具就是一个函数 + 一段描述（给 LLM 看的）
def search_web(query: str) -> str:
    """模拟搜索工具，实际可接 Google/Bing API"""
    # 真实场景这里调 API，这里用假数据演示
    fake_db = {
        "北京天气": "北京今天晴，25°C，微风",
        "上海天气": "上海今天小雨，22°C",
    }
    if "北京天气" in query:
        return fake_db.get("北京天气")
    return fake_db.get(query, f"未找到'{query}'的相关结果")

def calculate(expression: str) -> str:
    """安全的数学计算"""
    try:
        # 只允许数字和基本运算符，防止代码注入
        allowed = set("0123456789+-*/().% ")
        if not all(c in allowed for c in expression):
            return "错误：表达式包含不允许的字符"
        return str(eval(expression))
    except Exception as e:
        return f"计算错误: {e}"

def read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取文件失败: {e}"

def write_file(path: str, content: str) -> str:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入 {path}（{len(content)} 字符）"
    except Exception as e:
        return f"写入文件失败: {e}"

# ─── 2. 工具表（LLM 通过描述知道有什么工具可用）───
# 工具名 → (描述, JSON Schema, 函数)：ToolRuntime 的定义与 Skill 的 tool 定义同源派生
TOOLS: dict[str, tuple[str, dict, Callable[..., str]]] = {
    "search_web": (
        "搜索互联网获取信息，输入中文关键词",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
        search_web,
    ),
    "calculate": (
        "执行数学计算，输入数学表达式",
        {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "数学表达式，如 '3*15+2'"}},
            "required": ["expression"],
        },
        calculate,
    ),
    "read_file": (
        "读取文件内容",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径"}},
            "required": ["path"],
        },
        read_file,
    ),
    "write_file": (
        "写入文件内容",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["path", "content"],
        },
        write_file,
    ),
}


def _skill_tool(name: str) -> dict:
    """Skill 的 tool 定义（OpenAI 形状），与 ToolRuntime 里的描述同源。"""
    description, parameters, _ = TOOLS[name]
    return {"type": "function",
            "function": {"name": name, "description": description, "parameters": parameters}}


def _skill_tool_map(*names: str) -> dict[str, Callable[..., str]]:
    """技能的工具名 → 函数（legacy 工具是 `fn(**args)`）。"""
    return {name: TOOLS[name][2] for name in names}


# final_output：模型用来交付结构化最终答案的终结工具
OUTPUT_TOOL_NAMES = {"final_output"}


def make_final_output_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "final_output",
            "description": (
                "以结构化格式输出最终答案。调用此工具表示回答完成。"
                "result 字段放结构化数据，summary 字段放给用户看的总结。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "object",
                        "description": "最终答案的 JSON 结构化数据",
                    },
                    "summary": {
                        "type": "string",
                        "description": "给用户看的一句话总结",
                    },
                },
                "required": ["result"],
            },
        },
    }

def final_output_handler(result: dict, summary: str = "") -> str:
    sanitize_output(result)       # 递归检测字符串值
    if summary:
        sanitize_string(summary)  # 检测 summary
    return json.dumps({"result": result, "summary": summary}, ensure_ascii=False)
