"""Shell execution tool."""

import asyncio
import ctypes
import os
import re
from pathlib import Path
from typing import Any, Callable

from nanobot.agent.tools.base import Tool

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
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
        return None
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    if not kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(job)
        return None
    process_handle = process._transport.get_extra_info("subprocess")._handle
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    if not kernel32.AssignProcessToJobObject(job, int(process_handle)):
        kernel32.CloseHandle(job)
        return None
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
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=(os.name != "nt"),
                creationflags=(0x00000200 if os.name == "nt" else 0),
            )
            if os.name != "nt":
                process_group_id = process.pid
            else:
                job_handle[0] = _assign_to_kill_on_close_job(process)

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                await self._terminate_and_reap(process, process_group_id)
                return f"Error: Command timed out after {effective_timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Head + tail truncation to preserve both start and end of output
            max_len = self._MAX_OUTPUT
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            return result

        except asyncio.CancelledError:
            if process is not None:
                cleanup = asyncio.create_task(
                    self._terminate_and_reap(process, process_group_id, job_handle[0])
                )
                job_handle[0] = None  # cleanup owns and closes the handle
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        # A later cancel must not interrupt termination, wait/reap,
                        # or pipe draining. The original cancellation is re-raised
                        # after this independently-owned cleanup task completes.
                        continue
                await cleanup
            raise
        except Exception as e:
            return f"Error executing command: {str(e)}"
        finally:
            if job_handle[0] is not None:
                _close_windows_handle(job_handle[0])
                job_handle[0] = None

    @staticmethod
    async def _terminate_and_reap(
        process: asyncio.subprocess.Process,
        process_group_id: int | None = None,
        job_handle=None,
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

        # The shell can exit while a background descendant keeps inherited
        # stdout/stderr open. Drain with a grace period, then kill the saved
        # process group regardless of the shell's return code.
        try:
            await asyncio.wait_for(process.communicate(), timeout=1.0)
        except asyncio.TimeoutError:
            if os.name == "nt":
                await ExecTool._taskkill_tree(process.pid)
            else:
                import signal

                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.communicate(), timeout=1.0)
            except asyncio.TimeoutError:
                # A detached descendant may retain inherited pipes; forcibly
                # close our transports rather than wait for its lifetime.
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
