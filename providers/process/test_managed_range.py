"""包内测试：受管范围的平台后端（`providers.process`）。

用**真实子进程**证明受管范围的语义：终止以整棵进程树（含后代）为单位、可重复调用；
释放返回时范围已经退出，不留孤儿；**组长自己先退出也不让范围变空**——这是范围本位与
组长本位的分界。两条平台路径（POSIX 信号组升级 / Windows Job Object）共用这同一批断言；
本机只实跑所在平台的那条。**「宽限 → 强杀」的升级档只存在于 POSIX 后端**：Windows 上
`TerminateJobObject` 一次强杀到底，没有升级档（`grace_ms` 在那里只是等待的收尾上限），
所以它的真实触发用例带 `skipif(os.name == "nt")` ——见
`test_terminate_escalates_to_a_hard_kill_when_the_signal_is_ignored`。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

import pytest

from providers.process import SubprocessRange, SubprocessSeam
from providers.process.probe import _alive, _assert_gone


#: 子进程树剧本：组长再派生一个后代，把后代的 pid 写进 argv[1]，然后两个一起长睡。
_TREE_SCRIPT = """\
import subprocess, sys, time
from pathlib import Path
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
Path(sys.argv[1]).write_text(str(grandchild.pid))
time.sleep(300)
"""

#: 孤儿剧本：组长派生一个长睡后代、写下它的 pid，然后**自己立刻退出**。
_ORPHAN_SCRIPT = """\
import subprocess, sys
from pathlib import Path
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
Path(sys.argv[1]).write_text(str(grandchild.pid))
"""

#: 拥有者剧本：起一个长睡范围，证明它确实在跑，然后**不 release** 直接消失（`os._exit`
#: 跳过一切收尾代码）。job 句柄随进程消失而关闭，`KILL_ON_JOB_CLOSE` 是最后的兜底。
_OWNER_SCRIPT = """\
import os, subprocess, sys
from providers.process import SubprocessSeam
owned = SubprocessSeam().spawn([sys.executable, "-c", "import time; time.sleep(300)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(owned.pid, owned.poll() is None, flush=True)
os._exit(0)
"""

#: 拒绝温和档的剧本：先装好 SIGTERM 处置（忽略）再留下就绪标记，然后长睡——只有真升级到
#: 强杀才停得下来。
_STUBBORN_SCRIPT = """\
import signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text("ready")
time.sleep(300)
"""


def _leader_is_gone(range_: SubprocessRange, timeout_s: float = 20.0) -> bool:
    """组长是否已经退出（顺带收尸：POSIX 的僵尸要经 `poll` 才真的消失）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        range_.poll()                       # 收尸：在此之前 POSIX 的组长还是僵尸
        if not _alive(range_.pid):
            return True
        time.sleep(0.05)
    return False


def _kill_leader_only(pid: int) -> None:
    """只终止组长这**一个**进程（不碰后代）：用来证明下面的断言不是白捡的。

    Windows 上 `taskkill /T` 要靠**活着的**组长遍历进程树，组长已退出时它什么都够不着；
    POSIX 上进程组还在，但组长那个 pid 已经不存在了。
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _spawn_script(tmp_path: Path, script_text: str) -> tuple[SubprocessRange, int, TextIO]:
    """跑一段「组长 → 后代」剧本，返回 (受管范围, 后代 pid, 现场输出文件)。"""
    script, pidfile = tmp_path / "tree.py", tmp_path / "grandchild.pid"
    script.write_text(script_text, encoding="utf-8")
    out = (tmp_path / "tree.out").open("w", encoding="utf-8")
    range_ = SubprocessSeam().spawn([sys.executable, str(script), str(pidfile)], stdout=out)
    deadline = time.monotonic() + 20
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pidfile.exists(), f"后代迟迟没有起来，组长输出：{out.read_text(encoding='utf-8', errors='replace')}"
    return range_, int(pidfile.read_text(encoding="utf-8")), out


def test_terminate_kills_the_whole_range_and_repeats_safely(tmp_path):
    """终止以整棵进程树为单位：组长与后代都不再存在；重复终止是安全的 no-op。"""
    range_, grandchild, out = _spawn_script(tmp_path, _TREE_SCRIPT)
    try:
        # 先证明现场真有这棵树，否则「不存在」是白捡的
        assert _alive(range_.pid) and _alive(grandchild)
        assert range_.wait_for_exit(200) is None        # 两个都在长睡，范围没退

        range_.terminate(grace_ms=500)
        range_.terminate(grace_ms=500)                  # 幂等：重复终止不出错

        assert range_.poll() is not None                # 范围已退出并给出退出码
        _assert_gone(range_.pid, grandchild)
    finally:
        out.close()
        range_.release()


def test_release_waits_for_the_range_to_exit(tmp_path):
    """释放返回时受管范围已经退出（含后代），且释放与终止一样幂等。"""
    range_, grandchild, out = _spawn_script(tmp_path, _TREE_SCRIPT)
    try:
        range_.release()

        assert range_.poll() is not None
        _assert_gone(range_.pid, grandchild)
        range_.release()                                # 幂等：重复释放不出错
        range_.terminate()                              # 释放之后再终止也是 no-op
        assert range_.poll() is not None
    finally:
        out.close()


def test_terminate_reaches_the_descendant_after_the_leader_is_gone(tmp_path):
    """组长先退出、后代独活：范围没退——终止受管范围仍够得着后代。

    这是范围本位与「组长本位」的分界：组长本位会把「组长已退出」当成「范围已空」而
    早早 no-op，于是后代成为孤儿（Windows 上 `taskkill /T` 事后也救不回来）。
    """
    range_, grandchild, out = _spawn_script(tmp_path, _ORPHAN_SCRIPT)
    try:
        assert _leader_is_gone(range_), "组长没有按剧本退出"
        assert _alive(grandchild)                       # 现场：后代确实在长睡
        assert range_.poll() is None                    # 组长已死，范围没退

        # 空洞性证明：只杀组长这一个进程够不着后代，所以「后代不存在」不是白捡的
        _kill_leader_only(range_.pid)
        assert _alive(grandchild)

        range_.terminate(grace_ms=500)

        assert range_.poll() is not None
        _assert_gone(grandchild)
        range_.terminate(grace_ms=500)                  # 幂等：范围已空时是 no-op
    finally:
        out.close()
        range_.release()


def test_release_leaves_no_orphan_when_the_leader_exits_first(tmp_path):
    """释放返回时该范围保证已退出：组长先退出，后代也不留。"""
    range_, grandchild, out = _spawn_script(tmp_path, _ORPHAN_SCRIPT)
    try:
        assert _leader_is_gone(range_), "组长没有按剧本退出"
        assert _alive(grandchild)
        _kill_leader_only(range_.pid)                   # 只杀组长够不着它
        assert _alive(grandchild)

        range_.release()

        assert range_.poll() is not None
        _assert_gone(grandchild)
        range_.release()                                # 幂等：重复释放不出错
    finally:
        out.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows Job Object 一次强杀到底，没有「宽限 → 强杀」升级档")
def test_terminate_escalates_to_a_hard_kill_when_the_signal_is_ignored(tmp_path):
    """POSIX 升级档真的会被触发：范围忽略 SIGTERM 时长睡——`terminate(grace_ms=200)` 返回时
    它必须已经被强杀。

    这是「宽限 → 强杀」唯一的真实触发点：其余剧本的子进程一碰 SIGTERM 就死，升级档从不执行。
    若强杀那步被写坏（例如换成 `pass`），`terminate` 的收尾等待会等到 `_REAP_MS` 并把
    「范围仍在跑」报成 `TerminationError`，这条断言因此会红而不是静默通过。

    Windows 后端没有这一档（`TerminateJobObject` 一次到底，`grace_ms` 在那里只是等待上限），
    故本机跳过——这条断言只在 POSIX 上有意义。
    """
    script, marker = tmp_path / "stubborn.py", tmp_path / "ready"
    script.write_text(_STUBBORN_SCRIPT, encoding="utf-8")
    out = (tmp_path / "stubborn.out").open("w", encoding="utf-8")
    range_ = SubprocessSeam().spawn([sys.executable, str(script), str(marker)], stdout=out)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        # 就绪标记写在装好 SIG_IGN 之后：此刻起温和档真的杀不动它，「已被强杀」才不是白捡的
        assert marker.exists(), "子进程没来得及忽略 SIGTERM"
        assert _alive(range_.pid)

        started = time.monotonic()
        range_.terminate(grace_ms=200)                  # 温和档无效 → 必须升级到强杀
        elapsed = time.monotonic() - started

        assert range_.poll() is not None                # 返回即范围已退出
        _assert_gone(range_.pid)
        assert elapsed < 3, "升级档没有生效：等满了强杀之后的收尾上限"
    finally:
        out.close()
        range_.release()


@pytest.mark.skipif(os.name != "nt", reason="KILL_ON_JOB_CLOSE 是 Windows Job Object 的兜底")
def test_the_range_dies_with_the_process_that_owns_it():
    """拥有者进程消失（崩溃 / 被杀）时 job 句柄随之关闭：范围跟着死，不留孤儿。"""
    owner = subprocess.run([sys.executable, "-c", _OWNER_SCRIPT],
                           cwd=Path(__file__).resolve().parents[2],
                           capture_output=True, text=True, timeout=30)
    assert owner.returncode == 0, owner.stderr
    pid, alive = owner.stdout.split()

    assert alive == "True"                          # 拥有者退出前范围确实在跑，断言才不空洞
    _assert_gone(int(pid))


def test_wait_for_exit_reports_code_and_terminate_on_finished_range_is_noop():
    """正常退出的范围给出退出码；对已退出的范围再终止仍是 no-op，不改结局。"""
    range_ = SubprocessSeam().spawn([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert range_.wait_for_exit(timeout_ms=15_000) == 3
    assert range_.poll() == 3

    range_.terminate()

    assert range_.poll() == 3
    range_.release()
