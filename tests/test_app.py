"""跨包集成：app 装配（`build_harness` / `resume_session`）。

- 装配的 harness 能跑通一次 turn，并把日志落进持久化 store；
- 工具懒注册：只随技能装载可用（与 legacy `get_active_tools()` 同语义）；
- `register_skills` 注册的技能可经 `load_skill` 装载，`final_output` 借此终结本轮；
- `resume_session` 只靠重放事件日志恢复：模型可见历史、压缩摘要与技能状态都由日志重建，
  `meta.json` 里不再有 summary / active_skills 旁路（旧会话的那份旁路在迁移翻译期折进日志）；
- 旧会话迁移：v0 落盘形态的 fixture 迁移后技能工具已注册、摘要链非空；
- `meta.json` 不存模型可见内容，列表要展示的末条输入按需由日志重算；
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
from capabilities.compaction.definition import compaction_summaries
from capabilities.persistence.provider import META_FILE, PersistenceManager, Store
from miniharness.session import Session
from providers.mock import MockLLM

LONG = "这是一段很长的历史对话内容，用来把 token 数推过阈值。" * 6

#: v0 落盘形态的 fixture（消息数组 + `meta.json` 旁路）。结构与真实旧会话逐字一致，
#: 但内容是合成文本——真实会话数据不入库（`agent_sessions/` 在 .gitignore 里，仓库是公开的）。
LEGACY_V0_ID = "20260711_221103_a5190915"
LEGACY_V0_DIR = Path(__file__).resolve().parent / "fixtures" / "legacy-v0-session"


def _meta(tmp_path: Path, session_id: str) -> dict:
    return json.loads((tmp_path / session_id / META_FILE).read_text(encoding="utf-8"))


def _latest_tool_content(messages: list[dict]) -> dict:
    """把最后一次工具观察当作最终答复 —— 工具没执行成功时答复就会露馅。"""
    return {"text": messages[-1]["content"]}



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

    outcome = loop.turn("6 * 7 是多少")

    assert outcome["text"] == "6 * 7 = 42"
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

    outcome = loop.turn("查北京天气并结构化输出")

    assert json.loads(outcome["text"]) == {"result": {"city": "北京"}, "summary": "北京"}
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


def test_restart_rebuilds_a_compacted_history_word_for_word(tmp_path):
    """AC2：压缩 → 重启 → 只靠重放，投影与压缩后逐字一致（meta.json 里没有摘要旁路）。"""
    llm = MockLLM().then_text("收到")
    _, session, loop = build_harness(model="mock", session_id="s1",
                                     store_dir=str(tmp_path), llm=llm)
    for index in range(20):                                # 预置一段超阈值的历史
        role = "user" if index % 2 == 0 else "assistant"
        session.append(f"{role}/message", content=LONG)
    loop.turn("压缩一下")

    (compacted,) = [e for e in session.events if e["type"] == "context/compacted"]
    before = session.derive_messages()
    assert before[0] == {"role": "user", "content": f"[上下文已压缩] {compacted['summary']}"}
    assert len(before) < 20                                # 被遮蔽的事件确实没进投影

    # 重启：新 harness 只重放磁盘上的日志
    llm2 = MockLLM().then_text("继续")
    assert resume_session("s1", "继续", model="mock", store_dir=str(tmp_path),
                          llm=llm2) == "继续"

    # 恢复后的那次采样：重放出的投影 == 压缩后的投影逐字 + 本轮新输入
    assert llm2.calls[0] == before + [{"role": "user", "content": "继续"}]
    meta = _meta(tmp_path, "s1")
    assert "summary" not in meta and "active_skills" not in meta
    # 展示用的派生值仍可由日志重建（不是第二份权威源）
    stored = PersistenceManager(Store(str(tmp_path))).load_events("s1")
    assert meta["message_count"] == len(Session(events=stored).derive_messages())


def test_a_second_compaction_after_a_restart_merges_the_replayed_summary(tmp_path):
    """重启后接着压缩：`existing` 是重放出来的那份摘要，而不是空的（摘要链从日志重建）。"""
    llm = MockLLM().then_text("收到")
    ctx, session, loop = build_harness(model="mock", session_id="s1",
                                       store_dir=str(tmp_path), llm=llm)
    for index in range(20):
        role = "user" if index % 2 == 0 else "assistant"
        session.append(f"{role}/message", content=LONG)
    loop.turn("压缩一下")

    pm = PersistenceManager(Store(str(tmp_path)))
    first = [e["summary"] for e in pm.load_events("s1") if e["type"] == "context/compacted"]
    assert len(first) == 1

    # 会话在崩溃前又长了一截：落盘后重启
    for _ in range(15):
        session.append("user/message", content=LONG)
    ctx.get("persistence").flush()

    llm2 = MockLLM().then_text("继续")
    assert resume_session("s1", "继续", model="mock", store_dir=str(tmp_path),
                          llm=llm2) == "继续"

    stored = pm.load_events("s1")
    summaries = [e["summary"] for e in stored if e["type"] == "context/compacted"]
    assert len(summaries) == 2
    # 增量合并拿到了重放出的旧摘要（否则第二次摘要不会以它开头）
    assert summaries[1].startswith(summaries[0])
    # 投影只留最新的那份替换内容，被遮蔽的原始事件仍在日志里
    projected = Session(events=stored).derive_messages()
    visible = [m for m in projected if m["content"].startswith("[上下文已压缩]")]
    assert visible == [{"role": "user", "content": f"[上下文已压缩] {summaries[1]}"}]
    # 被遮蔽的原始事件一条都没删：10 条原始 user + 15 条补充 + 两次本轮输入
    assert len([e for e in stored if e["type"] == "user/message"]) == 10 + 15 + 2


def test_restart_restores_the_active_skill_from_the_log_alone(tmp_path):
    """AC3：技能装载 → 重启 → 只靠重放，技能状态与领域提示仍在（无需 load_skill 再来一次）。"""
    llm = MockLLM().then_tool_call("load_skill", {"name": "calculator"}).then_text("算好了")
    _, session, loop = build_harness(model="mock", session_id="s1",
                                     store_dir=str(tmp_path), llm=llm)
    loop.turn("加载计算器")

    # 装载写成事件；领域提示进了模型可见历史
    assert [(e["type"], e["name"]) for e in session.events
            if e["type"].startswith("skill/")] == [("skill/loaded", "calculator")]
    assert any(m["role"] == "system" and "你拥有计算能力" in m["content"]
               for m in session.derive_messages())

    # 重启后直接调技能工具（不再 load_skill）：工具注册必须已由日志重放重建
    llm2 = MockLLM().then_tool_call("calculate", {"expression": "6 * 7"})
    llm2.script.append(_latest_tool_content)
    assert resume_session("s1", "接着算", model="mock", store_dir=str(tmp_path),
                          llm=llm2) == "42"

    assert any(m["role"] == "system" and "你拥有计算能力" in m["content"]
               for m in llm2.calls[0])
    assert [e["name"] for e in PersistenceManager(Store(str(tmp_path))).load_events("s1")
            if e["type"] == "tool/result"] == ["load_skill", "calculate"]
    meta = _meta(tmp_path, "s1")
    assert "summary" not in meta and "active_skills" not in meta


def test_a_legacy_v0_session_resumes_with_its_skills_and_summary_chain(tmp_path):
    """F1 回归：v0 旧会话迁移后不丢技能状态与摘要链。

    fixture 是 v0 落盘**形态**的样本（消息数组 + `meta.json` 里的旁路）——结构与真实旧会话
    逐字一致、内容是合成文本。它的日志里既没有 `skill/*` 事件、也没有 `context/compacted`
    事件：旁路若不在**翻译期**折进日志，技能工具就注册不上（`calculate` 会答「工具未注册」），
    摘要链也从空重启。
    """
    store_dir = tmp_path / "sessions"
    (store_dir / LEGACY_V0_ID).parent.mkdir(parents=True)
    shutil.copytree(LEGACY_V0_DIR, store_dir / LEGACY_V0_ID)
    legacy = json.loads((store_dir / LEGACY_V0_ID / META_FILE).read_text(encoding="utf-8"))
    assert legacy["active_skills"]                       # 这份状态只存在于旁路里

    # resume 后直接调技能工具（不再 load_skill）：注册必须已由翻译出的日志重建
    llm = MockLLM().then_tool_call("calculate", {"expression": "6 * 7"})
    llm.script.append(_latest_tool_content)
    assert resume_session(LEGACY_V0_ID, "接着算", model="mock", store_dir=str(store_dir),
                          llm=llm) == "42"               # 工具真跑过才拿得到 42
    assert any(m["role"] == "system" and "你拥有计算能力" in m["content"]
               for m in llm.calls[0])                    # 领域提示也重建了

    events = PersistenceManager(Store(str(store_dir))).load_events(LEGACY_V0_ID)
    assert [e["name"] for e in events
            if e["type"] == "skill/loaded"] == legacy["active_skills"]
    # 摘要链非空：旁路里的 summary 折成了一条压缩事件（真实旧会话没压缩过，故其值为空串）
    assert compaction_summaries(events) == [legacy["summary"]]
    preserved = [e for e in events if e["type"] == "context/compacted"][-1]
    assert preserved["shadowed_seqs"] == [] and preserved["replacement"] == []
    # 仅用于保留：不遮蔽任何事件，也不给投影注入替换内容
    assert all("上下文已压缩" not in str(m.get("content"))
               for m in Session(events=events).derive_messages())


def test_meta_holds_no_copy_of_model_visible_content(tmp_path):
    """F3：`meta.json` 只存可由日志重算的计数索引，列表要展示的末条输入按需重算。"""
    llm = MockLLM().then_text("收到")
    _, _, loop = build_harness(model="mock", session_id="s1",
                               store_dir=str(tmp_path), llm=llm)
    loop.turn("北京天气如何")

    meta = _meta(tmp_path, "s1")
    assert "北京天气如何" not in json.dumps(meta, ensure_ascii=False)   # 不落内容的拷贝
    listed = PersistenceManager(Store(str(tmp_path))).list_sessions()
    assert [s["last_message"] for s in listed if s["id"] == "s1"] == ["北京天气如何"]
    assert listed[0]["message_count"] == meta["message_count"]


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
