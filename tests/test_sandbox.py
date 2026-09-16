"""跨包集成：沙箱 seam（`capabilities.shell` + `miniharness.sandbox` + `providers.sandbox`）。

票面四条验收在这里逐条可验：

1. **AC1**：调用意图（argv / cwd）+ 策略进沙箱，回可执行的 argv + 完整性要求
   （本文件验装配后的事实；形状细节在 `providers/sandbox/test_env_sandbox.py`）；
2. **AC2**：沙箱不可用时 **fail-closed**——两处失败注入（**seam 没装**、**seam 拒绝服务**），
   各用「本该写出的 marker 文件不存在」证明命令真的没跑，并各附**非空洞对照**：同一条命令
   （`sys.executable` 绝对路径，逐个用例都同一条）装上可用沙箱后 `status=ok` 且 marker 写入；
3. **AC3**：沙箱不负责终止——它只产出 argv 与环境，终止仍归受管范围；可观察证据在
   `tests/test_shell.py::test_the_command_runs_in_a_managed_range_and_can_be_terminated_from_outside`
   （那条命令同样先过沙箱），以及 `providers/sandbox/test_env_sandbox.py` 的
   「包装本身不执行任何东西」；
4. **AC4**：沙箱里跑的命令，退出与输出仍走 `ToolRuntime` 的**同一条结果通道**
   （`tools/result` + 同一个结果 dict）。

运行：`python -m pytest tests/test_sandbox.py -v`
"""
from __future__ import annotations

import json
import sys

from app.assembly import build_harness
from miniharness.core import Context
from miniharness.sandbox.contract import SandboxPolicy
from miniharness.tools.contract import FAILED, OK
from miniharness.tools.runtime import ToolRuntime
from providers.mock import MockLLM
from providers.sandbox import SANDBOX_MARKER, EnvSandbox
from tests.support import _marker_command, _shell_harness


def _harness(*, sandbox: EnvSandbox | None = None, load_sandbox: bool = True,
             policy: SandboxPolicy | None = None) -> tuple[Context, ToolRuntime]:
    """最小装配：受管范围 seam + （可选）沙箱 seam + `run_command`。

    `load_sandbox=False` 即**不装载沙箱**——AC2 的第一处失败注入。
    """
    return _shell_harness(sandbox=sandbox, load_sandbox=load_sandbox, policy=policy)


def test_a_sandboxed_command_reports_through_the_common_result_channel(monkeypatch):
    """AC1 + AC4：命令在沙箱约定的环境里跑，退出码与两个流仍走通用结果通道。"""
    monkeypatch.setenv("MINIHARNESS_TEST_SECRET", "s3cr3t")
    ctx, runtime = _harness(policy=SandboxPolicy(env_allowlist=("PATH",)))
    results: list[dict] = []
    ctx.on("tools/result", lambda payload: results.append(payload["result"]))

    result = runtime.run({"id": "c1", "name": "run_command", "args": {"argv": [
        sys.executable, "-c",
        "import os, sys;"
        "print(os.environ.get('MINIHARNESS_SANDBOX'));"
        "print('has-secret', os.environ.get('MINIHARNESS_TEST_SECRET') is not None,"
        " file=sys.stderr);"
        "sys.exit(3)"]}})

    assert result["status"] == OK                        # 非零退出码是正常结果，不是失败结局
    lines = result["value"]["stdout"].splitlines()
    assert lines[0] == "env"                             # 子进程看到沙箱标记（环境已收敛）
    assert result["value"]["stderr"].strip() == "has-secret False"   # 凭据没跟着进去
    assert result["value"]["exit_code"] == 3
    assert json.loads(result["content"])["exit_code"] == 3
    assert [r["status"] for r in results] == [OK]         # 同一条结果通道（tools/result）
    assert results[0]["value"]["exit_code"] == 3


def test_a_missing_sandbox_seam_fails_closed_and_nothing_runs(tmp_path):
    """AC2 失败注入一：沙箱没装 → 结构化 `failed`，命令**真的没跑**（marker 不存在）。"""
    marker = tmp_path / "ran.txt"
    _, runtime = _harness(load_sandbox=False)

    result = runtime.run({"id": "c1", "name": "run_command",
                          "args": {"argv": _marker_command(marker)}})

    assert result["status"] == FAILED
    assert "沙箱不可用" in result["error"]
    assert not marker.exists()                           # 没有回退到无约束执行

    # 非空洞对照：同一条命令、同一个装配，装上可用沙箱后真的跑起来并写下了 marker
    contrast = tmp_path / "contrast.txt"
    _, working = _harness()
    assert working.run({"id": "c2", "name": "run_command",
                        "args": {"argv": _marker_command(contrast)}})["status"] == OK
    assert contrast.read_text(encoding="utf-8") == "ran"


def test_a_sandbox_that_refuses_the_policy_fails_closed_and_nothing_runs(tmp_path):
    """AC2 失败注入二：沙箱在场但**拒绝服务**（策略超出后端能力）→ 同样失败、同样没跑。"""
    marker = tmp_path / "ran.txt"
    _, runtime = _harness(policy=SandboxPolicy(deny_network=True))

    result = runtime.run({"id": "c1", "name": "run_command",
                          "args": {"argv": _marker_command(marker)}})

    assert result["status"] == FAILED
    assert "不能强制断网" in result["error"]
    assert not marker.exists()

    # 非空洞对照：同一条命令，只把那个后端做不到的要求去掉，就正常执行
    contrast = tmp_path / "contrast.txt"
    _, working = _harness(policy=SandboxPolicy())
    assert working.run({"id": "c2", "name": "run_command",
                        "args": {"argv": _marker_command(contrast)}})["status"] == OK
    assert contrast.read_text(encoding="utf-8") == "ran"


def test_unloading_the_sandbox_stops_the_next_command(tmp_path):
    """seam 是**调用时刻**取的：装配后卸载沙箱，下一条命令立刻 fail-closed（不是缓存下来的）。"""
    ctx, runtime = _harness()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"

    assert runtime.run({"id": "c1", "name": "run_command",
                        "args": {"argv": _marker_command(first)}})["status"] == OK

    assert ctx.unload(ctx.get("sandbox"))                # 卸载沙箱 seam
    result = runtime.run({"id": "c2", "name": "run_command",
                          "args": {"argv": _marker_command(second)}})

    assert result["status"] == FAILED
    assert first.read_text(encoding="utf-8") == "ran"    # 卸载前确实执行过
    assert not second.exists()                           # 卸载后一次都没执行


def test_the_assembled_harness_runs_commands_inside_the_sandbox(tmp_path):
    """装配落位：`build_harness` 装的就是这个沙箱——审批放行后命令确实在沙箱环境里跑。"""
    ctx, _, _ = build_harness(session_id="s1", store_dir=str(tmp_path),
                              llm=MockLLM())
    ctx.get("skills").load("shell")
    ctx.on("tools/approve", lambda payload, next_: {"kind": "allow"})
    probe = tmp_path / "probe.txt"

    result = ctx.get("tools").run({"id": "c1", "name": "run_command", "args": {"argv": [
        sys.executable, "-c",
        "import os;"
        f"open(r'{probe}', 'w', encoding='utf-8').write(os.environ['{SANDBOX_MARKER}'])"]}})

    assert result["status"] == OK
    assert probe.read_text(encoding="utf-8") == "env"     # 沙箱标记确实在子进程环境里
