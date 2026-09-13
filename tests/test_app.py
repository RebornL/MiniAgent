"""跨包集成：app 装配（`build_harness` / `resume_session`）。

- 装配的 harness 能跑通一次 turn，并把日志落进持久化 store；
- 工具懒注册：只随技能装载可用（与 legacy `get_active_tools()` 同语义）；
- `register_skills` 注册的技能可经 `load_skill` 装载，`final_output` 借此终结本轮；
- `resume_session` 从事件日志重放恢复会话，并还原 summary / active_skills；
- 导入期不碰 config.json（全新 clone 上 `import app` 必须成功）。

运行：`python -m pytest tests/test_app.py -v`
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app.assembly import build_harness, resume_session
from capabilities.persistence.definition import PersistenceManager, Store
from miniharness.session import Session
from providers.mock import MockLLM



def test_build_harness_keeps_tools_lazy_until_the_skill_is_loaded(tmp_path):
    llm = (MockLLM()
           .then_tool_call("load_skill", {"name": "calculator"})
           .then_tool_call("calculate", {"expression": "6 * 7"})
           .then_text("6 * 7 = 42"))

    ctx, session, loop = build_harness(model="mock", session_id="s1",
                                       store_dir=str(tmp_path), llm=llm)

    runtime = ctx.get("tools")
    # 懒注册：初始只有 meta tools（legacy get_active_tools() 的语义）
    assert runtime.get("load_skill") is not None and runtime.get("unload_skill") is not None
    assert all(runtime.get(name) is None
               for name in ("calculate", "search_web", "read_file", "write_file"))

    answer = loop.turn("6 * 7 是多少")

    assert answer == "6 * 7 = 42"
    assert runtime.get("calculate") is not None            # 技能装载后工具才可见
    assert [event["name"] for event in session.events
            if event["type"] == "tool/result"] == ["load_skill", "calculate"]
    # 落日志：磁盘上就是事件日志本身（同一个真相源），模型可见内容由它重放重建
    stored = PersistenceManager(Store(str(tmp_path))).load_events("s1")
    assert stored == session.events
    messages = Session(events=stored).derive_messages()
    assert any(message["role"] == "tool" and "42" in message["content"] for message in messages)


def test_build_harness_loaded_skill_provides_the_terminal_tool(tmp_path):
    """技能装载 → final_output 可用 → 终结本轮且不再采样（MiniAgent 的原有行为）。"""
    llm = (MockLLM()
           .then_tool_call("load_skill", {"name": "structured-output"})
           .then_tool_call("final_output", {"result": {"city": "北京"}, "summary": "北京"})
           .then_text("不应走到这一步"))
    _, _, loop = build_harness(model="mock", session_id="s2",
                               store_dir=str(tmp_path), llm=llm)

    answer = loop.turn("查北京天气并结构化输出")

    assert json.loads(answer) == {"result": {"city": "北京"}, "summary": "北京"}
    assert len(llm.calls) == 2                       # 终结工具后不再采样


def test_skill_prompt_reaches_model_after_load(tmp_path):
    """legacy 每步重算 system prompt：装载技能后其领域提示必须立刻进入模型可见历史。"""
    llm = (MockLLM()
           .then_tool_call("load_skill", {"name": "calculator"})
           .then_text("算好了"))

    _, _, loop = build_harness(model="mock", session_id="s3",
                               store_dir=str(tmp_path), llm=llm)
    loop.turn("帮我算 2+2")

    # 装载 calculator 之后的那次采样，模型必须看到该技能的领域提示
    assert any(m["role"] == "system" and "你拥有计算能力" in m["content"]
               for m in llm.calls[1])


def test_resume_session_restores_summary_and_active_skills(tmp_path):
    """落盘的 summary / active_skills 在恢复后确实被还原（否则下次落盘会被清空）。"""
    llm = MockLLM().then_tool_call("load_skill", {"name": "calculator"}).then_text("算好了")
    ctx, session, loop = build_harness(model="mock", session_id="s1",
                                       store_dir=str(tmp_path), llm=llm)
    ctx.get("compaction").context.restore("此前讨论过 2+2")
    loop.turn("加载计算器")

    pm = PersistenceManager(Store(str(tmp_path)))
    state = pm.load_session("s1")
    assert state["active_skills"] == ["calculator"]
    assert state["summary"] == "此前讨论过 2+2"
    assert state["runs"]                    # 追踪 span 也随会话落盘，不被空数组覆盖

    # 新 harness 重放日志后继续一轮：若日志没重放、summary/skills 没还原，再落盘就丢了
    llm2 = MockLLM().then_text("继续")
    assert resume_session("s1", "继续", model="mock", store_dir=str(tmp_path),
                          llm=llm2) == "继续"

    restored = pm.load_session("s1")
    assert restored["active_skills"] == ["calculator"]
    assert restored["summary"] == "此前讨论过 2+2"
    # 恢复的那次采样：重放出的历史与已激活技能的领域提示都进了模型可见历史
    first_call = llm2.calls[0]
    assert any(m["role"] == "user" and m["content"] == "加载计算器" for m in first_call)
    assert any(m["role"] == "system" and "你拥有计算能力" in m["content"] for m in first_call)


def test_restart_replays_the_log_into_the_same_history_word_for_word(tmp_path):
    """AC2：重启后从日志重放恢复，会话内容与中断前逐字一致（没有旁路来源）。"""
    llm = (MockLLM()
           .then_tool_call("load_skill", {"name": "calculator"})
           .then_tool_call("calculate", {"expression": "6 * 7"})
           .then_text("6 * 7 = 42"))
    _, session, loop = build_harness(model="mock", session_id="s1",
                                     store_dir=str(tmp_path), llm=llm)
    loop.turn("6 * 7 是多少")
    before = session.derive_messages()

    # 重启：新 harness（新 Session / 新 Context / 新消费者）只从磁盘上的日志重放
    llm2 = MockLLM().then_text("继续")
    assert resume_session("s1", "继续", model="mock", store_dir=str(tmp_path),
                          llm=llm2) == "继续"

    # 恢复后的那次采样，模型收到的历史 == 中断前的投影逐字 + 本轮新输入
    assert llm2.calls[0] == before + [{"role": "user", "content": "继续"}]
    # 日志只增不改：重放出来的前缀就是中断前那份日志
    after = PersistenceManager(Store(str(tmp_path))).load_events("s1")
    assert after[:len(session.events)] == session.events


def test_import_works_on_a_fresh_clone_without_config_json(tmp_path):
    """config.json 在 .gitignore 里：没有它也必须能 `import app`（导入期不读配置）。"""
    root = Path(__file__).resolve().parents[1]
    for package in ("app", "capabilities", "miniharness", "providers", "tests"):
        shutil.copytree(root / package, tmp_path / package,
                        ignore=shutil.ignore_patterns("__pycache__"))
    assert not (tmp_path / "config.json").exists()

    proc = subprocess.run(
        [sys.executable, "-c", "import app; print('ok')"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True, text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
