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
        pid_file = child_pid_file
        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        command_file = tmp_path / "spawn_child.ps1"
        command_file.write_text(
            "$python = $args[0]; $script = $args[1]; $pidfile = $args[2]; "
            "$p = Start-Process -FilePath $python -ArgumentList @($script, $pidfile) -PassThru; "
            "Wait-Process -Id $p.Id",
            encoding="utf-8",
        )
        child_command = (
            f'powershell -NoProfile -File "{command_file}" '
            f'"{sys.executable}" "{child_script}" "{pid_file}"'
        )
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
    while (
        (not child_pid_file.exists() or (os.name != "nt" and not shell_pid_file.exists()))
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(0.01)
    assert child_pid_file.exists(), "exec child did not start"
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    shell_pid = int(shell_pid_file.read_text(encoding="utf-8")) if shell_pid_file.exists() else None
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
    child_pid_posix = child_pid_file.as_posix()
    shell_pid_posix = shell_pid_file.as_posix()
    command = (
        f"python3 -c 'import os,sys; open(sys.argv[1], \"w\").write(str(os.getpid()))' "
        f'"{shell_pid_posix}"; '
        f"python3 -c 'import os,sys,time; open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(60)' "
        f'"{child_pid_posix}" &'
    )
    execution = asyncio.create_task(ExecTool(timeout=60).execute(command))
    deadline = time.monotonic() + 10
    while not child_pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert child_pid_file.exists(), "background grandchild did not start"
    shell_pid = int(shell_pid_file.read_text(encoding="utf-8"))
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert os.getsid(shell_pid) == shell_pid
    assert os.getpgid(child_pid) == shell_pid

    # Wait until the shell has exited; communicate remains blocked because the
    # grandchild inherited stdout/stderr. This is the regression window.
    deadline = time.monotonic() + 5
    while Path(f"/proc/{shell_pid}/stat").exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    shell_stat = Path(f"/proc/{shell_pid}/stat")
    assert not shell_stat.exists() or shell_stat.read_text(encoding="utf-8").split()[2] == "Z"
    assert execution.done() is False

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=8)

    child_stat = Path(f"/proc/{child_pid}/stat")
    deadline = time.monotonic() + 5
    while child_stat.exists() and time.monotonic() < deadline:
        fields = child_stat.read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            break
        await asyncio.sleep(0.01)
    if child_stat.exists():
        fields = child_stat.read_text(encoding="utf-8").split()
        assert len(fields) > 2 and fields[2] == "Z", f"grandchild PID {child_pid} survived cancellation"


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
