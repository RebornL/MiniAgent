"""
一个最精简的 Agent 核心实现
依赖: pip install openai
"""
import json
import os
import time
import uuid
from pathlib import Path

from openai import OpenAI

from AgentTrace import Span, AgentTracer
from CallFunc import call_with_timeout
from Compaction import ContextManager, to_dict
from Persistence import PersistenceManager, Store
from RetryFunc import with_retry
from SkillManager import SkillManager, Skill



def _load_config() -> dict:
    """从 config.json 加载配置，文件不存在则报错提示"""
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件缺失: {config_path}\n"
            "请参考 README.md 创建 config.json，包含 base_url 和 api_key"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


_config = _load_config()
client = OpenAI(
    base_url=_config["base_url"],
    api_key=_config["api_key"],
)

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


def build_system_prompt(base_prompt: str, skills: SkillManager) -> str:
    """组装 system prompt = base + 激活 skill 的领域知识"""
    parts = [base_prompt]
    skill_prompt = skills.get_active_prompt()
    if skill_prompt:
        parts.append(f"\n\n--- 当前激活的技能 ---\n{skill_prompt}")
    return "\n".join(parts)

# ═══════════════════════════════════════════════════════════════
# 辅助：流式 LLM 调用
# ═══════════════════════════════════════════════════════════════
def stream_llm_call(
    client: OpenAI,
    model: str,
    messages: list[dict],
    tools: list[dict],
) -> tuple[str | None, list[dict] | None]:
    """流式调用 LLM，边收边打印，tool_call 实时展示"""

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        stream=True,
    )

    content_chunks: list[str] = []
    tool_call_chunks: dict[int, dict] = {}  # index → {id, name, arguments}
    shown_tool_names: set[int] = set()       # 已打印过的 tool 名

    print("🧠 ", end="", flush=True)

    for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue

        # ── 文本内容 ──
        if delta.content:
            print(delta.content, end="", flush=True)
            content_chunks.append(delta.content)

        # ── tool_calls：实时显示 ──
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tool_call_chunks:
                    tool_call_chunks[idx] = {
                        "id": tc_delta.id or "",
                        "name": "",
                        "arguments": "",
                    }
                if tc_delta.id:
                    tool_call_chunks[idx]["id"] = tc_delta.id
                if tc_delta.function.name:
                    tool_call_chunks[idx]["name"] = tc_delta.function.name
                    # ✅ 新增：一旦拿到 name 就立刻打印
                    if idx not in shown_tool_names:
                        print(f"\n  🔧 调用 {tc_delta.function.name}", end="", flush=True)
                        shown_tool_names.add(idx)
                if tc_delta.function.arguments:
                    tool_call_chunks[idx]["arguments"] += tc_delta.function.arguments

    # 如果前面没打印过 content 且没 tool_call，补换行
    if not content_chunks and not tool_call_chunks:
        print()

    # ── 组装结果 ──
    if tool_call_chunks:
        tc_list = []
        for idx in sorted(tool_call_chunks.keys()):
            tc = tool_call_chunks[idx]
            tc_list.append({
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                },
            })
            # 打印参数（紧跟 name 后面）
            args_preview = tc["arguments"][:60] + ("..." if len(tc["arguments"]) > 60 else "")
            if args_preview:
                print(f"({args_preview})")
        return None, tc_list
    else:
        print()  # 纯文本结束换行
        return "".join(content_chunks), None

def run_agent_with_trace(
    user_input: str,
    *,
    tracer: AgentTracer,
    client: OpenAI,
    ctx: ContextManager,
    pm: PersistenceManager,
    skills: SkillManager,
    base_system_prompt: str,
    session_id: str | None = None,
    max_steps: int = 5,
    model: str = "deepseek-v4-pro",
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
        skills.reset()
        for name in state.get("active_skills", []):
            # 加载之前对话的skill
            skills.load(name)
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
        # ← 新增：每次 LLM 调用前动态拼装 tools
        active_tools = skills.get_active_tools()
        active_tool_map = skills.get_active_tool_map()

        # ← 新增：动态拼装 system prompt
        system_prompt = build_system_prompt(base_system_prompt, skills)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})

        print(ctx.stats(messages))
        # 修改为流式输出
        t0 = time.time()
        try:
            # 补充重试机制
            def _call():
                return stream_llm_call(client, model, messages, active_tools)
            content, tool_calls = with_retry(_call, label="LLM")
        except Exception as e:
            llm_err = Span(
                span_id=f"llm_{os.urandom(4).hex()}",
                type="llm_call", start_time=t0, end_time=time.time(), error=str(e),
            )
            run.children.append(llm_err)
            raise

        # 记录 trace（用简化版，因为流式没有完整 response 对象）
        # 手动构造 Span 时，把 tool_calls 拍平
        flat_tool_calls = [
            {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}
            for tc in (tool_calls or [])
        ]
        input_tokens = ctx.count_tokens(messages)
        output_text = content or ""
        if tool_calls:
            output_text += json.dumps(tool_calls, ensure_ascii=False)
        output_tokens = len(ctx.encoder.encode(output_text))
        estimated_tokens = input_tokens + output_tokens
        llm_span = Span(
            span_id=f"llm_{os.urandom(4).hex()}",
            type="llm_call",
            start_time=t0,
            end_time=time.time(),
            input={"message_count": len(messages)},
            output={"content": content, "tool_calls": flat_tool_calls},  # ← 拍平后存
            tokens_used=estimated_tokens,
        )
        run.children.append(llm_span)

        # 无 tool_calls → 纯文本答案
        if tool_calls is None:
            # 保存每一轮问答的结论，避免重复回答
            messages.append({"role": "assistant", "content": content})
            run.end_time = time.time()
            pm.save_session(
                session_id, messages, ctx.summary,
                tracer.to_dicts(), list(skills._active), user_input,
            )
            print(tracer.summary(run))
            print(f"💾 会话已保存: {session_id}")
            return content or ""

        # 有 tool_calls → 和之前一样，但不再 append assistant 消息（stream 里没有标准 msg 对象）
        # 手动构造 assistant 消息
        assistant_msg = {
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        }
        messages.append(assistant_msg)

        for tc in tool_calls:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"])

            t0 = time.time()
            # try:
            #     result = active_tool_map[name](**args)  # ← 注意这里用 active_tool_map
            # except Exception as e:
            #     result = f"工具执行错误: {e}"
            # 包一层超时
            result = call_with_timeout(
                active_tool_map[name],
                kwargs=args,
                timeout=30,  # 可配置
            )

            tool_span = tracer.log_tool_call(name, args, str(result), time.time() - t0)
            run.children.append(tool_span)

            print(f"  🔧 {name}({args}) → {str(result)[:80]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": str(result),
            })

        # t0 = time.time()
        # try:
        #     response = client.chat.completions.create(
        #         model=model,
        #         messages=messages,
        #         tools=active_tools,
        #         tool_choice="auto",
        #     )
        # except Exception as e:
        #     llm_err = Span(
        #         span_id=f"llm_{os.urandom(4).hex()}",
        #         type="llm_call",
        #         start_time=t0,
        #         end_time=time.time(),
        #         error=str(e),
        #     )
        #     run.children.append(llm_err)
        #     raise
        #
        # llm_span = tracer.log_llm_call(messages, response, time.time() - t0)
        # run.children.append(llm_span)
        #
        # msg = response.choices[0].message
        #
        # # ✅ 新增：统一转 dict 再 append
        # messages.append(to_dict(msg))
        #
        # # 无需工具 → 输出答案
        # if not msg.tool_calls:
        #     run.end_time = time.time()
        #     # ✅ 新增：最终保存
        #     pm.save_session(
        #         session_id, messages, ctx.summary,
        #         tracer.to_dicts(), list(skills._active), user_input,
        #     )
        #     print(tracer.summary(run))
        #     print(f"💾 会话已保存: {session_id}")
        #     return msg.content or ""
        #
        # # 执行工具
        # for tool_call in msg.tool_calls:
        #     name = tool_call.function.name
        #     args = json.loads(tool_call.function.arguments)
        #
        #     t0 = time.time()
        #     try:
        #         result = active_tool_map[name](**args)
        #     except Exception as e:
        #         result = f"工具执行错误: {e}"
        #
        #     tool_span = tracer.log_tool_call(name, args, result, time.time() - t0)
        #     run.children.append(tool_span)
        #
        #     messages.append({
        #         "role": "tool",
        #         "tool_call_id": tool_call.id,
        #         "content": result,
        #     })

        # ✅ 新增：压缩检查
        messages = ctx.maybe_compact(messages, client)

        # ✅ 新增：每轮自动保存（防崩溃）
        pm.save_messages(session_id, messages)

    # 达到最大步数
    run.end_time = time.time()
    pm.save_session(session_id, messages, ctx.summary, tracer.to_dicts(), list(skills._active), user_input)
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

def chat_loop(
    client: OpenAI,
    skills: SkillManager,
    base_system_prompt: str,
    model: str = "deepseek-v4-flash",
    session_id: str | None = None,
    store_dir: str = "./agent_sessions",
):
    """
    交互式多轮对话。

    用法:
      >>> chat_loop(client, skills, "你是助手...")
      You: 北京天气怎么样？
      Agent: 北京今天晴，25°C
      You: 那上海呢？
      Agent: 上海今天小雨，22°C
      You: /exit
    """
    tracer = AgentTracer()
    ctx = ContextManager()
    pm = PersistenceManager(Store(store_dir))

    # 如果没有传入 session_id，新建一个
    if session_id is None:
        session_id = pm.new_session_id()
        print(f"🆕 新会话: {session_id}")
    else:
        print(f"📂 恢复会话: {session_id}")

    print("输入 /exit 退出，/history 查看历史会话，/switch <id> 切换会话\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见")
            break

        if not user_input:
            continue

        # ── /exit ──
        if user_input == "/exit":
            print("👋 再见")
            break

        # ── /help ──
        elif user_input == "/help":
            print("""
      命令:
        /exit              退出
        /history           查看所有历史会话
        /switch <id>       切换到指定会话
        /new               新建会话（放弃当前）
        /help              显示此帮助
      直接输入文字即可对话。
            """.strip())
            continue

        # ── /new ──
        elif user_input == "/new":
            session_id = pm.new_session_id()
            tracer = AgentTracer()
            ctx = ContextManager()
            skills.reset()
            print(f"🆕 新会话: {session_id}")
            continue

        # ── /history ──
        elif user_input == "/history":
            sessions = pm.list_sessions()
            if not sessions:
                print("📭 暂无历史会话")
                continue
            for s in sessions:
                marker = " ← 当前" if s["id"] == session_id else ""
                print(f"  {s['id']} | {s['message_count']}条 | {s['updated']} | {s['last_message']}{marker}")
            continue

        # ── /switch ──
        elif user_input.startswith("/switch"):
            parts = user_input.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                print("⚠️ 用法: /switch <session_id>")
                print("   先用 /history 查看可用会话，再切换")
                continue

            new_id = parts[1].strip()

            if new_id == session_id:
                print(f"⚠️ 已经是当前会话: {session_id}")
                continue

            state = pm.load_session(new_id)
            if not state["messages"]:
                print(f"❌ 会话 {new_id} 不存在或为空")
                continue

            session_id = new_id
            tracer = AgentTracer()
            ctx = ContextManager()
            ctx.restore(state["summary"])
            skills.reset()
            print(f"✅ 已切换到 {session_id}（{len(state['messages'])} 条消息）")

        # ── 正常对话 ──
        else:
            answer = run_agent_with_trace(
                user_input,
                tracer=tracer,
                client=client,
                ctx=ctx,
                pm=pm,
                skills=skills,
                base_system_prompt=base_system_prompt,
                session_id=session_id,
                model=model,
            )
            print(f"Agent: {answer}\n")

# ─── 4. 跑起来 ────────────────────────────────────
if __name__ == "__main__":
    skills = SkillManager()

    skills.register(Skill(
        name="web-search",
        description="互联网搜索能力",
        tools=[{
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "搜索互联网获取信息，输入中文关键词",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                    "required": ["query"],
                },
            },
        }],
        tool_map={"search_web": search_web},
        system_prompt="你拥有搜索能力。遇到不确定的事实性问题，请先搜索再回答，不要猜测。",
    ))

    skills.register(Skill(
        name="calculator",
        description="数学计算能力",
        tools=[{
            "type": "function",
            "function": {
                "name": "calculate",
                "description": "执行数学计算，输入数学表达式",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string", "description": "数学表达式"}},
                    "required": ["expression"],
                },
            },
        }],
        tool_map={"calculate": calculate},
        system_prompt="你拥有计算能力。遇到数学计算请调用 calculate 工具，不要心算。",
    ))

    # Skill 3: 文件操作
    skills.register(Skill(
        name="file-ops",
        description="文件读写能力",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "读取文件内容",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "文件路径"}},
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "写入文件内容",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "文件路径"},
                            "content": {"type": "string", "description": "文件内容"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
        ],
        tool_map={"read_file": read_file, "write_file": write_file},
        system_prompt="你拥有文件读写能力。操作文件前请确认路径正确。",
    ))

    base_prompt = (
        "你是一个有用的助手。"
        "你可以使用 load_skill 加载需要的技能模块，用 unload_skill 释放不再需要的模块。"
        "遇到不确定的事实时，请先加载对应技能再操作，不要猜测。"
    )

    chat_loop(client, skills, base_prompt, "deepseek-v4-pro")
    # tracer = AgentTracer()
    # ctx = ContextManager()
    # pm = PersistenceManager()
    # answer = run_agent_with_trace(
    #     "北京天气怎么样？顺便帮我算 156*23",
    #     tracer=tracer,
    #     client=client,
    #     ctx=ctx,
    #     pm=pm,
    #     skills=skills,
    #     base_system_prompt=base_prompt,
    # )
    # print(f"\n🎯 最终答案: {answer}")

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
    # answer = run_agent_with_trace(
    #     "那上海呢？",
    #     tracer=tracer,
    #     client=client,
    #     ctx=ctx,
    #     pm=pm,
    #     session_id="20260701_143022_a1b2c3d4",
    # )
    # print(f"\n{'=' * 50}")
    # print(f"🎯 最终结果: {answer}")

    # agent_tracer = AgentTracer()
    # result = run_agent_with_trace("1、北京天气怎么样？顺便帮我算一下 156 * 23; 2、上海天气怎么样？顺便帮我算一下 1126 * 523", agent_tracer)
    # print(f"\n{'='*50}")
    # print(f"🎯 最终结果: {result}")