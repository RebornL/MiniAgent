"""包内测试：沙箱后端（`providers.sandbox.EnvSandbox`）。

只依赖本包与更下层的契约（`miniharness.sandbox.contract`）；用**真实的环境变量**与真实存在
的文件系统验证它**真能强制**的那些维度，不跑任何子进程——沙箱一个进程都不起（起进程、等
退出、终止是受管范围的事，见 `providers/process/test_managed_range.py`）。

覆盖：环境收敛（白名单之外一个都不留）、子进程不会拿到凭据、裸命令名在**收敛后的 PATH** 里
解析、解析不了就拒绝服务、策略要求而它强制不了的隔离（断网）也拒绝服务、
以及「包装本身不执行任何东西」——沙箱不拥有进程生命周期。

运行：`python -m pytest providers/sandbox/test_env_sandbox.py -v`
"""
from __future__ import annotations

import os
import sys

import pytest

from miniharness.sandbox.contract import (
    CommandIntent,
    SandboxPolicy,
    SandboxUnavailableError,
)
from providers.sandbox import SANDBOX_MARKER, EnvSandbox


def test_the_converged_environment_holds_the_allowlist_plus_the_sandbox_marker(monkeypatch):
    """AC1：产物是「可执行的 argv + 完整性要求」——要求里的环境就是子进程将看到的环境。"""
    monkeypatch.setenv("MINIHARNESS_TEST_SECRET", "s3cr3t")
    monkeypatch.setenv("MINIHARNESS_TEST_KEEP", "kept")
    sandbox = EnvSandbox()

    wrapped = sandbox.wrap(
        CommandIntent((sys.executable, "-c", "pass"), cwd="/somewhere"),
        SandboxPolicy(env_allowlist=("MINIHARNESS_TEST_KEEP",)))

    assert wrapped.argv == (sys.executable, "-c", "pass")   # 已给路径：原样（不二次解析）
    assert wrapped.requirements.env == {                    # 白名单之外一个都不留
        "MINIHARNESS_TEST_KEEP": "kept",
        SANDBOX_MARKER: "env",
    }
    assert "MINIHARNESS_TEST_SECRET" not in wrapped.requirements.env
    assert wrapped.requirements.cwd == "/somewhere"         # 工作目录随要求一起给出


def test_a_bare_program_name_is_resolved_against_the_converged_path(tmp_path, monkeypatch):
    """AC1：「可执行的 argv」是字面意思——裸名字解析成绝对路径，跑哪个二进制由沙箱决定。"""
    stub = tmp_path / ("probe.exe" if os.name == "nt" else "probe")
    stub.write_text("", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    wrapped = EnvSandbox().wrap(CommandIntent(("probe", "--flag")),
                                SandboxPolicy(env_allowlist=("PATH",)))

    assert os.path.normcase(wrapped.argv[0]) == os.path.normcase(str(stub))
    assert wrapped.argv[1] == "--flag"                      # 其余参数逐项原样


def test_a_bare_program_name_the_converged_environment_cannot_resolve_is_refused(monkeypatch):
    """fail-closed：解析不出「到底跑哪一个」时拒绝服务，而不是交给调用方的环境去碰运气。"""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    sandbox = EnvSandbox()

    with pytest.raises(SandboxUnavailableError):
        sandbox.wrap(CommandIntent(("probe-not-here",)), SandboxPolicy())   # 策略没给 PATH

    with pytest.raises(SandboxUnavailableError):
        sandbox.wrap(CommandIntent(("probe-not-here",)), SandboxPolicy(env_allowlist=("PATH",)))


def test_a_demand_the_backend_cannot_enforce_fails_closed_not_silently():
    """AC2 的实现侧：策略要求断网，而后端强制不了——拒绝服务，绝不假装隔离生效。"""
    intent = CommandIntent((sys.executable, "--flag"))
    sandbox = EnvSandbox()

    with pytest.raises(SandboxUnavailableError):
        sandbox.wrap(intent, SandboxPolicy(deny_network=True))

    # 对照（非空洞）：同一个沙箱、同一条意图，在没有断网要求时照常包装
    assert sandbox.wrap(intent, SandboxPolicy()).argv == intent.argv


def test_wrapping_alone_starts_nothing(tmp_path):
    """AC3：沙箱不拥有进程生命周期——`wrap` 只产出 argv 与要求，一个进程都不起。"""
    marker = tmp_path / "ran.txt"
    wrapped = EnvSandbox().wrap(
        CommandIntent((sys.executable, "-c",
                       f"open(r'{marker}', 'w', encoding='utf-8').write('ran')")),
        SandboxPolicy())

    assert wrapped.argv[1:]                     # 产物齐备
    assert not marker.exists()                  # 但没有任何东西被执行


def test_an_empty_argv_is_refused_before_anything_else():
    """空 argv 无从包装：在解析之前就拒绝（`ValueError`，不是沙箱不可用）。"""
    with pytest.raises(ValueError):
        EnvSandbox().wrap(CommandIntent(()), SandboxPolicy())
