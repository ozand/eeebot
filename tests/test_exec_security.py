"""Tests for exec tool internal URL blocking."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.tools.shell import ExecTool


def _read_proc_stat(path: Path) -> list[str] | None:
    """Read process state; disappearance while reading means it was reaped."""
    try:
        return path.read_text(encoding="utf-8").split()
    except (FileNotFoundError, ProcessLookupError):
        return None


def test_proc_stat_read_tolerates_process_disappearing_after_exists(monkeypatch):
    """A process can be reaped between Path.exists() and reading /proc."""
    proc_stat = Path("/proc/12345/stat")
    monkeypatch.setattr(Path, "exists", lambda self: True)

    def _vanished_read_text(self, *args, **kwargs):
        raise ProcessLookupError("process exited after exists check")

    monkeypatch.setattr(Path, "read_text", _vanished_read_text)
    assert _read_proc_stat(proc_stat) is None


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
            stat_fields = None
            while time.monotonic() < deadline:
                stat_fields = _read_proc_stat(proc_stat)
                if stat_fields is None:
                    # The process may be reaped between cleanup and this
                    # observation; disappearance means it is no longer live.
                    break
                if len(stat_fields) > 2 and stat_fields[2] == "Z":
                    break
                await asyncio.sleep(0.01)
            if stat_fields is not None:
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
    child_pid = None
    while time.monotonic() < deadline:
        try:
            content = child_pid_file.read_text(encoding="utf-8").strip()
            child_pid = int(content) if content else None
        except (FileNotFoundError, ValueError):
            child_pid = None
        if child_pid is not None and shell_pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert child_pid is not None, "background grandchild did not write a valid PID before startup deadline"
    assert shell_pid_file.exists(), "background shell did not write its PID before startup deadline"
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

    async def _observe_cleanup(
        process, process_group_id=None, job_handle=None, *, drain_pipes=True
    ):
        cleanup_started.set()
        return await original_cleanup(
            process, process_group_id, job_handle, drain_pipes=drain_pipes
        )

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
        fields = None
        while time.monotonic() < deadline:
            fields = _read_proc_stat(proc_stat)
            if fields is None:
                # The process may be reaped between cleanup and observation.
                break
            if len(fields) > 2 and fields[2] == "Z":
                break
            await asyncio.sleep(0.01)
        if fields is not None:
            assert len(fields) > 2 and fields[2] == "Z", f"PID {pid} remains running"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows process-tree cleanup follow-up #2027")
@pytest.mark.asyncio
async def test_windows_job_setup_failure_does_not_run_command(tmp_path, monkeypatch):
    import os

    if os.name != "nt":
        pytest.skip("Windows Job Object failure-path test")
    marker = tmp_path / "must-not-exist.txt"
    def fail_job_assignment(process):
        assert process.returncode is None, "command process exited before ownership succeeded"
        raise OSError("injected Job Object failure")

    monkeypatch.setattr("nanobot.agent.tools.shell._assign_to_kill_on_close_job",
                        fail_job_assignment)
    result = await ExecTool(timeout=5).execute(f'echo ran > "{marker}"')
    assert result.startswith("Error executing command:")
    assert not marker.exists(), "untrusted command ran before Job Object ownership succeeded"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows process-tree cleanup follow-up #2027")
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
async def test_exec_streams_output_in_bounded_reads(monkeypatch):
    class _Stream:
        def __init__(self, chunks):
            self.chunks = iter(chunks)
            self.read_sizes = []
            self.eof_count = 0

        async def read(self, size=-1):
            self.read_sizes.append(size)
            chunk = next(self.chunks, b"")
            if not chunk:
                self.eof_count += 1
            return chunk

    class _Process:
        pid = 123
        returncode = 0
        _transport = None

        class _Stdin:
            def write(self, data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

        stdin = _Stdin()

        def __init__(self):
            self.stdout = _Stream([b"a" * 20_000, b"z\n"])
            self.stderr = _Stream([b"b" * 20_000, b"w\n"])

        async def wait(self):
            while self.stdout.eof_count < 1 or self.stderr.eof_count < 1:
                await asyncio.sleep(0)
            return self.returncode

        def kill(self):
            self.returncode = -9

        async def communicate(self):
            stdout = await self.stdout.read(-1)
            stderr = await self.stderr.read(-1)
            return stdout, stderr

    process = _Process()

    async def _create(*args, **kwargs):
        return process

    original_drain = ExecTool._drain_output_stream

    async def _observe_drain(stream, capture):
        await original_drain(stream, capture)

    monkeypatch.setattr(ExecTool, "_drain_output_stream", staticmethod(_observe_drain))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    monkeypatch.setattr("nanobot.agent.tools.shell._assign_to_kill_on_close_job", lambda process: None)

    result = await ExecTool(timeout=5).execute("echo test")

    assert result.startswith("a" * 5_000)
    assert "... (30," in result and " chars truncated) ..." in result
    assert result.endswith("Exit code: 0")
    assert process.stdout.read_sizes and all(size == 65_536 for size in process.stdout.read_sizes)
    assert process.stderr.read_sizes and all(size == 65_536 for size in process.stderr.read_sizes)


async def test_exec_bounded_head_tail_matches_legacy_output_format():
    from nanobot.agent.tools.shell import _BoundedOutput, _BoundedSegment

    output = _BoundedOutput(ExecTool._MAX_OUTPUT)
    stdout = "o" * 12_000
    stderr = "e" * 12_000
    stdout_segment = _BoundedSegment(ExecTool._MAX_OUTPUT)
    stdout_segment.add(stdout)
    output.add_segment(stdout_segment)
    output.add("\n")
    output.add("STDERR:\n")
    stderr_segment = _BoundedSegment(ExecTool._MAX_OUTPUT)
    stderr_segment.add(stderr)
    output.add_segment(stderr_segment)
    output.add("\n\nExit code: 0")

    legacy = "\n".join((stdout, f"STDERR:\n{stderr}", "\nExit code: 0"))
    half = ExecTool._MAX_OUTPUT // 2
    expected = (
        legacy[:half]
        + f"\n\n... ({len(legacy) - ExecTool._MAX_OUTPUT:,} chars truncated) ...\n\n"
        + legacy[-half:]
    )
    assert output.render() == expected

    # Multibyte UTF-8 split across byte chunks must decode as one character;
    # malformed bytes retain the legacy errors="replace" behavior.
    import codecs

    segment = _BoundedSegment(ExecTool._MAX_OUTPUT)
    capture = __import__("nanobot.agent.tools.shell", fromlist=["_BoundedTextCapture"])._BoundedTextCapture(segment)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    capture.add(decoder.decode(b"A\xe2"))
    capture.add(decoder.decode(b"\x82\xacB\xff"))
    capture.finish(decoder.decode(b"", final=True))
    assert segment.full() == "A€B�"


@pytest.mark.asyncio
async def test_exec_waited_process_with_open_pipe_times_out_and_cleans_up(monkeypatch):
    class _Stream:
        async def read(self, size):
            await asyncio.Event().wait()

    class _Process:
        pid = 123
        returncode = 0
        stdout = _Stream()
        stderr = _Stream()
        _transport = None

        class _Stdin:
            def write(self, data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

        stdin = _Stdin()

        async def wait(self):
            return self.returncode

    process = _Process()

    class _Transport:
        def close(self):
            pass

    process._transport = _Transport()
    terminated = asyncio.Event()

    async def _create(*args, **kwargs):
        return process

    reader_count = 0

    async def _blocking_reader(stream, capture):
        nonlocal reader_count
        reader_count += 1
        if reader_count == 1:
            raise asyncio.CancelledError
        await asyncio.Event().wait()

    async def _terminate(*args, **kwargs):
        terminated.set()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    monkeypatch.setattr(ExecTool, "_drain_output_stream", staticmethod(_blocking_reader))
    monkeypatch.setattr("nanobot.agent.tools.shell._assign_to_kill_on_close_job", lambda process: None)
    monkeypatch.setattr(ExecTool, "_terminate_and_reap", staticmethod(_terminate))

    result = await ExecTool(timeout=0.05).execute("fake command")
    assert result == "Error: Command timed out after 0.05 seconds"
    assert terminated.is_set()


@pytest.mark.asyncio
async def test_exec_reader_failure_after_root_exit_still_owns_cleanup(monkeypatch):
    class _Stream:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.reads = 0

        async def read(self, size):
            self.reads += 1
            if self.fail:
                raise OSError("injected pipe read error")
            return b""

    class _Process:
        pid = 123
        returncode = 0
        stdout = _Stream(fail=True)
        stderr = _Stream()

        class _Stdin:
            def write(self, data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

        stdin = _Stdin()

        async def wait(self):
            return self.returncode

        async def communicate(self):
            return b"", b""

        class _Transport:
            def close(self):
                pass

        _transport = _Transport()

    process = _Process()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    assert isinstance(process.stdout, _Stream)

    async def _create(*args, **kwargs):
        return process

    async def _cleanup(*args, **kwargs):
        cleanup_started.set()
        await allow_cleanup.wait()
        process.returncode = -9

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    monkeypatch.setattr("nanobot.agent.tools.shell._assign_to_kill_on_close_job", lambda process: None)
    monkeypatch.setattr(ExecTool, "_terminate_and_reap", staticmethod(_cleanup))

    execution = asyncio.create_task(ExecTool(timeout=5).execute("synthetic command"))
    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
    execution.cancel()
    await asyncio.sleep(0)
    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=3)


@pytest.mark.asyncio
async def test_exec_exit_separator_matches_legacy_output_segments():
    from nanobot.agent.tools.shell import _BoundedOutput, _BoundedSegment

    cases = (
        ("", "", 0),
        ("out", "", 0),
        ("", "err", 1),
        ("out", "err", 7),
        ("   ", "", 0),
        ("out", "   ", 0),
    )
    for stdout, stderr, code in cases:
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr and stderr.strip():
            parts.append("STDERR:\n" + stderr)
        parts.append(f"\nExit code: {code}")
        legacy = "\n".join(parts)

        output = _BoundedOutput(ExecTool._MAX_OUTPUT)
        if stdout:
            output.add_segment(_BoundedSegment(ExecTool._MAX_OUTPUT))
            output.segments[-1].add(stdout)
            output.total += len(stdout)
        if stderr and stderr.strip():
            if stdout:
                output.add("\n")
            output.add("STDERR:\n")
            segment = _BoundedSegment(ExecTool._MAX_OUTPUT)
            segment.add(stderr)
            output.add_segment(segment)
        if stdout or (stderr and stderr.strip()):
            output.add("\n")
        output.add("\nExit code: " + str(code))
        assert output.render() == legacy


@pytest.mark.asyncio
async def test_exec_empty_output_keeps_legacy_exit_format():
    from nanobot.agent.tools.shell import _BoundedOutput

    for code in (0, 7):
        output = _BoundedOutput(ExecTool._MAX_OUTPUT)
        output.add("\nExit code: " + str(code))
        legacy = "\n".join(("\nExit code: " + str(code),))
        assert output.render() == legacy


@pytest.mark.asyncio
async def test_exec_timeout_terminates_streaming_process():
    import sys

    command = (
        f'"{sys.executable}" -c '
        '"import sys,time; sys.stdout.write(\'started\'); sys.stdout.flush(); time.sleep(5)"'
    )
    result = await ExecTool(timeout=0.2).execute(command)
    assert result == "Error: Command timed out after 0.2 seconds"


@pytest.mark.asyncio
async def test_exec_repeated_cancellation_during_reader_handoff_cleans_process(monkeypatch):
    class _Stream:
        async def read(self, size):
            await asyncio.Event().wait()

    class _Process:
        pid = 123
        returncode = None
        stdout = _Stream()
        stderr = _Stream()

        class _Stdin:
            def write(self, data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

        stdin = _Stdin()

        async def wait(self):
            await asyncio.Event().wait()

        class _Transport:
            def close(self):
                pass

        _transport = _Transport()

    process = _Process()
    reader_started = asyncio.Event()
    handoff_started = asyncio.Event()
    release_handoff = asyncio.Event()
    terminated = asyncio.Event()
    async def _create(*args, **kwargs):
        return process

    async def blocked_reader(stream, capture):
        reader_started.set()
        await asyncio.Event().wait()

    original_stop = ExecTool._stop_readers

    async def paused_handoff(process_arg, tasks):
        handoff_started.set()
        await release_handoff.wait()
        return await original_stop(process_arg, tasks)

    async def terminate(*args, **kwargs):
        terminated.set()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _create)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    monkeypatch.setattr("nanobot.agent.tools.shell._assign_to_kill_on_close_job", lambda process: None)
    monkeypatch.setattr(ExecTool, "_drain_output_stream", staticmethod(blocked_reader))
    monkeypatch.setattr(ExecTool, "_stop_readers", staticmethod(paused_handoff))
    monkeypatch.setattr(ExecTool, "_terminate_and_reap", staticmethod(terminate))
    execution = asyncio.create_task(ExecTool(timeout=60).execute("fake command"))
    await asyncio.wait_for(reader_started.wait(), timeout=5)
    execution.cancel()
    await asyncio.wait_for(handoff_started.wait(), timeout=5)
    execution.cancel()
    release_handoff.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=5)
    assert terminated.is_set()


async def test_exec_large_stdout_and_stderr_are_drained_with_bounded_result():
    import sys

    command = (
        f'"{sys.executable}" -c '
        '"import sys; sys.stdout.write(\'o\'*50000); sys.stderr.write(\'e\'*50000)"'
    )
    result = await ExecTool(timeout=10).execute(command)

    assert len(result) <= ExecTool._MAX_OUTPUT + 100
    assert result.startswith("o" * (ExecTool._MAX_OUTPUT // 2))
    assert "truncated)" in result
    assert result.endswith("e" * (ExecTool._MAX_OUTPUT // 2 - len("\n\nExit code: 0")) + "\n\nExit code: 0")


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
