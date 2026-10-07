"""Tests for exec tool internal URL blocking."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.tools.shell import ExecTool


@pytest.mark.asyncio
async def test_watchdog_cancellation_kills_and_reaps_real_exec_subprocess(tmp_path):
    """Cancelling ExecTool.execute must not leave its real child process alive."""
    import asyncio
    import os
    import sys
    import time

    child_pid_file = tmp_path / "child.pid"
    shell_pid_file = tmp_path / "shell.pid"
    if os.name == "nt":
        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        child_command = f'"{sys.executable}" "{child_script}" "{child_pid_file}"'
    else:
        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        child_command = (
            f'echo $$ > "{shell_pid_file}"; '
            f'"{sys.executable}" "{child_script}" "{child_pid_file}"'
        )
    tool = ExecTool(timeout=60)
    execution = asyncio.create_task(tool.execute(child_command))
    deadline = time.monotonic() + 10
    child_pid = None
    shell_pid = None
    while time.monotonic() < deadline:
        # File existence is not a completion signal: open(..., 'w') creates
        # the file before the child has written its PID. Retry empty/partial
        # contents within the same startup deadline before parsing.
        try:
            content = child_pid_file.read_text(encoding="utf-8").strip()
            child_pid = int(content) if content else None
        except (FileNotFoundError, ValueError):
            child_pid = None
        if os.name != "nt":
            try:
                content = shell_pid_file.read_text(encoding="utf-8").strip()
                shell_pid = int(content) if content else None
            except (FileNotFoundError, ValueError):
                shell_pid = None
        if child_pid is not None and (os.name == "nt" or shell_pid is not None):
            break
        await asyncio.sleep(0.01)
    assert child_pid is not None, "exec child did not write a valid PID before startup deadline"
    assert os.name == "nt" or shell_pid is not None, "exec shell did not write a valid PID before startup deadline"
    if os.name != "nt":
        import os as posix_os

        assert shell_pid is not None
        assert posix_os.getsid(shell_pid) == shell_pid
        assert posix_os.getpgid(child_pid) == shell_pid, "grandchild did not inherit ExecTool's process group"

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=8)

    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        process = kernel32.OpenProcess(0x00100000 | 0x1000, False, child_pid)
        if process:
            try:
                assert kernel32.WaitForSingleObject(process, 0) == 0, f"child PID {child_pid} is still alive"
            finally:
                kernel32.CloseHandle(process)
    else:
        for pid in (shell_pid, child_pid):
            assert pid is not None
            proc_stat = Path(f"/proc/{pid}/stat")
            deadline = time.monotonic() + 5
            while proc_stat.exists() and time.monotonic() < deadline:
                stat_fields = proc_stat.read_text(encoding="utf-8").split()
                if len(stat_fields) > 2 and stat_fields[2] == "Z":
                    break
                await asyncio.sleep(0.01)
            if proc_stat.exists():
                stat_fields = proc_stat.read_text(encoding="utf-8").split()
                assert len(stat_fields) > 2 and stat_fields[2] == "Z", (
                    f"PID {pid} remains running (state={stat_fields[2] if len(stat_fields) > 2 else 'unknown'})"
                )


@pytest.mark.asyncio
async def test_watchdog_cancellation_kills_grandchild_after_shell_exits(tmp_path):
    """A shell exit must not bypass process-group cleanup for a pipe-holding child."""
    import asyncio
    import os
    import sys
    import time

    if os.name == "nt":
        pytest.skip("POSIX process-group semantics")
    if sys.platform != "linux":
        pytest.skip("/proc process-state inspection requires Linux")

    child_pid_file = tmp_path / "background-child.pid"
    shell_pid_file = tmp_path / "background-shell.pid"
    shell_exit_file = tmp_path / "shell-exited"
    child_script = tmp_path / "background_child.py"
    child_script.write_text(
        "import os, signal, sys, time\n"
        "child_pid, shell_pid, exit_marker = sys.argv[1:]\n"
        "open(child_pid, 'w').write(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "parent_pid = int(open(shell_pid).read())\n"
        "while os.getppid() == parent_pid: time.sleep(0.01)\n"
        "open(exit_marker, 'w').write('shell exited')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    command = (
        f'echo $$ > "{shell_pid_file.as_posix()}"; '
        f'python3 "{child_script.as_posix()}" "{child_pid_file.as_posix()}" '
        f'"{shell_pid_file.as_posix()}" "{shell_exit_file.as_posix()}" & exit 0'
    )
    execution = asyncio.create_task(ExecTool(timeout=60).execute(command))
    deadline = time.monotonic() + 10
    while not child_pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert child_pid_file.exists(), "background grandchild did not start"
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    shell_pid_file.read_text(encoding="utf-8")
    child_stat = Path(f"/proc/{child_pid}/stat")

    # The background child confirms its parent shell exited while the child
    # still holds the captured pipes open, leaving communicate() pending.
    deadline = time.monotonic() + 5
    while not shell_exit_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert shell_exit_file.exists(), "shell did not exit while descendant remained"
    assert execution.done() is False

    child_fields = child_stat.read_text(encoding="utf-8").split()
    child_pgrp = int(child_fields[4])
    child_session = int(child_fields[5])
    child_start_time = child_fields[21]
    assert child_pgrp == child_session, "grandchild process group differs from its session"
    assert child_pgrp != os.getpgid(os.getpid()), "grandchild accidentally joined pytest's process group"

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=8)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            fields = child_stat.read_text(encoding="utf-8").split()
        except (FileNotFoundError, ProcessLookupError):
            break
        if len(fields) > 2 and fields[2] == "Z":
            break
        await asyncio.sleep(0.01)
    if child_stat.exists():
        try:
            fields = child_stat.read_text(encoding="utf-8").split()
        except (FileNotFoundError, ProcessLookupError):
            fields = []
        assert not fields or fields[2] == "Z", f"grandchild PID {child_pid} survived cancellation"
        assert not fields or fields[21] != child_start_time, f"grandchild PID {child_pid} still exists"


@pytest.mark.asyncio
async def test_watchdog_repeated_cancellation_finishes_real_process_cleanup(tmp_path, monkeypatch):
    """A second cancellation must not interrupt cleanup of a live process tree."""
    import asyncio
    import os
    import sys
    import time

    if os.name == "nt":
        pytest.skip("POSIX process-group semantics")
    if sys.platform != "linux":
        pytest.skip("/proc process-state inspection requires Linux")

    child_pid_file = tmp_path / "child.pid"
    shell_pid_file = tmp_path / "shell.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text("\n".join((
        "import os, signal, sys, time",
        "open(sys.argv[1], 'w').write(str(os.getpid()))",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        "time.sleep(60)",
        "",
    )), encoding="utf-8")
    command = f'echo $$ > "{shell_pid_file}"; python3 "{child_script}" "{child_pid_file}"'
    cleanup_started = asyncio.Event()
    original_cleanup = ExecTool._terminate_and_reap

    async def _observe_cleanup(process, process_group_id=None):
        cleanup_started.set()
        return await original_cleanup(process, process_group_id)

    monkeypatch.setattr(ExecTool, "_terminate_and_reap", staticmethod(_observe_cleanup))
    execution = asyncio.create_task(ExecTool(timeout=60).execute(command))
    deadline = time.monotonic() + 10
    while (not child_pid_file.exists() or not shell_pid_file.exists()) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert child_pid_file.exists() and shell_pid_file.exists(), "real shell/child did not start"
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    shell_pid = int(shell_pid_file.read_text(encoding="utf-8"))
    execution.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=5)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=8)

    for pid in (shell_pid, child_pid):
        proc_stat = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 5
        while proc_stat.exists() and time.monotonic() < deadline:
            fields = proc_stat.read_text(encoding="utf-8").split()
            if len(fields) > 2 and fields[2] == "Z":
                break
            await asyncio.sleep(0.01)
        if proc_stat.exists():
            fields = proc_stat.read_text(encoding="utf-8").split()
            assert len(fields) > 2 and fields[2] == "Z", f"PID {pid} remains running"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows process-tree cleanup follow-up #2027")
@pytest.mark.xfail(reason="Windows Job Object ownership follow-up #2027", strict=True)
@pytest.mark.asyncio
async def test_watchdog_cancellation_kills_windows_descendant_after_shell_exits(tmp_path, monkeypatch):
    """Use a saved shell PID to verify tree cleanup after the shell exits."""
    import asyncio
    import ctypes
    import sys
    import time

    child_pid_file = tmp_path / "windows-child.pid"
    child_script = tmp_path / "windows_child.py"
    child_script.write_text("\n".join((
        "import os, sys, time",
        "open(sys.argv[1], 'w').write(str(os.getpid()))",
        "time.sleep(60)",
        "",
    )), encoding="utf-8")
    command = f'"{sys.executable}" "{child_script}" "{child_pid_file}"'
    execution = asyncio.create_task(ExecTool(timeout=60).execute(command))
    deadline = time.monotonic() + 10
    while not child_pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert child_pid_file.exists(), "Windows descendant did not start"
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    shell_process = execution.get_coro().cr_frame.f_locals["process"]
    assert shell_process is not None
    # Make execute's shell complete while the separate child still holds its
    # inherited stdout/stderr pipe handles; cleanup must use the saved PID.
    killer = await asyncio.create_subprocess_exec(
        "taskkill.exe", "/PID", str(shell_process.pid), "/F",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await killer.wait()
    for _ in range(100):
        if shell_process.returncode is not None:
            break
        await asyncio.sleep(0.01)
    assert shell_process.returncode is not None, "test did not terminate the parent shell"
    await asyncio.sleep(0.1)
    assert execution.done() is False, "descendant should retain inherited output pipes"
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=10)

    kernel32 = ctypes.windll.kernel32
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        process = kernel32.OpenProcess(0x00100000 | 0x1000, False, child_pid)
        if not process:
            break
        try:
            if kernel32.WaitForSingleObject(process, 0) == 0:
                break
        finally:
            kernel32.CloseHandle(process)
        await asyncio.sleep(0.02)
    else:
        pytest.fail(f"Windows descendant PID {child_pid} survived process-tree cleanup")


def _fake_resolve_private(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]


def _fake_resolve_localhost(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]


def _fake_resolve_public(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


@pytest.mark.asyncio
async def test_exec_blocks_curl_metadata():
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        result = await tool.execute(
            command='curl -s -H "Metadata-Flavor: Google" http://169.254.169.254/computeMetadata/v1/'
        )
    assert "Error" in result
    assert "internal" in result.lower() or "private" in result.lower()


@pytest.mark.asyncio
async def test_exec_blocks_wget_localhost():
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_localhost):
        result = await tool.execute(command="wget http://localhost:8080/secret -O /tmp/out")
    assert "Error" in result


@pytest.mark.asyncio
async def test_exec_allows_normal_commands():
    tool = ExecTool(timeout=5)
    result = await tool.execute(command="echo hello")
    assert "hello" in result
    assert "Error" not in result.split("\n")[0]


@pytest.mark.asyncio
async def test_exec_allows_curl_to_public_url():
    """Commands with public URLs should not be blocked by the internal URL check."""
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        guard_result = tool._guard_command("curl https://example.com/api", "/tmp")
    assert guard_result is None


@pytest.mark.asyncio
async def test_exec_blocks_chained_internal_url():
    """Internal URLs buried in chained commands should still be caught."""
    tool = ExecTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        result = await tool.execute(
            command="echo start && curl http://169.254.169.254/latest/meta-data/ && echo done"
        )
    assert "Error" in result


@pytest.mark.asyncio
async def test_exec_scrubs_runtime_state_env_vars(monkeypatch):
    """Regression for #594: STATE_DIR/NANOBOT_RUNTIME_STATE_ROOT/SOURCE must
    never leak into exec children, so a subagent running e.g. `pytest` inside
    a self-evolving-cycle test can't have its test writes redirected into the
    live durable state root. An unrelated env var must still be inherited."""
    monkeypatch.setenv("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state")
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", "/var/lib/eeepc-agent/self-evolving-agent/state")
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_SOURCE", "live")
    monkeypatch.setenv("EXEC_TEST_MARKER", "present")

    tool = ExecTool(timeout=5)
    result = await tool.execute(command="env")

    assert "STATE_DIR=" not in result
    assert "NANOBOT_RUNTIME_STATE_ROOT=" not in result
    assert "NANOBOT_RUNTIME_STATE_SOURCE=" not in result
    assert "EXEC_TEST_MARKER=present" in result
