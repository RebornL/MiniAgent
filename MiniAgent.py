"""
一个最精简的 Agent 核心实现
依赖: pip install openai
"""
import json
import os
import time
import uuid

from openai import OpenAI

from AgentTrace import Span, AgentTracer
from Compaction import ContextManager, to_dict
from Persistence import PersistenceManager, Store

client = OpenAI(
    base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
    api_key="ark-83528937-561f-455e-a54f-2c96067a1830-7abcc",
)  # 默认读 OPENAI_API_KEY 环境变量

# ─── 1. 定义工具 ─────────────────────────────────
# 工具就是一个函数 + 一段描述（给 LLM 看的）
def search_web(query: str) -> str:
    """模拟搜索工具，实际可接 Google/Bing API"""
    # 真实场景这里调 API，这里用假数据演示
    fake_db = {
        "北京天气": "北京今天晴，25°C，微风",
        "上海天气": "上海今天小雨，22°C",
    }
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

# ─── 2. 工具注册表（LLM 通过描述知道有什么工具可用）───
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "搜索互联网获取信息，输入中文关键词",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "执行数学计算，输入数学表达式",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式，如 '3*15+2'"}
                },
                "required": ["expression"],
            },
        },
    },
]

# 工具名 → 实际函数的映射
TOOL_MAP = {
    "search_web": search_web,
    "calculate": calculate,
}

# ─── 3. 核心循环（Agent 的"大脑"）─────────────────
SYSTEM_PROMPT = """你是一个有用的助手。你可以使用工具来获取信息或执行计算。
遇到不确定的事情时，请调用工具而不是猜测。"""

def run_agent(user_input: str, max_steps: int = 5) -> str:
    """
    Agent 主循环：思考 → 行动 → 观察 → 再思考 → ... → 回答
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]

    for step in range(max_steps):
        print(f"\n{'='*50}")
        print(f"🔄 第 {step+1} 轮思考...")

        # 调用 LLM，告诉它有哪些工具可用
        response = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",  # LLM 自己决定要不要调工具
        )

        msg = response.choices[0].message

        # 情况 A：LLM 认为不需要调工具，直接输出最终答案
        if not msg.tool_calls:
            print(f"✅ Agent 给出最终回答")
            return msg.content

        # 情况 B：LLM 想调工具
        # 先把 LLM 的回复（含 tool_call）加入对话历史
        messages.append(msg)

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            print(f"🔧 调用工具: {name}({args})")

            # 执行工具
            func = TOOL_MAP[name]
            result = func(**args)

            print(f"📊 工具返回: {result}")

            # 把工具执行结果加入对话历史
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

        # 循环回去，让 LLM 看到工具结果后继续思考

    return "⚠️ 达到最大步数限制，Agent 未能在限定步数内完成任务"


def run_agent_with_trace(
    user_input: str,
    *,
    tracer: AgentTracer,
    client: OpenAI,
    ctx: ContextManager,
    pm: PersistenceManager,
    session_id: str | None = None,
    max_steps: int = 5,
) -> str:
    """
    带 可观测 + 压缩 + 持久化 的 Agent 循环。
    在原来 run_agent_with_trace 上直接加持久化能力。
    """

    # ── 创建或恢复会话 ─────────────────────────────
    if session_id:
        state = pm.load_session(session_id)
        messages = state["messages"]
        ctx.restore(state["summary"])
        print(f"📂 恢复会话 {session_id}，已有 {len(messages)} 条消息")
    else:
        session_id = pm.new_session_id()
        messages = []

    # 追加用户输入 + 确保 system prompt 在第一位
    messages.append({"role": "user", "content": user_input})
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

    # ── Trace 开始 ─────────────────────────────────
    run_id = f"run_{os.urandom(6).hex()}"
    run = tracer.start_run(run_id, user_input)

    # ── 主循环 ─────────────────────────────────────
    for step in range(max_steps):
        print(ctx.stats(messages))

        t0 = time.time()
        try:
            response = client.chat.completions.create(
                model="deepseek-v4-pro",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
        except Exception as e:
            llm_err = Span(
                span_id=f"llm_{os.urandom(4).hex()}",
                type="llm_call",
                start_time=t0,
                end_time=time.time(),
                error=str(e),
            )
            run.children.append(llm_err)
            raise

        llm_span = tracer.log_llm_call(messages, response, time.time() - t0)
        run.children.append(llm_span)

        msg = response.choices[0].message

        # ✅ 新增：统一转 dict 再 append
        messages.append(to_dict(msg))

        # 无需工具 → 输出答案
        if not msg.tool_calls:
            run.end_time = time.time()
            # ✅ 新增：最终保存
            pm.save_session(
                session_id, messages, ctx.summary,
                tracer.to_dicts(), user_input,
            )
            print(tracer.summary(run))
            print(f"💾 会话已保存: {session_id}")
            return msg.content or ""

        # 执行工具
        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            t0 = time.time()
            try:
                result = TOOL_MAP[name](**args)
            except Exception as e:
                result = f"工具执行错误: {e}"

            tool_span = tracer.log_tool_call(name, args, result, time.time() - t0)
            run.children.append(tool_span)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

        # ✅ 新增：压缩检查
        messages = ctx.maybe_compact(messages, client)

        # ✅ 新增：每轮自动保存（防崩溃）
        pm.save_messages(session_id, messages)

    # 达到最大步数
    run.end_time = time.time()
    pm.save_session(session_id, messages, ctx.summary, tracer.to_dicts(), user_input)
    print(tracer.summary(run))
    print(f"💾 会话已保存: {session_id}")
    return "⚠️ 达到最大步数限制"

def resume_session(
    session_id: str,
    new_input: str,
    *,
    client: OpenAI,
    tools: list[dict],
    tool_map: dict[str, callable],
    system_prompt: str,
    store_dir: str = "./agent_sessions",
    max_steps: int = 5,
) -> str:
    """恢复历史会话并继续对话"""
    return run_agent(
        new_input,
        client=client,
        tools=tools,
        tool_map=tool_map,
        system_prompt=system_prompt,
        session_id=session_id,
        max_steps=max_steps,
        persistence=PersistenceManager(Store(store_dir)),
    )

# ─── 4. 跑起来 ────────────────────────────────────
if __name__ == "__main__":
    # client = OpenAI()
    tracer = AgentTracer()
    ctx = ContextManager()
    pm = PersistenceManager()

    # 新建会话
    # answer = run_agent_with_trace(
    #     "北京天气怎么样？",
    #     tracer=tracer,
    #     client=client,
    #     ctx=ctx,
    #     pm=pm,
    # )
    # print(f"\n{'=' * 50}")
    # print(f"🎯 最终结果: {answer}")

    # 恢复继续
    answer = run_agent_with_trace(
        "那上海呢？",
        tracer=tracer,
        client=client,
        ctx=ctx,
        pm=pm,
        session_id="20260701_143022_a1b2c3d4",
    )
    print(f"\n{'=' * 50}")
    print(f"🎯 最终结果: {answer}")

    # agent_tracer = AgentTracer()
    # result = run_agent_with_trace("1、北京天气怎么样？顺便帮我算一下 156 * 23; 2、上海天气怎么样？顺便帮我算一下 1126 * 523", agent_tracer)
    # print(f"\n{'='*50}")
    # print(f"🎯 最终结果: {result}")