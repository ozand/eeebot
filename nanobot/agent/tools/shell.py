"""Shell execution tool."""

import asyncio
import codecs
import ctypes
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

from nanobot.agent.tools.base import Tool

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_STREAM_CHUNK_SIZE = 65_536
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _assign_to_kill_on_close_job(process: asyncio.subprocess.Process):
    """Assign a process to a private Windows Job Object; no-op elsewhere."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    if not kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    process_handle = process._transport.get_extra_info("subprocess")._handle
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    if not kernel32.AssignProcessToJobObject(job, int(process_handle)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def _close_windows_handle(handle) -> None:
    if os.name == "nt" and handle:
        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        close_handle(handle)


# Runtime-state location env vars that must never leak into child processes
# spawned by this tool. Incident #594: a qwen subagent ran `pytest` via exec,
# and the inherited STATE_DIR caused test cycles (run_self_evolving_cycle) to
# write fixture data into the LIVE durable state root instead of the test's
# tmp_path workspace. The coordinator itself never goes through this tool —
# it reads/writes state via direct in-process Python calls — so scrubbing
# here only affects subagent-spawned children, not the coordinator's own
# state access.
SCRUBBED_STATE_ENV_VARS = (
    "STATE_DIR",
    "NANOBOT_RUNTIME_STATE_ROOT",
    "NANOBOT_RUNTIME_STATE_SOURCE",
)


class _BoundedSegment:
    def __init__(self, limit: int):
        self.limit = limit
        self.half = limit // 2
        self.prefix = ""
        self.tail = ""
        self.total = 0

    def add(self, text: str) -> None:
        if not text:
            return
        self.total += len(text)
        if len(self.prefix) < self.limit:
            self.prefix += text[: self.limit - len(self.prefix)]
        self.tail = (self.tail + text)[-self.limit:]

    def full(self) -> str | None:
        if self.total <= self.limit:
            return self.prefix
        return None

    def head(self) -> str:
        return self.prefix[: self.half]

    def suffix(self) -> str:
        return self.tail[-self.half:]


class _BoundedOutput:
    def __init__(self, limit: int):
        self.limit = limit
        self.half = limit // 2
        self.segments: list[_BoundedSegment] = []
        self.total = 0

    def add(self, text: str) -> None:
        segment = _BoundedSegment(self.limit)
        segment.add(text)
        self.add_segment(segment)

    def add_segment(self, segment: _BoundedSegment) -> None:
        self.segments.append(segment)
        self.total += segment.total

    def render(self) -> str:
        if self.total <= self.limit:
            return "".join(segment.full() or "" for segment in self.segments)
        head = "".join(segment.head() for segment in self.segments)[: self.half]
        suffix_parts: list[str] = []
        remaining = self.half
        for segment in reversed(self.segments):
            piece = segment.suffix()[-remaining:]
            suffix_parts.append(piece)
            remaining -= len(piece)
            if remaining <= 0:
                break
        tail = "".join(reversed(suffix_parts))
        omitted = self.total - len(head) - len(tail)
        return f"{head}\n\n... ({omitted:,} chars truncated) ...\n\n{tail}"


class _BoundedTextCapture:
    def __init__(self, segment: _BoundedSegment):
        self.segment = segment
        self.had_text = False
        self.non_whitespace = False

    def add(self, text: str) -> None:
        self.had_text = self.had_text or bool(text)
        self.non_whitespace = self.non_whitespace or bool(text.strip())
        self.segment.add(text)

    def finish(self, final_text: str = "") -> None:
        self.add(final_text)


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        denied_paths: "set[Path] | None" = None,
        on_prevent_access: "Callable[[Path], None] | None" = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self.denied_paths = {p.resolve() for p in denied_paths} if denied_paths else set()
        self.on_prevent_access = on_prevent_access

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @staticmethod
    async def _drain_output_stream(stream, capture) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await stream.read(_STREAM_CHUNK_SIZE):
            capture.add(decoder.decode(chunk))
        capture.finish(decoder.decode(b"", final=True))

    @staticmethod
    async def _discard_output_stream(stream) -> None:
        while await stream.read(_STREAM_CHUNK_SIZE):
            pass

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600)."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None, **kwargs: Any,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)
        process: asyncio.subprocess.Process | None = None
        process_group_id: int | None = None
        job_handle = [None]

        env = os.environ.copy()
        for var in SCRUBBED_STATE_ENV_VARS:
            env.pop(var, None)
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            if os.name == "nt":
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c",
                    "import subprocess,sys; gate=sys.stdin.buffer.read(1); "
                    "sys.exit(subprocess.call(sys.argv[1], shell=True) if gate else 125)",
                    command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    creationflags=0,
                )
                job_handle[0] = _assign_to_kill_on_close_job(process)
                assert process.stdin is not None
                process.stdin.write(b"\0")
                await process.stdin.drain()
                process.stdin.close()
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                )
                process_group_id = process.pid

            output = _BoundedOutput(self._MAX_OUTPUT)
            stdout_segment = _BoundedSegment(self._MAX_OUTPUT)
            stderr_segment = _BoundedSegment(self._MAX_OUTPUT)
            stdout_capture = _BoundedTextCapture(stdout_segment)
            stderr_capture = _BoundedTextCapture(stderr_segment)
            stdout_task = asyncio.create_task(self._drain_output_stream(process.stdout, stdout_capture))
            stderr_task = asyncio.create_task(self._drain_output_stream(process.stderr, stderr_capture))
            drain_tasks = (stdout_task, stderr_task)
            wait_task = asyncio.create_task(process.wait())
            try:
                done, _ = await asyncio.wait(
                    (wait_task, *drain_tasks),
                    timeout=effective_timeout,
                    return_when=asyncio.ALL_COMPLETED,
                )
                if not done:
                    raise asyncio.TimeoutError
                errors = [
                    task.exception() for task in done
                    if task.done() and not task.cancelled()
                ]
                if any(error is not None for error in errors):
                    raise next(error for error in errors if error is not None)
                if not wait_task.done() or len(done) != 1 + len(drain_tasks):
                    raise asyncio.TimeoutError
                await asyncio.gather(*drain_tasks)
                if stdout_capture.had_text:
                    output.add_segment(stdout_segment)
                if stderr_capture.non_whitespace:
                    if stdout_capture.had_text:
                        output.add("\n")
                    output.add("STDERR:\n")
                    output.add_segment(stderr_segment)
            except asyncio.TimeoutError:
                cleanup = asyncio.create_task(
                    self._cancel_readers_and_terminate(
                        process, process_group_id, wait_task, drain_tasks, job_handle[0]
                    )
                )
                job_handle[0] = None
                cancellation = await self._await_cleanup_despite_cancellation(cleanup)
                if cancellation is not None:
                    raise cancellation
                return f"Error: Command timed out after {effective_timeout} seconds"

            if stdout_capture.had_text or stderr_capture.non_whitespace:
                output.add("\n")
            output.add("\nExit code: " + str(process.returncode))
            return output.render()

        except asyncio.CancelledError:
            if process is not None:
                cleanup = asyncio.create_task(
                    self._cancel_readers_and_terminate(
                        process,
                        process_group_id,
                        wait_task if "wait_task" in locals() else None,
                        drain_tasks if "drain_tasks" in locals() else (),
                        job_handle[0],
                        outer_cancellation=True,
                    )
                )
                job_handle[0] = None
                await self._await_cleanup_despite_cancellation(cleanup)
            raise
        except Exception as e:
            if process is not None:
                cleanup = asyncio.create_task(
                    self._cancel_readers_and_terminate(
                        process,
                        process_group_id,
                        wait_task if "wait_task" in locals() else None,
                        drain_tasks if "drain_tasks" in locals() else (),
                        job_handle[0],
                    )
                )
                job_handle[0] = None
                cancellation = await self._await_cleanup_despite_cancellation(cleanup)
                if cancellation is not None:
                    raise cancellation from e
            return f"Error executing command: {str(e)}"
        finally:
            if "drain_tasks" in locals():
                for task in drain_tasks:
                    if not task.done():
                        task.cancel()
            if job_handle[0] is not None:
                _close_windows_handle(job_handle[0])
                job_handle[0] = None

    @staticmethod
    async def _await_cleanup_despite_cancellation(
        cleanup: asyncio.Task,
    ) -> asyncio.CancelledError | None:
        cancellation = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancellation = error
        await cleanup
        return cancellation

    @staticmethod
    async def _cancel_readers_and_terminate(
        process: asyncio.subprocess.Process,
        process_group_id: int | None,
        wait_task: asyncio.Task | None,
        reader_tasks: tuple[asyncio.Task, ...],
        job_handle,
        outer_cancellation: bool = False,
    ) -> None:
        # This independently-owned task performs the entire handoff before
        # any await in execute can be interrupted by repeated cancellation.
        if wait_task is not None and not wait_task.done():
            wait_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.25)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                pass
        readers_stopped = await ExecTool._stop_readers(process, reader_tasks)
        await ExecTool._terminate_and_reap(
            process, process_group_id, job_handle, drain_pipes=readers_stopped
        )
        if outer_cancellation:
            raise asyncio.CancelledError

    @staticmethod
    async def _stop_readers(
        process: asyncio.subprocess.Process, tasks: tuple[asyncio.Task, ...] | list[asyncio.Task]
    ) -> bool:
        if not tasks:
            return True
        if all(task.done() for task in tasks):
            await asyncio.gather(*tasks, return_exceptions=True)
            return True
        for task in tasks:
            if not task.done():
                task.cancel()

        async def settle() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)

        settled = asyncio.create_task(settle())
        try:
            await asyncio.wait_for(asyncio.shield(settled), timeout=0.25)
        except asyncio.TimeoutError:
            process._transport.close()
            try:
                await asyncio.wait_for(asyncio.shield(settled), timeout=0.25)
            except asyncio.TimeoutError:
                return False
        return settled.done()

    @staticmethod
    async def _terminate_and_reap(
        process: asyncio.subprocess.Process,
        process_group_id: int | None = None,
        job_handle=None,
        drain_pipes: bool = True,
    ) -> None:
        """Terminate the command tree and reap the shell process."""
        if os.name != "nt" and process_group_id is None:
            process_group_id = process.pid
        if os.name == "nt":
            # Closing this invocation's job kills descendants even after the
            # root shell has exited. The job handle is transferred to cleanup.
            if job_handle is not None:
                _close_windows_handle(job_handle)
            # The shell may already have exited while descendants still hold
            # inherited pipes. Use its saved PID even when returncode is set;
            # when it is still alive, also try to terminate the full tree.
            await ExecTool._taskkill_tree(process.pid)
            if process.returncode is None:
                process.kill()
        else:
            import signal

            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                group_was_present = False
            else:
                group_was_present = True

            # The grace period applies to the group, not just the shell:
            # signaling is repeated even if the shell exits before SIGKILL.
            # Do not later signal a numeric PGID known to be absent: it could
            # have been recycled during the grace interval.
            if group_was_present:
                await asyncio.sleep(1.0)
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            if os.name != "nt":
                import signal

                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.returncode is None:
                process.kill()
            await process.wait()

        if not drain_pipes:
            process._transport.close()
            return

        # The shell can exit while a background descendant keeps inherited
        # stdout/stderr open. Drain with a grace period, then kill the saved
        # process group regardless of the shell's return code.
        drain_tasks = [
            asyncio.create_task(ExecTool._discard_output_stream(process.stdout)),
            asyncio.create_task(ExecTool._discard_output_stream(process.stderr)),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*drain_tasks), timeout=1.0)
        except asyncio.TimeoutError:
            readers_stopped = await ExecTool._stop_readers(process, drain_tasks)
            if os.name == "nt":
                await ExecTool._taskkill_tree(process.pid)
            else:
                import signal

                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if not readers_stopped:
                process._transport.close()
                return
            drain_tasks = [
                asyncio.create_task(ExecTool._discard_output_stream(process.stdout)),
                asyncio.create_task(ExecTool._discard_output_stream(process.stderr)),
            ]
            try:
                await asyncio.wait_for(asyncio.gather(*drain_tasks), timeout=1.0)
            except asyncio.TimeoutError:
                # A detached descendant may retain inherited pipes. Settle the
                # existing readers before closing transports; never overlap
                # reads on the same StreamReader.
                await ExecTool._stop_readers(process, drain_tasks)
                process._transport.close()

    @staticmethod
    async def _taskkill_tree(root_pid: int) -> None:
        """Best-effort terminate a Windows process tree by its saved root PID."""
        cleanup = await asyncio.create_subprocess_exec(
            "taskkill.exe", "/PID", str(root_pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            cleanup.kill()
            await cleanup.wait()

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        from nanobot.security.network import contains_internal_url
        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.denied_paths:
            normalized_cmd = cmd.replace("\\", "/")
            for p in self.denied_paths:
                p_fwd = p.as_posix()
                if p_fwd in normalized_cmd:
                    if self.on_prevent_access:
                        self.on_prevent_access(p)
                    return f"Error: Command blocked by safety guard (access to protected fitness sidecar: {p.name})"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)   # Windows: C:\...
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths
