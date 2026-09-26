"""Tests for exec tool internal URL blocking."""

from __future__ import annotations

import socket
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

    pid_file = tmp_path / "child.pid"
    if os.name == "nt":
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
        child_code = (
            "import os, sys, time; "
            "open(sys.argv[1], 'w').write(str(os.getpid())); "
            "time.sleep(60)"
        )
        child_command = f'{sys.executable} -c "{child_code}" "{pid_file}"'
    tool = ExecTool(timeout=60)
    execution = asyncio.create_task(tool.execute(child_command))
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert pid_file.exists(), "exec child did not start"
    pid = int(pid_file.read_text(encoding="utf-8"))

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=8)

    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        process = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
        assert process, f"child PID {pid} could not be inspected"
        try:
            assert kernel32.WaitForSingleObject(process, 0) == 0, f"child PID {pid} is still alive"
        finally:
            kernel32.CloseHandle(process)
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


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
