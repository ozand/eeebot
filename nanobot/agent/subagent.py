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
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ExecToolConfig
from nanobot.providers.base import LLMProvider
from nanobot.runtime import context_compaction as _ctx_compact
from nanobot.utils.helpers import build_assistant_message

# ── #1101: identical-tool-call loop breaker ───────────────────────────────────
# Number of consecutive identical (tool_name, canonical_args) calls before the
# warning message is injected.  2×K triggers abort.  Env-tunable; default 3.
_LOOP_BREAKER_K_DEFAULT = 3


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
        web_search_config: "WebSearchConfig | None" = None,
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
    ):
        from nanobot.config.schema import ExecToolConfig, WebSearchConfig

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.subagent_config = subagent_config
        configured_max_running = getattr(subagent_config, "max_running", None)
        self.max_running = int(max_running or configured_max_running or 1)
        # Issue #578: previously hardcoded to 15 inside _run_subagent's loop, decoupled
        # from agents.defaults.maxToolIterations (the main agent's own cap). Callers
        # should now pass the same value through so subagents aren't cut off earlier
        # than the coordinator's budget allows.
        self.max_iterations = int(max_iterations) if max_iterations else 15
        self.restrict_to_workspace = restrict_to_workspace
        self.system_context = system_context.strip()
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
        # #939 Part E: excluded skill names for the loop summary
        self._excluded_skill_names: list[str] = list(excluded_skill_names or [])

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
            extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
            # #939 Part C: wire SKILL.md read instrumentation callback.
            _on_skill_read = None
            if self._skill_fitness_state_dir is not None:
                workspace_skills = (self.workspace / "skills").resolve()

                def _on_skill_read(skill_path: Path) -> None:  # noqa: E301
                    try:
                        rel = skill_path.relative_to(workspace_skills)
                    except ValueError:
                        return
                    if len(rel.parts) == 2 and rel.parts[1] == "SKILL.md":
                        self._skill_reads_this_cycle.append(
                            {"skill": rel.parts[0], "path": f"skills/{rel.as_posix()}"}
                        )
            tools.register(ReadFileTool(
                workspace=self.workspace,
                allowed_dir=allowed_dir,
                extra_allowed_dirs=extra_read,
                on_skill_read=_on_skill_read,
            ))
            tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
            ))
            tools.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
            tools.register(WebFetchTool(proxy=self.web_proxy))

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
            # #1101: loop-breaker state
            _lbk = _loop_breaker_k()
            _lb_last_key: str | None = None
            _lb_count: int = 0
            # #1101: wall-clock soft deadline (injectable for tests)
            _wall_deadline = _subagent_wall_deadline(_monotonic=getattr(self, '_monotonic', None))

            while iteration < max_iterations:
                # #1101: wall-clock deadline check (same graceful path as max_iterations)
                if _wall_deadline is not None:
                    _clock = getattr(self, '_monotonic', None) or time.monotonic
                    if _clock() >= _wall_deadline:
                        logger.warning("Subagent [{}] wall-clock deadline reached", task_id)
                        stop_reason = "wall_clock_deadline"
                        break

                iteration += 1
                _iter_msg_start = len(messages)

                response = await self.provider.chat_with_retry(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=self.model,
                )
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
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
                        })
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
                            "context_compaction: subagent loop compaction failed open: %s",
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

                    if _lb_count >= 2 * _lbk:
                        # Hard abort: 2K identical response tuples.
                        _names = ", ".join(tc.name for tc in response.tool_calls)
                        logger.warning(
                            "Subagent [{}] abort: {} identical response tuples (2K={})",
                            task_id, _lb_count, 2 * _lbk,
                        )
                        stop_reason = "identical_call_loop"
                        break
                    elif _lb_count == _lbk:
                        # Warning injection at exactly K: add synthetic tool result message.
                        _last_tc = response.tool_calls[-1]
                        _names = ", ".join(tc.name for tc in response.tool_calls)
                        logger.info(
                            "Subagent [{}] loop-breaker warning: {} identical response tuples",
                            task_id, _lb_count,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": _last_tc.id + "-lb",
                            "name": "loop_breaker",
                            "content": (
                                f"Safety stop: this response (tools: [{_names}]) has been "
                                f"repeated {_lb_count} consecutive times with no change. "
                                f"Change approach, try a different strategy, or "
                                f"provide the final answer now."
                            ),
                        })
                else:
                    final_result = response.content
                    break

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
            elif final_result is None:
                final_result = "Task completed but no final response was generated."

            finished_at = self._utc_now()
            _telem_status = "bounded_stop" if stop_reason else "ok"
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
        if correlation_context:
            payload.update(correlation_context)
        if context_usage is not None:
            payload["context_usage"] = context_usage
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

    def _build_subagent_prompt(self) -> str:
        """Build the system prompt for the subagent.

        Uses the full ContextBuilder pipeline so that AGENTS.md, always-skills
        (including memory/MEMORY.md), and the skills catalogue are all visible
        to the subagent — exactly as they are for the main agent session.

        #939 Part E: passes ``excluded_skill_names`` to suppress operator-only
        builtin skills (weather, tmux, clawhub) from the loop summary without
        changing normal ContextBuilder defaults for interactive sessions.
        """
        from nanobot.agent.context import ContextBuilder

        builder = ContextBuilder(self.workspace)
        # #1300: the loop profile is strict — a prompt that cannot hold every
        # critical AGENTS.md section raises SystemPromptOverflow here, and the
        # bridge records the cycle as failed instead of spawning on a prompt
        # missing its standing instructions. What was kept/dropped is left on
        # ``last_prompt_fit`` for the bridge to journal.
        try:
            prompt = builder.build_system_prompt(
                excluded_skill_names=self._excluded_skill_names or None,
                loop_profile=True,
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
        """
        if self._skill_fitness_state_dir is None or not self._skill_reads_this_cycle:
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
