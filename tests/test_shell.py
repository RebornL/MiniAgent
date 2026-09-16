"""跨包集成：`run_command`（`capabilities.shell` + `providers.process` + `app.assembly`）。

票面要求的三条**真实子进程**验收（命令先过沙箱 seam，再进受管范围）：

1. 正常命令返回它的输出与退出码（非零退出码是正常结果，不是「失败」结局）；
2. 命令确实跑在受管范围里——拿到范围句柄后**可被外部终止**，工具随之返回；
3. 被拒时工具体未执行（外部命令从未启动，用「本该写出的文件不存在」证明）。

另加：参数不经 shell 原样送达、`cwd` 生效、真实输出流也会被截断，装配层确实
让 `shell` 技能默认不装载、并为 `run_command` 装了审批（无审批者默认拒绝），
且默认不装载的 `shell` 仍能从 meta 工具的描述里被发现。

运行：`python -m pytest tests/test_shell.py -v`
"""
from __future__ import annotations

import sys
import threading
import time

from app.assembly import SHELL_ENV_ALLOWLIST, build_harness
from capabilities.permission.provider import PermissionPlugin
from capabilities.shell.definition import truncated_note
from capabilities.shell.provider import ShellTool
from miniharness.sandbox.contract import SandboxPolicy
from miniharness.tools.contract import DENIED, OK
from miniharness.tools.runtime import ToolRuntime
from providers.mock import MockLLM
from providers.process import SubprocessRange, SubprocessSeam
from tests.support import _marker_command, _shell_harness

#: 与装配层同一份沙箱策略（`app.assembly.SHELL_ENV_ALLOWLIST`）：这里验的是装配后的行为。
_POLICY = SandboxPolicy(env_allowlist=SHELL_ENV_ALLOWLIST)


def _harness(*, seam: SubprocessSeam | None = None, plugin=None,
             tool: ShellTool | None = None, approver: bool = False) -> ToolRuntime:
    """最小装配：工具流水线 + 受管范围 seam + 沙箱 seam + 可选审批策略 + `run_command`。"""
    return _shell_harness(seam=seam, plugin=plugin, tool=tool,
                          approver=approver, policy=_POLICY)[1]


def test_run_command_reports_stdout_stderr_and_exit_code():
    """AC1：真实子进程的输出与退出码原样回来；非零退出码是正常结果，不是失败结局。"""
    runtime = _harness()

    ok = runtime.run({"id": "c1", "name": "run_command", "args": {"argv": [
        sys.executable, "-c",
        "import sys; print('出到 stdout'); print('出到 stderr', file=sys.stderr)"]}})

    assert ok["status"] == OK
    assert ok["value"]["exit_code"] == 0
    assert ok["value"]["stdout"].strip() == "出到 stdout"
    assert ok["value"]["stderr"].strip() == "出到 stderr"
    assert ok["value"]["stdout_truncated"] is False
    assert ok["value"]["stderr_truncated"] is False

    nonzero = runtime.run({"id": "c2", "name": "run_command",
                           "args": {"argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}})

    assert nonzero["status"] == OK                    # 非零退出码不是工具失败
    assert nonzero["value"]["exit_code"] == 3


def test_arguments_reach_the_child_verbatim_and_cwd_applies(tmp_path):
    """AC2：参数不经 shell（元字符原样送达）；`cwd` 对子进程生效。"""
    runtime = _harness()

    verbatim = runtime.run({"id": "c1", "name": "run_command", "args": {"argv": [
        sys.executable, "-c", "import sys; print(sys.argv[1]); print(sys.argv[2])",
        "a; echo pwned", "$(whoami) && rm -rf /"]}})

    assert verbatim["value"]["stdout"].splitlines() == ["a; echo pwned", "$(whoami) && rm -rf /"]

    runtime.run({"id": "c2", "name": "run_command", "args": {
        "argv": [sys.executable, "-c", "open('here.txt', 'w', encoding='utf-8').write('x')"],
        "cwd": str(tmp_path)}})

    assert (tmp_path / "here.txt").read_text(encoding="utf-8") == "x"


def test_the_command_runs_in_a_managed_range_and_can_be_terminated_from_outside():
    """AC3：命令跑在受管范围里——外部拿到句柄即可终止，工具随之返回。

    「可被策略层终止」在这里是可观察事实：工具阻塞在 `wait_for_exit` 上，范围被终止
    （`terminate` 是策略层 / 超时接线的动词）后它立刻带着退出码返回。
    """
    ranges: list[SubprocessRange] = []

    class _RecordingSeam(SubprocessSeam):
        def spawn(self, argv, **kwargs):
            range_ = super().spawn(argv, **kwargs)
            ranges.append(range_)
            return range_

    runtime = _harness(seam=_RecordingSeam())
    results: list[dict] = []
    worker = threading.Thread(target=lambda: results.append(runtime.run(
        {"id": "c1", "name": "run_command", "args": {
            "argv": [sys.executable, "-c", "import time; time.sleep(300)"]}})), daemon=True)
    worker.start()

    deadline = time.monotonic() + 20
    while not ranges and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ranges, "命令没有进入受管范围"
    range_ = ranges[0]
    assert range_.poll() is None                      # 空洞性：命令确实在跑，还没退出

    range_.terminate(grace_ms=500)                    # 外部（策略层）终止整个范围
    worker.join(timeout=20)

    assert not worker.is_alive()
    assert results[0]["status"] == OK                 # 工具带着终止后的退出码返回
    assert results[0]["value"]["exit_code"] is not None
    assert range_.poll() is not None                  # 范围已退出，没留活口


def test_a_real_flood_is_capped():
    """真实输出流超限也会被截断：`yes` 那类洪泛既撑不爆内存，也照样给出退出码。"""
    limit = 512
    runtime = _harness(tool=ShellTool(limit=limit, policy=_POLICY))

    value = runtime.run({"id": "c1", "name": "run_command", "args": {"argv": [
        sys.executable, "-c", "print('x' * 200000)"]}})["value"]

    assert value["exit_code"] == 0
    assert value["stdout_truncated"] is True
    assert value["stdout"] == "x" * limit + truncated_note(limit)
    assert len(value["stdout"]) < 200_000


def test_a_denied_command_never_starts(tmp_path):
    """被拒时工具体未执行：`deny` 与「`ask` 且无审批者」都不会启动外部命令。"""
    marker = tmp_path / "ran.txt"
    args = {"argv": _marker_command(marker)}

    denied = _harness(plugin=PermissionPlugin(denied={"run_command"}))
    needs_approval = _harness(plugin=PermissionPlugin(approval_required={"run_command"}))

    for label, runtime in (("deny", denied), ("ask 无审批者", needs_approval)):
        result = runtime.run({"id": "c1", "name": "run_command", "args": args})

        assert result["status"] == DENIED, label
        assert not marker.exists(), label

    # 空洞性证明：同样的命令在审批者放行后确实执行（不是命令本身跑不起来）
    approved = _harness(plugin=PermissionPlugin(approval_required={"run_command"}),
                        approver=True)
    allowed = approved.run({"id": "c2", "name": "run_command", "args": args})

    assert allowed["status"] == OK
    assert marker.read_text(encoding="utf-8") == "ran"


def test_the_assembled_harness_ships_shell_unloaded_and_behind_approval(tmp_path):
    """装配层：`shell` 技能默认不装载；装载后 `run_command` 仍走 `ask`，无审批者默认拒绝。"""
    ctx, _, _ = build_harness(session_id="s1", store_dir=str(tmp_path),
                              llm=MockLLM())
    runtime = ctx.get("tools")
    marker = tmp_path / "ran.txt"
    args = {"argv": _marker_command(marker)}

    assert runtime.get("run_command") is None         # 默认不装载：模型须先 load_skill

    ctx.get("skills").load("shell")                   # 显式装载（load_skill 的工具路径）
    assert runtime.get("run_command") is not None

    denied = runtime.run({"id": "c1", "name": "run_command", "args": args})

    assert denied["status"] == DENIED                 # 无审批者 → 默认拒绝
    assert not marker.exists()

    ctx.on("tools/approve", lambda payload, next_: {"kind": "allow"})
    allowed = runtime.run({"id": "c2", "name": "run_command", "args": args})

    assert allowed["status"] == OK
    assert marker.read_text(encoding="utf-8") == "ran"


def test_the_shell_skill_is_discoverable_while_unloaded(tmp_path):
    """F2：模型只看到 meta 工具时 `shell` 仍点得到——`load_skill` 的描述列出可用技能。

    `shell` 默认不装载：`run_command` 不可见，它的 system_prompt 也要装载后才注入，base
    prompt 又只点名 `structured-output`。所以「可用技能」只能从 meta 工具的描述里发现。
    """
    ctx, _, _ = build_harness(session_id="s1", store_dir=str(tmp_path),
                              llm=MockLLM())
    runtime = ctx.get("tools")

    def descriptions() -> dict[str, str]:
        return {spec["function"]["name"]: spec["function"]["description"]
                for spec in runtime.specs()}

    assert "run_command" not in descriptions()             # 默认不装载：工具不可见
    assert "shell" in descriptions()["load_skill"]         # 但可用技能里点得到

    ctx.get("skills").load("shell")
    assert "shell" in descriptions()["unload_skill"]       # 装载后：已加载集里看得到
