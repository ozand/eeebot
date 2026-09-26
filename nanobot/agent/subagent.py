"""Subagent manager for background task execution."""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.memory_search import MemorySearchTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.toolsets import EXECUTOR_TOOL_NAMES as EXECUTOR_TOOLSET
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ExecToolConfig
from nanobot.providers.base import LLMProvider
from nanobot.runtime import context_compaction as _ctx_compact
from nanobot.utils.helpers import build_assistant_message

#: #1767 -- longest requested path kept verbatim in a failure row. A
#: pathological request must not be able to inflate the bounded scan file.
_MAX_ATTEMPTED_NAME_CHARS = 120


def _attempted_skill_name(requested: str) -> str:
    """The skill name a failed ``SKILL.md`` read was asking for.

    A request that looks like ``skills/<name>/SKILL.md`` (with either
    separator, optionally prefixed) yields ``<name>`` -- the same vocabulary
    the success path records, so the two halves of the question can be
    compared without a join. Anything else is kept as the requested string,
    because a request that does not follow the convention is exactly the
    kind of mistake worth reading verbatim.
    """
    norm = str(requested).replace("\\", "/").strip().rstrip("/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if len(parts) >= 3 and parts[-1] == "SKILL.md" and parts[-3] == "skills":
        return parts[-2][:_MAX_ATTEMPTED_NAME_CHARS]
    return norm[:_MAX_ATTEMPTED_NAME_CHARS]


# ── #1101: identical-tool-call loop breaker ───────────────────────────────────
# Number of consecutive identical (tool_name, canonical_args) calls before the
# warning message is injected.  2×K triggers abort.  Env-tunable; default 3.
_LOOP_BREAKER_K_DEFAULT = 3

# ── #1774: truncated-response continuation ───────────────────────────────────
#: How many consecutive truncated responses before the cycle stops with an
#: explicit reason.  Three, because the measured behaviour is a single long
#: reply that the ceiling cuts once -- 23 truncations over 9,714 executor calls,
#: none of them consecutive -- so two continuations is already well past the
#: observed need, and a fourth would mean the model is producing an unbounded
#: answer that a fifth attempt will not finish either.  The bound exists to stop
#: a spin, not to fund retries.
_MAX_CONSECUTIVE_TRUNCATIONS = 3

#: The turn that asks for the rest.  Written as an instruction about the work,
#: not about the plumbing: the executor has no way to act on "the ceiling" and
#: telling it about a limit it cannot control invites it to editorialise about
#: the limit instead of finishing.
_TRUNCATION_CONTINUATION_NOTE = (
    "Your previous message was cut off before it finished. Continue from exactly "
    "where it stopped -- do not restart it and do not repeat what you already "
    "wrote. If you were issuing a tool call, issue it now as your whole reply."
)


def _loop_breaker_k() -> int:
    """Return K from NANOBOT_LOOP_BREAKER_K env (positive int, default 3)."""
    raw = os.environ.get("NANOBOT_LOOP_BREAKER_K", "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except (ValueError, TypeError):
        pass
    return _LOOP_BREAKER_K_DEFAULT


def _canonical_tool_key(name: str, arguments: Any) -> str:
    """Stable string key for a tool call ignoring call ID.

    Sorts dict keys for canonical ordering; falls back gracefully for
    non-dict arguments so unusual invocations never crash the guard.
    """
    try:
        if isinstance(arguments, dict):
            return json.dumps({"n": name, "a": arguments}, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return json.dumps({"n": name, "a": str(arguments)}, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    except Exception:
        return f"{name}:?"


def _canonical_response_key(tool_calls: "list[Any]") -> str:
    """Stable string key for an entire response's tool-call tuple.

    Compares the ordered sequence of (name, canonical_args) across all calls
    in the response, ignoring call IDs.  A multi-tool response [A, B] repeated
    twice increments the counter; [A, B, A, B] alternating resets it.
    """
    parts = [_canonical_tool_key(tc.name, tc.arguments) for tc in tool_calls]
    try:
        return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(parts)


def _subagent_wall_deadline(
    _now: "float | None" = None,
    _monotonic: "Any | None" = None,
) -> "float | None":
    """Return monotonic deadline for the subagent wall-clock guard.

    Reads NANOBOT_SUBAGENT_WALL_SECS (positive float, default 3000 seconds
    = 50 minutes, safely below TimeoutStartSec=55min).  Returns None when
    env is unset/invalid so the guard is skipped.  Injectable clock for tests.
    """
    raw = os.environ.get("NANOBOT_SUBAGENT_WALL_SECS", "").strip()
    if not raw:
        # Default: 3000 s (50 min) soft deadline, active by default
        secs: float = 3000.0
    else:
        try:
            secs = float(raw)
            if secs <= 0:
                return None
        except (ValueError, TypeError):
            return None
    clock = _monotonic or time.monotonic
    start = _now if _now is not None else clock()
    return start + secs


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        web_search_config: Any | None = None,
        web_proxy: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        subagent_config: Any | None = None,
        restrict_to_workspace: bool = False,
        max_running: int | None = None,
        max_iterations: int | None = None,
        system_context: str = "",
        # #939 Part C: optional skill-fitness instrumentation context.
        # When supplied, successful SKILL.md reads inside the spawn window
        # are collected in memory and persisted by the bridge after the
        # subagent finishes (outside the spawn boundary, so the write is
        # harness-side and protected by the sidecar integrity check).
        skill_fitness_state_dir: "Path | None" = None,
        skill_fitness_repo: "Path | None" = None,
        skill_fitness_cycle_id: str = "",
        skill_fitness_cycle_base_sha: str = "",
        # Optional: names to exclude from the loop skills summary (Part E).
        excluded_skill_names: "list[str] | None" = None,
        telemetry_component: str = "",
        web_tools_enabled: bool = False,
        denied_paths: "set[Path] | None" = None,
        release_root: "Path | None" = None,
        role_system_prompt: "str | None" = None,
        # ADR-035 keep-work (#1942 B2): commit the workspace's dirty tree to
        # the current branch after every completed tool-execution step, so a
        # kill between steps loses only the in-flight one, not every
        # checkpoint before it. Opt-in (default False, matching every other
        # instrumentation param here) -- only the eeebot executor spawn site
        # passes True; the planner spawn and every other caller are
        # unaffected.
        checkpoint_commits: bool = False,
        # D3 (ADR-035 Test Contract, external review finding #3): the exact
        # branch the bridge resolved for THIS spawn (via
        # `_setup_cycle_branch`). `_maybe_checkpoint_commit` binds to this
        # value with an exact comparison, never a `selfevo/cycle-` prefix
        # test -- a workspace checked out on a DIFFERENT cycle's branch
        # (stray checkout, race) must never receive this executor's
        # checkpoint. Required (fail-closed) whenever `checkpoint_commits`
        # is True; `None` skips checkpointing entirely rather than guessing.
        expected_cycle_branch: str | None = None,
        wall_deadline: float | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig, WebSearchConfig

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self._wall_deadline = wall_deadline
        self.last_max_call_gap_s: float | None = None
        self.subagent_config = subagent_config
        configured_max_running = getattr(subagent_config, "max_running", None)
        self.max_running = int(max_running or configured_max_running or 1)
        # Issue #578: previously hardcoded to 15 inside _run_subagent's loop, decoupled
        # from agents.defaults.maxToolIterations (the main agent's own cap). Callers
        # should now pass the same value through so subagents aren't cut off earlier
        # than the coordinator's budget allows.
        self.max_iterations = int(max_iterations) if max_iterations else 15
        self.restrict_to_workspace = restrict_to_workspace
        self.denied_paths = {p.resolve() for p in denied_paths} if denied_paths else set()
        self.prevented_access_attempts: list[str] = []
        self.system_context = system_context.strip()
        #: #1725 (ADR-022): passed straight to ContextBuilder so the loop
        #: profile can load IDENTITY.md/SOUL.md/goals.md/USER.md/
        #: OPERATING.md through the normal prompt-fit cap and telemetry,
        #: replacing the old post-fit ``system_context`` charter+identity
        #: tail for the self-evolving executor. ``None`` for every other
        #: caller — those blocks render ``[missing: <name>]``, same as a
        #: genuinely absent file.
        self.release_root = release_root
        #: #1852 (ADR-031 rule 5): a caller that already holds a complete,
        #: fixed system prompt (a role file assembled by
        #: ``nanobot.runtime.role_prompt.build_role_system_prompt``) passes
        #: it here to bypass the ContextBuilder/ADR-022 ontology path
        #: entirely -- OPERATING.md's cycle contract (commit, verify, gate)
        #: does not apply to a session that only reads and plans. ``None``
        #: (every caller before this) leaves :meth:`_build_subagent_prompt`
        #: unchanged.
        self._role_system_prompt = role_system_prompt.strip() if role_system_prompt else None
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        from nanobot.runtime.state import resolve_runtime_state_location

        self._state_root, self._runtime_state_source = resolve_runtime_state_location(self.workspace)
        self._telemetry_dir = self._state_root / "subagents"
        # #939 Part C: skill-fitness instrumentation
        self._skill_fitness_state_dir = skill_fitness_state_dir
        self._skill_fitness_repo = skill_fitness_repo
        self._skill_fitness_cycle_id = skill_fitness_cycle_id
        self._skill_fitness_cycle_base_sha = skill_fitness_cycle_base_sha
        # Collected in memory during the spawn window; written by the bridge
        # after the subagent finishes (harness-side, protected by sidecar guard).
        self._skill_reads_this_cycle: list[dict] = []
        # Planner-only evidence is held in memory until the integrity bracket
        # closes; writing the fitness sidecar during planner spawn is a tamper.
        self._planner_release_skill_reads: list[str] = []
        #: #1767 -- names the executor asked for and did not get this cycle.
        self._skill_read_failures_this_cycle: list[str] = []
        #: ADR-028 rule 5 (#1812) -- every day-file read this cycle, any
        #: day, each tagged with the tool-call position (the loop's own
        #: ``iteration`` counter) it happened at. Collected (and cleared)
        #: by :meth:`collect_day_file_reads` after the spawn window closes;
        #: the bridge, not this class, turns it into a persisted row --
        #: ADR-028 rule 4 keeps this file free of the module name that
        #: would do that persisting.
        self._day_file_reads_this_cycle: list[dict] = []
        # #939 Part E: excluded skill names for the loop summary
        self._excluded_skill_names: list[str] = list(excluded_skill_names or [])
        self._telemetry_component = str(telemetry_component or "").strip()
        self.web_tools_enabled = bool(web_tools_enabled)
        self._checkpoint_commits = bool(checkpoint_commits)
        self._expected_cycle_branch = (expected_cycle_branch or "").strip() or None

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        **runtime_options: Any,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}
        if runtime_options:
            origin["runtime_options"] = {key: value for key, value in runtime_options.items() if value is not None}
        correlation_context = self._build_subagent_correlation_context()
        self._write_subagent_telemetry(
            task_id,
            self._build_subagent_telemetry_payload(
                task_id=task_id,
                task=task,
                label=display_label,
                started_at=self._utc_now(),
                finished_at=None,
                status="running",
                summary=None,
                result=None,
                origin=origin,
                session_key=session_key,
                correlation_context=correlation_context,
            ),
        )

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                session_key=session_key,
                correlation_context=correlation_context,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        session_key: str | None = None,
        correlation_context: dict[str, Any] | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        correlation_context = correlation_context or self._build_subagent_correlation_context()
        context_usage = {"peak_tokens": 0, "iterations": []}

        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry()
            allowed_dir = self.workspace if self.restrict_to_workspace else None
            # ADR-033: package skills include release-owned operator instructions.
            extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
            # #939 Part C: wire SKILL.md read instrumentation callback.
            _on_skill_read = None
            _on_complete_skill_read = None
            # #1767: the failure mirror. A SKILL.md read the executor asked
            # for and did not get is recorded as the requested spelling --
            # that string is the whole evidence, since a failed lookup
            # resolves to nothing.
            _on_skill_read_failed = None
            if self._skill_fitness_state_dir is not None or self._role_system_prompt is not None:
                workspace_skills = (self.workspace / "skills").resolve()
                release_base = self.release_root or BUILTIN_SKILLS_DIR.parent.parent
                release_skills = (Path(release_base) / "nanobot" / "skills").resolve()

                def _skill_relpath(skill_path: Path) -> tuple[Path, str] | None:
                    try:
                        rel = skill_path.relative_to(workspace_skills)
                        return rel, f"skills/{rel.as_posix()}"
                    except ValueError:
                        try:
                            rel = skill_path.relative_to(release_skills)
                            return rel, f"nanobot/skills/{rel.as_posix()}"
                        except ValueError:
                            return None

                def _on_skill_read(skill_path: Path) -> None:  # noqa: E301
                    resolved = _skill_relpath(skill_path)
                    if resolved is None:
                        return
                    rel, path = resolved
                    if len(rel.parts) == 2 and rel.parts[1] == "SKILL.md":
                        # #1857: `iteration` (this method's own loop counter,
                        # read at call time) is the tool-call position the
                        # obligation needs to be checkable against -- same
                        # pattern as `_on_day_file_read` below.
                        self._skill_reads_this_cycle.append(
                            {"skill": rel.parts[0], "path": path, "position": iteration}
                        )

                def _on_complete_skill_read(skill_path: Path) -> None:  # noqa: E301
                    resolved = _skill_relpath(skill_path)
                    if resolved is None:
                        return
                    rel, path = resolved
                    if len(rel.parts) == 2 and path == "nanobot/skills/task-writing/SKILL.md":
                        self._planner_release_skill_reads.append(path)

                def _on_skill_read_failed(requested: str) -> None:  # noqa: E301
                    self._skill_read_failures_this_cycle.append(
                        _attempted_skill_name(requested)
                    )
            # ADR-028 rule 5 (#1812): wire the day-file read instrumentation
            # the same way. `iteration` is this method's own loop counter
            # (defined below, before the while loop runs) -- the closure
            # reads its CURRENT value at call time, which is exactly the
            # tool-call position the obligation needs to be checkable
            # against, not merely asserted. The path check (is this under
            # the day-file directory, which day) already happened inside
            # ReadFileTool, which owns that check -- this method only
            # receives the day string a real read resolved to. Deliberately
            # kept nameless of the module's own vocabulary here: ADR-028
            # rule 4 forbids that word from this file's source altogether.
            _on_day_file_read = None
            if self._skill_fitness_state_dir is not None:

                def _on_day_file_read(day: str) -> None:  # noqa: E301
                    self._day_file_reads_this_cycle.append({"day": day, "position": iteration})
            tools.register(ReadFileTool(
                workspace=self.workspace,
                allowed_dir=allowed_dir,
                extra_allowed_dirs=extra_read,
                on_skill_read=_on_skill_read,
                on_complete_skill_read=_on_complete_skill_read,
                on_skill_read_failed=_on_skill_read_failed,
                on_day_file_read=_on_day_file_read,
            ))
            def _record_prevented_access(p: Path) -> None:
                self.prevented_access_attempts.append(str(p))

            tools.register(WriteFileTool(
                workspace=self.workspace,
                allowed_dir=allowed_dir,
                denied_paths=self.denied_paths,
                on_prevent_write=_record_prevented_access,
            ))
            tools.register(EditFileTool(
                workspace=self.workspace,
                allowed_dir=allowed_dir,
                denied_paths=self.denied_paths,
                on_prevent_write=_record_prevented_access,
            ))
            tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
                denied_paths=self.denied_paths,
                on_prevent_access=_record_prevented_access,
            ))
            tools.register(MemorySearchTool(
                workspace=self.workspace,
                state_dir=self._state_root,
            ))
            if self.web_tools_enabled:
                from nanobot.agent.tools.web import WebFetchTool, WebSearchTool

                tools.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
                tools.register(WebFetchTool(proxy=self.web_proxy))

            assert tuple(tools.tool_names) == self.registered_tool_names(), (
                "registered tool set must match the configured declaration"
            )
            system_prompt = self._build_subagent_prompt()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # Run agent loop (limited iterations)
            max_iterations = self.max_iterations
            iteration = 0
            final_result: str | None = None
            stop_reason: str | None = None
            memory_unavailable_reason: str = ""
            # #1101: loop-breaker state
            _lbk = _loop_breaker_k()
            _lb_last_key: str | None = None
            _lb_count: int = 0
            # #1774: truncation-continuation state. `count` is every truncated
            # response this cycle, `consecutive` only the current run, and
            # `continued` records that at least one continuation was followed by
            # a response that was not truncated -- i.e. the recovery actually
            # worked, which is the number that makes this change measurable.
            _truncation = {"count": 0, "consecutive": 0, "continued": False}
            # #1101/#1899: wall-clock soft deadline and progress watchdog
            _clock = getattr(self, '_monotonic', None) or time.monotonic
            _wall_deadline = (
                self._wall_deadline
                if self._wall_deadline is not None
                else _subagent_wall_deadline(_monotonic=_clock)
            )
            from nanobot.runtime.session_clock import ProgressWatchdog, should_stop_for_wall_clock
            _watchdog = ProgressWatchdog(clock=_clock)

            while iteration < max_iterations:
                # #1101/#1899: wall-clock deadline check with safety margin
                if _wall_deadline is not None and should_stop_for_wall_clock(_wall_deadline, clock=_clock):
                    logger.warning("Subagent [{}] wall-clock deadline safety margin reached", task_id)
                    stop_reason = "wall_clock_deadline"
                    break

                # #1899: progress watchdog check (no progress for N minutes)
                if _watchdog.is_stalled():
                    logger.warning(
                        "Subagent [{}] progress watchdog timeout reached (no progress for {:.1f}s)",
                        task_id, _watchdog.timeout_secs,
                    )
                    stop_reason = "progress_watchdog_timeout"
                    break

                iteration += 1
                _iter_msg_start = len(messages)

                # #1793 (ADR-026): patch the step line of the already-built
                # system prompt in place for this turn -- cheap (a single
                # regex substitution, no re-read of any file) and reads the
                # SAME `iteration`/`max_iterations` this loop's own
                # condition tests, so the two can never diverge. A no-op on
                # a system prompt built without a step line (there is
                # always one here, since _build_subagent_prompt always
                # calls build_system_prompt with iteration=1 above).
                if messages and messages[0].get("role") == "system":
                    from nanobot.agent.context import ContextBuilder as _CtxBuilder

                    messages[0]["content"] = _CtxBuilder.update_step_position(
                        str(messages[0].get("content") or ""), iteration, max_iterations,
                    )

                # #1775: inject step position notification into the tail of the
                # conversation (the last tool result) for the active turn, so the
                # model sees its position in the recency window. Numbering matches
                # the system prompt's `Step {iteration} of {max_iterations}.` exactly.
                if iteration > 1 and messages and messages[-1].get("role") == "tool":
                    _remaining = max(0, max_iterations - iteration)
                    _step_tail = f"\n\n[Step {iteration}/{max_iterations} | remaining {_remaining}]"
                    _prev_content = str(messages[-1].get("content") or "")
                    if not _prev_content.endswith(_step_tail):
                        messages[-1]["content"] = f"{_prev_content}{_step_tail}"

                # #1893/#1775: the subagent's last tick belongs to its final answer,
                # not another tool call. This uses (not extends) its max_iterations box.
                _is_final_turn = iteration == max_iterations
                if _is_final_turn:
                    if self._telemetry_component == "planner":
                        messages.append({
                            "role": "user", "content": (
                                "This is your final planning turn. Do not call tools. "
                                "Return only the final JSON object with a non-empty plan "
                                "and iterations_planned, using the evidence already read."
                            ),
                        })
                    else:
                        messages.append({
                            "role": "user", "content": (
                                "This is your final execution turn. Do not call tools. "
                                "Provide your final response and summary of work completed."
                            ),
                        })
                _tools_def = [] if _is_final_turn else tools.get_definitions()
                try:
                    # #1899: bound in-flight model call by remaining progress watchdog time
                    _call_timeout = _watchdog.remaining_time()
                    if _call_timeout <= 0:
                        logger.warning("Subagent [{}] progress timeout before model call", task_id)
                        stop_reason = "progress_watchdog_timeout"
                        break

                    if self._telemetry_component:
                        from nanobot.observability.llm_telemetry import (
                            call_context,
                            current_cycle_id,
                        )

                        with call_context(current_cycle_id(), self._telemetry_component):
                            response = await asyncio.wait_for(
                                self.provider.chat_with_retry(
                                    messages=messages,
                                    tools=_tools_def,
                                    model=self.model,
                                ),
                                timeout=_call_timeout,
                            )
                    else:
                        response = await asyncio.wait_for(
                            self.provider.chat_with_retry(
                                messages=messages,
                                tools=_tools_def,
                                model=self.model,
                            ),
                            timeout=_call_timeout,
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Subagent [{}] model call timed out by progress watchdog ({:.1f}s)",
                        task_id, _watchdog.timeout_secs,
                    )
                    stop_reason = "progress_watchdog_timeout"
                    break

                _watchdog.record_call_completed()
                usage = getattr(response, "usage", None)
                prompt_tokens = (
                    usage.get("prompt_tokens")
                    if isinstance(usage, dict)
                    else None
                )
                if not isinstance(prompt_tokens, int) or isinstance(prompt_tokens, bool) or prompt_tokens < 0:
                    prompt_tokens = None

                if prompt_tokens is not None:
                    context_usage["iterations"].append(prompt_tokens)
                    if prompt_tokens > context_usage["peak_tokens"]:
                        context_usage["peak_tokens"] = prompt_tokens

                if response.finish_reason == "error":
                    raise RuntimeError(f"LLM execution failed: {response.content}")

                # #1774: a response cut at the completion ceiling is a
                # CONTINUATION SIGNAL, not a final answer. It is cut mid-stream,
                # so it carries no complete tool call, `has_tool_calls` is False,
                # and the branch below would have taken the fragment as the
                # cycle's result -- measured on host eeepc over 9,714 executor
                # calls, 21 of the 23 truncated responses were the last call of
                # their cycle, with most of the iteration budget unspent.
                #
                # This is the OUTPUT ceiling, not context pressure: completion
                # was exactly 8,192 on every one of the 23, never above, and one
                # of them had ~33,000 tokens of input headroom left.
                #
                # Guarded on `has_tool_calls` as well: a response that did parse
                # a complete tool call is usable whatever the finish reason, and
                # must keep going down the normal path.
                if response.finish_reason == "length" and not response.has_tool_calls:
                    _truncation["count"] += 1
                    _truncation["consecutive"] += 1
                    if _is_final_turn:
                        stop_reason = "response_truncated"
                        break
                    if _truncation["consecutive"] >= _MAX_CONSECUTIVE_TRUNCATIONS:
                        logger.warning(
                            "Subagent [{}] abort: {} consecutive truncated responses (max={})",
                            task_id, _truncation["consecutive"], _MAX_CONSECUTIVE_TRUNCATIONS,
                        )
                        stop_reason = "response_truncated"
                        break
                    # Keep the partial text: it is the first half of the thought
                    # the model is being asked to finish, and discarding it would
                    # make the continuation a retry from nothing.
                    messages.append(build_assistant_message(
                        response.content or "",
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append({"role": "user", "content": _TRUNCATION_CONTINUATION_NOTE})
                    continue
                if _truncation["consecutive"]:
                    # A response that was not truncated after one that was: the
                    # continuation recovered the cycle.
                    _truncation["continued"] = True
                    _truncation["consecutive"] = 0

                if _is_final_turn and response.has_tool_calls:
                    # A provider ignoring the empty tool set must not execute
                    # another call outside the subagent's final-response turn.
                    stop_reason = "finalization_tool_call"
                    break
                if response.has_tool_calls:
                    tool_call_dicts = [
                        tc.to_openai_tool_call()
                        for tc in response.tool_calls
                    ]
                    messages.append(build_assistant_message(
                        response.content or "",
                        tool_calls=tool_call_dicts,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))

                    # Execute tools
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)
                        result = await tools.execute(tool_call.name, tool_call.arguments)
                        if tool_call.name == "search_memory":
                            try:
                                memory_result = json.loads(result)
                                if (
                                    isinstance(memory_result, dict)
                                    and memory_result.get("status") == "unavailable"
                                    and memory_result.get("decision") == "blocked"
                                ):
                                    memory_unavailable_reason = str(
                                        memory_result.get("reason") or "memory_unavailable"
                                    )[:200]
                            except (TypeError, ValueError):
                                # A malformed result is also unavailable evidence,
                                # never a real zero-result search.
                                memory_unavailable_reason = "invalid_memory_search_result"
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
                        })
                    # ADR-035 keep-work (#1942 B2): this iteration's tool
                    # calls just finished -- checkpoint now, before whatever
                    # comes next (another provider call, a break) can be the
                    # thing that gets killed.
                    if self._checkpoint_commits:
                        self._maybe_checkpoint_commit()
                    _watchdog.record_step_completed()
                    if memory_unavailable_reason:
                        stop_reason = "memory_search_unavailable"
                        break
                    # #959/#1122 context compaction: trim old tool results once
                    # per provider response, using real prompt usage plus the
                    # estimated appended delta for this iteration. Fail-open.
                    try:
                        _appended_delta = (
                            _ctx_compact._total_tokens(messages[_iter_msg_start:])
                            if prompt_tokens is not None
                            else 0
                        )
                        messages = _ctx_compact.compact_messages(
                            messages,
                            cycle_id=self._skill_fitness_cycle_id,
                            iteration=iteration,
                            state_root=self._state_root,
                            prompt_tokens=prompt_tokens,
                            prompt_token_delta=_appended_delta,
                        )
                    except Exception as _compact_exc:
                        logger.warning(
                            "context_compaction: subagent loop compaction failed open: {}",
                            _compact_exc,
                        )

                    # #1101: identical-call loop breaker — compare response-level tuple.
                    # A multi-tool response [A, B] repeated across iterations increments
                    # the counter; a different tuple (or single-call change) resets it.
                    _resp_key = _canonical_response_key(response.tool_calls)
                    if _resp_key == _lb_last_key:
                        _lb_count += 1
                    else:
                        _lb_last_key = _resp_key
                        _lb_count = 1

                    if _lb_count >= _lbk:
                        _names = ", ".join(tc.name for tc in response.tool_calls)
                        logger.warning(
                            "Subagent [{}] abort: {} identical response tuples (K={})",
                            task_id, _lb_count, _lbk,
                        )
                        stop_reason = "identical_call_loop"
                        break
                else:
                    final_result = response.content
                    break

            if memory_unavailable_reason:
                final_result = json.dumps({
                    "action_taken": "Memory-dependent work deferred because verified memory retrieval was unavailable.",
                    "files_changed": [],
                    "outcome": "blocked",
                    "concrete_next_action": "Restore the memory search corpus and retry.",
                    "findings": [f"memory_search_unavailable:{memory_unavailable_reason}"],
                }, ensure_ascii=False)
                stop_reason = "memory_search_unavailable"
            if stop_reason == "identical_call_loop":
                final_result = (
                    f"Task aborted: subagent repeated the same tool call "
                    f"{_lb_count} consecutive times (stop_reason=identical_call_loop)."
                )
            elif stop_reason == "wall_clock_deadline":
                final_result = (
                    "I reached the wall-clock time limit before completing the task. "
                    "You can try breaking the task into smaller steps."
                )
            elif stop_reason == "finalization_tool_call":
                final_result = (
                    "Task aborted: planner final turn returned a tool call instead of a final answer."
                    if self._telemetry_component == "planner"
                    else "Task aborted: final execution turn returned a tool call instead of a final answer."
                )
            elif stop_reason == "response_truncated":
                # #1774: distinguishable from a normal completion on purpose --
                # before this, the same situation ended the cycle as a silent
                # "partial: no artifact recorded".
                final_result = (
                    f"Task aborted: {_truncation['consecutive']} consecutive responses were cut "
                    f"at the model's completion ceiling (stop_reason=response_truncated). "
                    f"The reply is too long to finish; the next attempt should ask for a "
                    f"smaller step."
                )
            elif stop_reason == "progress_watchdog_timeout":
                final_result = (
                    f"Task aborted: no progress (model call or tool step) "
                    f"completed for {_watchdog.timeout_secs:.1f}s (stop_reason=progress_watchdog_timeout)."
                )
            elif final_result is None:
                final_result = "Task completed but no final response was generated."

            finished_at = self._utc_now()
            _telem_status = "blocked" if stop_reason == "memory_search_unavailable" else (
                "bounded_stop" if stop_reason else "ok"
            )
            self.last_max_call_gap_s = _watchdog.max_call_gap_s
            self._write_subagent_telemetry(
                task_id,
                self._build_subagent_telemetry_payload(
                    task_id=task_id,
                    task=task,
                    label=label,
                    started_at=self._read_subagent_started_at(task_id) or finished_at,
                    finished_at=finished_at,
                    status=_telem_status,
                    summary=final_result,
                    result=final_result,
                    origin=origin,
                    session_key=session_key,
                    correlation_context=correlation_context,
                    stop_reason=stop_reason,
                    context_usage=context_usage,
                    truncation={
                        "count": _truncation["count"],
                        "continued": _truncation["continued"],
                    },
                    max_call_gap_s=_watchdog.max_call_gap_s,
                ),
            )
            peak = context_usage["peak_tokens"]
            iters = len(context_usage["iterations"])
            logger.info("Subagent [{}] completed successfully (peak_tokens: {}, iterations: {})", task_id, peak, iters)
            await self._announce_result(task_id, label, task, final_result, origin, "ok")

        except asyncio.CancelledError:
            cancelled_at = self._utc_now()
            self._write_subagent_telemetry(
                task_id,
                self._build_subagent_telemetry_payload(
                    task_id=task_id,
                    task=task,
                    label=label,
                    started_at=self._read_subagent_started_at(task_id) or cancelled_at,
                    finished_at=cancelled_at,
                    status="cancelled",
                    summary="Cancelled before completion.",
                    result="Cancelled before completion.",
                    origin=origin,
                    session_key=session_key,
                    correlation_context=correlation_context,
                    context_usage=context_usage,
                ),
            )
            logger.info("Subagent [{}] cancelled", task_id)
            raise
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            finished_at = self._utc_now()
            self._write_subagent_telemetry(
                task_id,
                self._build_subagent_telemetry_payload(
                    task_id=task_id,
                    task=task,
                    label=label,
                    started_at=self._read_subagent_started_at(task_id) or finished_at,
                    finished_at=finished_at,
                    status="error",
                    summary=error_msg,
                    result=error_msg,
                    origin=origin,
                    session_key=session_key,
                    correlation_context=correlation_context,
                    context_usage=context_usage,
                ),
            )
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, error_msg, origin, "error")

    def _maybe_checkpoint_commit(self) -> None:
        """ADR-035 keep-work (#1942 B2): commit the workspace's dirty tree
        to the current branch after a completed tool-execution step --
        ONLY when that branch is a ``selfevo/cycle-*`` cycle branch. "The
        executor's work is committed to the cycle branch as it goes, at
        least at every completed step that changed files ... a kill loses
        minutes, not the attempt."

        The branch guard matters because ``bridge._setup_cycle_branch`` can
        fail open and leave the checkout on ``main`` (dirty_tree/
        checkout_failed) while the executor still spawns -- committing a
        checkpoint straight to the SHARED local ``main`` there would be the
        "workspace main diverges" defect class, not a harmless checkpoint.
        A branch this loose check cannot see (renamed, detached HEAD) is
        treated the same as ``main``: skip, never guess.

        Never pushed here -- push happens once, at integration or gate
        time, the same as every other cycle-branch commit; a checkpoint's
        only job is to exist locally so a kill doesn't lose it. Fail-open
        and silent: only called when ``self._checkpoint_commits`` is True
        (the eeebot executor spawn only), and a checkpoint failure must
        never abort or even flag the loop turn it rides along on -- the
        end-of-turn commit/gate path is unaffected either way. Every git
        call is timeout-bounded so a stuck index.lock cannot hang the
        executor's own loop.

        D3 (ADR-035 Test Contract, external review finding #3; architect
        resolution on #1979 external-review followups): the branch guard
        below is an EXACT comparison against ``self._expected_cycle_branch``
        (what the bridge resolved for this spawn via
        ``_setup_cycle_branch``), read via ``symbolic-ref HEAD`` (never a
        ``selfevo/cycle-`` prefix test, and never ``rev-parse
        --abbrev-ref``, which also answers for a detached HEAD) -- a
        workspace sitting on a DIFFERENT cycle's branch (stray checkout
        left by another process, a race) is a real ``selfevo/cycle-*``
        branch too, so a prefix match would silently commit this
        executor's work onto it. ``expected_cycle_branch`` being unset
        (``None``) skips checkpointing entirely; it never falls back to
        guessing from HEAD.

        No private/temporary index: this stages the workspace's ORDINARY
        index with ``git add``, exactly like a normal commit would -- the
        architect's #1979 resolution retired an earlier private-index
        design as unnecessary complexity. The commit is still written
        through plumbing (``write-tree`` / ``commit-tree`` / ``update-ref``)
        rather than ``git commit``, so it never reads or writes ``HEAD``
        itself: ``update-ref refs/heads/<expected_cycle_branch> <new>
        <old>`` targets the branch ref directly, with the ref's tip at the
        START of this call passed as the compare-and-swap old-value.
        Because ``write-tree`` builds the new commit FROM the same index
        ``git add`` just staged, the index already equals the committed
        tree once this succeeds -- no follow-up sync step, no desync.

        HEAD is re-checked (same ``symbolic-ref`` test) after ``write-tree``
        but before ``commit-tree`` -- a checkout racing in between the
        first check and here must not let this commit land at all, even
        onto the still-nominally-correct expected branch: the working tree
        it just staged from may no longer coherently represent that
        branch's state. Any failed check, or a rejected CAS (the branch
        moved between capturing ``old`` and ``update-ref`` -- e.g. another
        writer, or the same race from a different angle), unstages via
        ``git reset -q`` (never ``--hard``: it must not touch the working
        tree, only the index) and skips -- the fail-open, silent contract
        below is unchanged; a debug log line records why for anyone
        reading logs, but nothing here can fail the loop turn it rides
        along on.
        """
        import subprocess as _sp_ckpt

        from nanobot.runtime.commit_markers import (
            CHECKPOINT_SUBJECT_PREFIX, CHECKPOINT_TRAILER, is_blocked_checkpoint_filename,
        )

        expected_branch = self._expected_cycle_branch
        if not expected_branch:
            return
        expected_ref = f"refs/heads/{expected_branch}"
        repo_root = self.workspace
        git = ["git", "-c", f"safe.directory={repo_root}", "-C", str(repo_root)]

        def _on_expected_branch() -> bool:
            symref = _sp_ckpt.run(
                git + ["symbolic-ref", "-q", "HEAD"], capture_output=True, text=True, timeout=10,
            )
            return symref.returncode == 0 and symref.stdout.strip() == expected_ref

        def _abandon(reason: str) -> None:
            # Unstage only -- never touch the working tree (`-q`, no
            # `--hard`). Best-effort: a failure here is still fail-open,
            # the checkpoint was already being skipped either way.
            _sp_ckpt.run(git + ["reset", "-q"], capture_output=True, text=True, timeout=10)
            logger.debug("Subagent checkpoint skipped ({}): {}", expected_branch, reason)

        try:
            if not _on_expected_branch():
                _abandon("HEAD not on expected branch")
                return
            old_tip = _sp_ckpt.run(
                git + ["rev-parse", expected_ref], capture_output=True, text=True, timeout=10,
            )
            if old_tip.returncode != 0 or not old_tip.stdout.strip():
                return
            old_sha = old_tip.stdout.strip()

            status = _sp_ckpt.run(
                git + ["status", "--porcelain", "-uall"], capture_output=True, text=True, timeout=10,
            )
            if status.returncode != 0 or not status.stdout.strip():
                return
            changed: list[str] = []
            for line in status.stdout.splitlines():
                if not line.strip():
                    continue
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                path = path.strip('"')
                if path and not is_blocked_checkpoint_filename(path):
                    changed.append(path)
            if not changed:
                return
            for path in changed:
                _sp_ckpt.run(git + ["add", "--", path], capture_output=True, text=True, timeout=10)
            if len(changed) == 1:
                paths_desc = changed[0]
            else:
                joined = ", ".join(changed)
                paths_desc = joined if len(joined) <= 60 and len(changed) <= 3 else f"{len(changed)} paths"
            subject = f"{CHECKPOINT_SUBJECT_PREFIX} — {paths_desc}"

            tree = _sp_ckpt.run(
                git + ["write-tree"], capture_output=True, text=True, timeout=10,
            )
            if tree.returncode != 0 or not tree.stdout.strip():
                _abandon("write-tree failed")
                return

            if not _on_expected_branch():
                _abandon("HEAD moved during staging")
                return

            commit = _sp_ckpt.run(
                git + ["commit-tree", tree.stdout.strip(), "-p", old_sha, "-m", subject, "-m", CHECKPOINT_TRAILER],
                capture_output=True, text=True, timeout=10,
            )
            if commit.returncode != 0 or not commit.stdout.strip():
                _abandon("commit-tree failed")
                return
            new_sha = commit.stdout.strip()

            cas = _sp_ckpt.run(
                git + ["update-ref", expected_ref, new_sha, old_sha],
                capture_output=True, text=True, timeout=10,
            )
            if cas.returncode != 0:
                _abandon("update-ref CAS rejected (branch moved concurrently)")
                return
        except Exception:
            pass

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        # Inject as system message to trigger main agent
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    def _build_subagent_correlation_context(self) -> dict[str, Any]:
        """Return best-effort runtime correlation data for durable telemetry."""
        try:
            from nanobot.runtime.state import load_runtime_state_for_workspace
            from nanobot.runtime.state_access import ledger_window

            runtime = load_runtime_state_for_workspace(self.workspace)
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            ledger = ledger_window(
                self._state_root,
                since_ts=since.isoformat().replace("+00:00", "Z"),
                phases=frozenset({"started"}),
            )
            if ledger.status == "complete" and ledger.rows:
                runtime["cycle_id"] = ledger.rows[-1].get("cycle_id")
        except Exception:
            return {}

        if not isinstance(runtime, dict):
            return {}

        correlation: dict[str, Any] = {}
        goal_id = runtime.get("active_goal") or runtime.get("goal_id")
        cycle_id = runtime.get("cycle_id")
        report_path = runtime.get("report_path")
        current_task_id = runtime.get("current_task_id")
        task_reward_signal = runtime.get("task_reward_signal")
        task_feedback_decision = runtime.get("task_feedback_decision")

        if isinstance(goal_id, str) and goal_id:
            correlation["goal_id"] = goal_id
        if isinstance(cycle_id, str) and cycle_id:
            correlation["cycle_id"] = cycle_id
        if isinstance(report_path, str) and report_path:
            correlation["report_path"] = report_path
        if isinstance(current_task_id, str) and current_task_id:
            correlation["current_task_id"] = current_task_id
        if isinstance(task_reward_signal, dict):
            correlation["task_reward_signal"] = task_reward_signal
        if isinstance(task_feedback_decision, dict):
            correlation["task_feedback_decision"] = task_feedback_decision
        return correlation

    def _build_subagent_telemetry_payload(
        self,
        *,
        task_id: str,
        task: str,
        label: str,
        started_at: str,
        finished_at: str | None,
        status: str,
        summary: str | None,
        result: str | None,
        origin: dict[str, str],
        session_key: str | None,
        correlation_context: dict[str, Any] | None = None,
        stop_reason: str | None = None,
        context_usage: dict[str, Any] | None = None,
        truncation: dict[str, Any] | None = None,
        max_call_gap_s: float | None = None,
    ) -> dict[str, Any]:
        payload = {
            "subagent_id": task_id,
            "task": task,
            "label": label,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "summary": summary,
            "result": result,
            "origin": origin,
            "parent_context": self._build_parent_context(session_key, origin),
            "workspace": str(self.workspace),
            "runtime_state_root": str(self._state_root),
            "runtime_state_source": self._runtime_state_source,
        }
        if stop_reason:
            payload["stop_reason"] = stop_reason
        if max_call_gap_s is not None:
            payload["max_call_gap_s"] = round(float(max_call_gap_s), 1)
        if correlation_context:
            payload.update(correlation_context)
        if context_usage is not None:
            payload["context_usage"] = context_usage
        if truncation is not None:
            # #1774: written unconditionally once the caller passes it, zeros
            # included. A cycle that saw no truncation and a cycle from a
            # release that could not record one must not read the same.
            payload["truncation"] = truncation
        return payload

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    def _subagent_path(self, task_id: str) -> Path:
        return self._telemetry_dir / f"{task_id}.json"

    def _build_parent_context(self, session_key: str | None, origin: dict[str, str]) -> dict[str, Any]:
        parent_context: dict[str, Any] = {"origin": origin}
        if session_key:
            parent_context["session_key"] = session_key
        return parent_context

    def _read_subagent_started_at(self, task_id: str) -> str | None:
        path = self._subagent_path(task_id)
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                started_at = data.get("started_at")
                return started_at if isinstance(started_at, str) else None
        except Exception:
            return None
        return None

    def _write_subagent_telemetry(self, task_id: str, payload: dict[str, Any]) -> None:
        self._telemetry_dir.mkdir(parents=True, exist_ok=True)
        path = self._subagent_path(task_id)
        tmp_path = path.with_suffix('.json.tmp')
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
        # `context_usage` rides in `payload`, so it lands in this one file. An
        # earlier revision also wrote a `<task_id>_context.json` beside it;
        # that broke five tests in tests/test_loop_breaker.py, which read
        # telemetry with `glob("*.json")` over this directory and started
        # picking up the sidecar instead. Never add a second `.json` here.

    EXECUTOR_TOOL_NAMES = EXECUTOR_TOOLSET

    @classmethod
    def declared_tool_names(cls) -> tuple[str, ...]:
        """Names declared in the loop executor prompt and registered by it."""
        return cls.EXECUTOR_TOOL_NAMES

    def registered_tool_names(self) -> tuple[str, ...]:
        """Names registered for this manager's configured execution role."""
        if self.web_tools_enabled:
            return self.EXECUTOR_TOOL_NAMES + ("web_search", "web_fetch")
        return self.EXECUTOR_TOOL_NAMES

    def _build_subagent_prompt(self) -> str:
        """Build the system prompt for the subagent.

        Uses the full ContextBuilder pipeline so that the ADR-022 ontology
        blocks (IDENTITY.md, SOUL.md, goals.md, USER.md, OPERATING.md,
        AGENTS.md), always-skills (including memory/MEMORY.md), and the
        skills catalogue are all visible to the subagent — exactly as they
        are for the main agent session. ``release_root`` (set by the bridge
        for the self-evolving executor) is where the release-owned blocks
        are read from; ``None`` for every other caller.

        #1725: the bridge no longer appends a post-fit charter+identity
        ``system_context`` tail for the executor — those blocks are now
        loaded and capped inside the fit itself. ``system_context`` (when a
        caller still sets it) is still appended after the fit, unchanged,
        for whatever non-loop use it may have elsewhere.

        #939 Part E: passes ``excluded_skill_names`` to suppress operator-only
        builtin skills (weather, tmux, clawhub) from the loop summary without
        changing normal ContextBuilder defaults for interactive sessions.
        """
        # #1852 (ADR-031 rule 5): a role-prompt caller supplies its whole,
        # fixed system prompt and skips the ContextBuilder ontology path
        # (OPERATING.md's cycle contract) entirely -- see ``__init__``.
        # ``getattr`` (not ``self._role_system_prompt``): several tests
        # construct a SubagentManager via ``__new__`` and set only the
        # attributes their scenario needs, predating this one -- absent
        # means "no override" exactly like the real ``__init__`` default.
        if getattr(self, '_role_system_prompt', None) is not None:
            prompt = self._role_system_prompt
            if self.system_context:
                prompt += "\n\n---\n\n" + self.system_context
            return prompt

        from nanobot.agent.context import ContextBuilder

        # #1766 (ADR-023): the scorecard block reads state/scorecard/latest.json
        # from the same harness-owned state root skill_fitness already uses
        # (skill_fitness_state_dir) — never self.workspace, and never a new
        # env-derived path, so the loop-writable/harness-owned boundary this
        # relies on is exactly the one already threaded through the bridge.
        builder = ContextBuilder(
            self.workspace, release_root=self.release_root, state_dir=self._skill_fitness_state_dir,
        )
        # #1300: the loop profile is strict — a prompt that cannot hold every
        # critical AGENTS.md section raises SystemPromptOverflow here, and the
        # bridge records the cycle as failed instead of spawning on a prompt
        # missing its standing instructions. What was kept/dropped is left on
        # ``last_prompt_fit`` for the bridge to journal.
        try:
            prompt = builder.build_system_prompt(
                excluded_skill_names=self._excluded_skill_names or None,
                loop_profile=True,
                degrade_on_overflow=True,
                # #1793 (ADR-026): step 1 of this cycle's budget -- the loop
                # (below, in the spawn loop) patches this in place on every
                # later turn via ContextBuilder.update_step_position, using
                # the SAME self.max_iterations this prompt is built with, so
                # the two can never diverge.
                iteration=1,
                max_iterations=self.max_iterations,
                cycle_id=self._skill_fitness_cycle_id,
            )
        finally:
            self.last_prompt_fit = builder.last_fit
        if self.system_context:
            # #1379: appended AFTER the fit, so it is outside the cap and
            # outside ``last_prompt_fit["chars"]``/``["sections"]`` — the
            # ledger row describes the capped builder prompt, not this
            # operator-owned tail (charter + loop identity).
            prompt += builder.SECTION_SEPARATOR + self.system_context
        return prompt

    def collect_skill_reads(self) -> int:
        """Persist accumulated SKILL.md reads to the skill-fitness sidecar.

        Called by the bridge AFTER the spawn window closes (harness-side write,
        protected by the FITNESS_SIDECARS spawn-boundary integrity check).  The
        bridge snapshots sidecar hashes before spawn and re-hashes after; this
        write therefore lands OUTSIDE the protected window and is never flagged
        as an integrity incident.

        Returns the count of rows persisted (0 when instrumentation is not
        configured or no reads accumulated).  Fail-open.

        #1767: the marker row also carries the SKILL.md reads that FAILED,
        so ``skill_count: 0`` stops being ambiguous between "the executor
        never asked for a skill" and "it asked and the lookup failed" --
        two findings whose fixes point in opposite directions.

        #1666 phase 1 / #1654: also records ONE per-cycle marker row
        (``skill_fitness.record_cycle_skill_scan``) regardless of whether
        any skill was read this cycle -- a cycle that read zero skills must
        be distinguishable from a cycle where this method was never called
        at all (instrumentation off, an older release, a crash). That
        marker is unconditional; the detailed per-skill rows below remain
        conditional on there being anything to record.
        """
        if self._skill_fitness_state_dir is None:
            return 0
        # Read outside the try: the enclosing `except` exists to swallow a
        # failed WRITE, and folding the attribute access into it would turn
        # a missing field into a silently empty one -- the exact
        # no-data-as-zero confusion this row exists to prevent.
        attempted = list(self._skill_read_failures_this_cycle)
        self._skill_read_failures_this_cycle.clear()
        try:
            from nanobot.runtime.skill_fitness import record_cycle_skill_scan
            # #1857: the earliest tool-call position among this cycle's own
            # skill reads -- same "how early was the obligation met" signal
            # ADR-028 rule 5 (#1812) already records for the day file.
            positions = [
                int(r["position"]) for r in self._skill_reads_this_cycle
                if isinstance(r.get("position"), int)
            ]
            record_cycle_skill_scan(
                self._skill_fitness_state_dir,
                cycle_id=self._skill_fitness_cycle_id,
                skills_read=[
                    str(r.get("skill") or "") for r in self._skill_reads_this_cycle if r.get("skill")
                ],
                skills_attempted_not_found=attempted,
                tool_call_position=min(positions) if positions else None,
            )
        except Exception:
            pass
        if not self._skill_reads_this_cycle:
            return 0
        try:
            from nanobot.runtime.skill_fitness import record_skill_reads
            n = record_skill_reads(
                state_dir=self._skill_fitness_state_dir,
                reads=self._skill_reads_this_cycle,
                repo=self._skill_fitness_repo,
                cycle_id=self._skill_fitness_cycle_id,
                cycle_base_sha=self._skill_fitness_cycle_base_sha,
            )
            self._skill_reads_this_cycle.clear()
            return n
        except Exception:
            return 0

    def collect_day_file_reads(self) -> list[dict]:
        """Return and clear this cycle's day-file reads (ADR-028 rule 5 /
        #1812): a list of ``{"day": str, "position": int}``, any day, in
        the order they were read.

        Called by the bridge after the spawn window closes, for EVERY
        cycle regardless of that cycle's integration outcome -- the
        obligation is to read today's day file as the first action of the
        cycle, not a condition of the cycle's code being accepted. The
        bridge (not this class) turns this into a persisted row: ADR-028
        rule 4 keeps this file, and the prompt-assembly path it shares
        with :meth:`_build_subagent_prompt`, free of the module name that
        would do that persisting.

        Returns ``[]`` when instrumentation is not configured or nothing
        was read.
        """
        if self._skill_fitness_state_dir is None:
            return []
        reads = list(self._day_file_reads_this_cycle)
        self._day_file_reads_this_cycle.clear()
        return reads

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
