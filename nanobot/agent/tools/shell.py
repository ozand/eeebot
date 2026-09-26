"""Shell execution tool."""

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable

from nanobot.agent.tools.base import Tool

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

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                await self._terminate_and_reap(process)
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
                await self._terminate_and_reap(process)
            raise
        except Exception as e:
            return f"Error executing command: {str(e)}"

    @staticmethod
    async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
        """Terminate the command tree and reap the shell process."""
        if process.returncode is not None:
            await process.wait()
            return

        if os.name == "nt":
            cleanup = await asyncio.create_subprocess_exec(
                "taskkill.exe", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()
        else:
            import signal

            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            if os.name != "nt":
                import signal

                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.returncode is None:
                process.kill()
            await process.wait()
        # The original communicate() coroutine is cancelled by wait_for on
        # timeout/cancellation; drain and close its pipe transports after exit.
        await process.communicate()

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
