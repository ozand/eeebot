"""Phase-1 subagent tool-harness: read-only tools + a small turn loop.

Implements the read-only slice of ``docs/changes/643-subagent-tool-harness/design.md``:
``read``/``grep``/``ls`` tools, workspace confinement, a single veto hook, and a
turn loop that reuses the existing LiteLLM-backed :class:`LLMProvider` call
mechanics (``nanobot.providers.base.LLMProvider.chat_with_retry``) rather than
inventing a second LLM client (issue #643, resolved question 1).

Everything here is policy-free except the single ``before_tool_call`` veto
hook (design.md "Single veto hook as the only policy seam"): tools never
raise into the loop, and the loop never invents its own budget/stop-reason
model — it reuses ``nanobot.runtime.stop_guards`` so a harness run is
comparable to any other stop-guard-tracked run (resolved question 5).

Phase 1 is strictly read-only: no ``edit``/``write``/``bash`` tool exists
here (those are gated to phase 2/3 per the design and issue #643's decision).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from nanobot.observability.llm_telemetry import call_context
from nanobot.runtime._io import utc_iso as _utc_iso
from nanobot.runtime.stop_guards import STOP_REASON_GATE_CLEAN
from nanobot.runtime.stop_guards import derive_stop_reason as _derive_stop_reason

# The R11-R13 stop-reason vocabulary in stop_guards.py is cycle-stall-shaped
# (gate_clean/max_iterations/no_progress/budget_<name>) and has no entry for
# "the LLM call itself failed" — that is a harness-loop concern, not a
# cycle-level one, so it stays harness-local rather than growing the shared
# enum (found live: un/qwen model group down was silently reported as
# gate_clean/completed because chat_with_retry degrades to an error-content
# LLMResponse instead of raising; see #643 live-verification follow-up).
STOP_REASON_LLM_ERROR = "llm_error"

# ---------------------------------------------------------------------------
# Shared truncation (design.md "Shared truncation module")
# ---------------------------------------------------------------------------

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50_000
_HEAD_FRACTION = 0.6  # 60% of the kept budget is head, 40% is tail


def _apply_bridge_reasoning_effort(config: Any) -> str | None:
    """Push the operator-selected bridge reasoning effort into ``config`` (#832).

    ``_make_provider`` forwards ``config.agents.defaults.reasoning_effort`` (with
    ``supermind.reasoning_effort`` winning when supermind is enabled) into the
    completion call. Set both from ``SUBAGENT_BRIDGE_REASONING_EFFORT`` so a
    model without a baked ``-high`` name variant (e.g. ``cl/gpt-5.6-luna``) can
    still run the materializer at high reasoning. No-op when the env is
    unset/invalid. Returns the applied effort (or ``None``)."""
    from nanobot.runtime.llm_proposer import bridge_reasoning_effort

    effort = bridge_reasoning_effort()
    if not effort:
        return None
    config.agents.defaults.reasoning_effort = effort
    supermind = getattr(config, "supermind", None)
    if supermind is not None and getattr(supermind, "enabled", False):
        supermind.reasoning_effort = effort
    return effort


def truncate_text(
    text: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[str, dict[str, Any]]:
    """Deterministic head-tail truncation shared by ``read`` and ``grep`` output.

    Returns ``(possibly_truncated_text, metadata)`` where metadata is
    ``{"truncated": bool, "total_lines": int, "total_bytes": int}`` — told to
    the model explicitly rather than silently dropped, per design.md.
    """
    total_bytes = len(text.encode("utf-8", errors="replace"))
    lines = text.split("\n")
    total_lines = len(lines)

    over_lines = total_lines > max_lines
    over_bytes = total_bytes > max_bytes
    if not over_lines and not over_bytes:
        return text, {"truncated": False, "total_lines": total_lines, "total_bytes": total_bytes}

    if over_lines:
        head_n = max(1, int(max_lines * _HEAD_FRACTION))
        tail_n = max(0, max_lines - head_n)
        head = lines[:head_n]
        tail = lines[len(lines) - tail_n:] if tail_n else []
        omitted = total_lines - head_n - tail_n
        body = "\n".join(head) + f"\n... [{omitted} lines omitted] ...\n" + "\n".join(tail)
    else:
        body = text

    encoded = body.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        head_bytes = max(1, int(max_bytes * _HEAD_FRACTION))
        tail_bytes = max(0, max_bytes - head_bytes)
        head_part = encoded[:head_bytes].decode("utf-8", errors="ignore")
        tail_part = encoded[len(encoded) - tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
        body = head_part + "\n... [byte-truncated] ...\n" + tail_part

    return body, {"truncated": True, "total_lines": total_lines, "total_bytes": total_bytes}


# ---------------------------------------------------------------------------
# Workspace confinement (design.md "Safety model")
# ---------------------------------------------------------------------------


class PathEscapeError(Exception):
    """Raised when a resolved path is not a descendant of the workspace root.

    Never propagates past :func:`before_tool_call` / the tool functions — it
    is always converted to a veto tool-result string.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class WorkspaceOperations:
    """The concrete Operations backend: every path is resolved and confined.

    Tools are written against this small interface (design.md "Pluggable
    Operations interface") rather than against ``open()``/``Path`` directly,
    so "what a tool does" stays separate from "what a tool is allowed to
    touch". This is the only backend eeebot ships.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def resolve(self, rel_path: str) -> Path:
        """Resolve *rel_path* against the workspace root; raise on escape.

        Follows symlinks (``Path.resolve()``) and verifies the resolved path
        is a descendant of ``self.root`` before any I/O happens.
        """
        candidate = Path(rel_path or ".")
        base = candidate if candidate.is_absolute() else self.root / candidate
        try:
            resolved = base.resolve(strict=False)
        except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
            raise PathEscapeError(f"could not resolve path {rel_path!r}: {exc}") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise PathEscapeError(
                f"path {rel_path!r} resolves outside the workspace root"
            )
        return resolved

    def read_text(self, rel_path: str) -> str:
        path = self.resolve(rel_path)
        if not path.exists():
            raise FileNotFoundError(f"no such file: {rel_path}")
        if path.is_dir():
            raise IsADirectoryError(f"is a directory, not a file: {rel_path}")
        return path.read_text(encoding="utf-8", errors="replace")

    def list_dir(self, rel_path: str) -> list[tuple[str, bool]]:
        path = self.resolve(rel_path)
        if not path.exists():
            raise FileNotFoundError(f"no such directory: {rel_path}")
        if not path.is_dir():
            raise NotADirectoryError(f"not a directory: {rel_path}")
        entries: list[tuple[str, bool]] = []
        for child in path.iterdir():
            entries.append((child.name, child.is_dir()))
        entries.sort(key=lambda e: (not e[1], e[0].lower()))
        return entries

    def iter_files(self, rel_path: str | None, glob: str | None) -> Iterator[Path]:
        """Yield confined files under *rel_path* (default: workspace root).

        Files reached via a glob match that individually escape the root
        (e.g. a symlink inside the workspace pointing outside it) are
        skipped rather than aborting the whole search — the base path itself
        is still confinement-checked via :meth:`resolve`.
        """
        base = self.resolve(rel_path) if rel_path else self.root
        if base.is_file():
            yield base
            return
        if not base.is_dir():
            raise NotADirectoryError(f"not a directory: {rel_path}")
        pattern = glob or "**/*"
        for candidate in sorted(base.glob(pattern)):
            if not candidate.is_file():
                continue
            try:
                candidate.resolve().relative_to(self.root)
            except ValueError:
                continue  # symlink escape found via glob traversal — skip silently
            yield candidate


# ---------------------------------------------------------------------------
# Tool contracts
# ---------------------------------------------------------------------------

TOOL_READ = "read"
TOOL_GREP = "grep"
TOOL_LS = "ls"

# Which tools take a path-like argument the veto hook must confinement-check,
# and the argument name to check.
_PATH_ARG_BY_TOOL = {TOOL_READ: "path", TOOL_GREP: "path", TOOL_LS: "path"}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_READ,
            "description": (
                "Read a text file from the confined workspace, with 1-based "
                "line numbers. Large files are truncated (head+tail) with "
                "truncation metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path."},
                    "offset": {"type": "integer", "description": "1-based line number to start at (default 1)."},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_GREP,
            "description": (
                "Search workspace files for a Python regular expression. "
                "Pure-Python search (no external binary required). Matches "
                "are returned as path:line:text, truncated (head+tail)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression to search for."},
                    "path": {"type": "string", "description": "Workspace-relative directory or file to search (default: workspace root)."},
                    "glob": {"type": "string", "description": "Glob pattern relative to path (default: '**/*')."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_LS,
            "description": "List a workspace directory (sorted, directories marked).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative directory path (default: workspace root)."},
                },
                "required": [],
            },
        },
    },
]


@dataclass
class ToolOutcome:
    ok: bool
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def tool_read(ops: WorkspaceOperations, args: dict[str, Any]) -> ToolOutcome:
    path = str(args.get("path") or "").strip()
    if not path:
        return ToolOutcome(ok=False, text="read: 'path' is required")
    try:
        offset = int(args["offset"]) if args.get("offset") is not None else 1
        limit = int(args["limit"]) if args.get("limit") is not None else None
    except (TypeError, ValueError):
        return ToolOutcome(ok=False, text="read: 'offset'/'limit' must be integers")

    try:
        content = ops.read_text(path)
    except PathEscapeError as exc:
        # Defense in depth: before_tool_call should already have vetoed this.
        return ToolOutcome(ok=False, text=f"read: {exc.reason}")
    except (FileNotFoundError, IsADirectoryError) as exc:
        return ToolOutcome(ok=False, text=f"read: {exc}")
    except OSError as exc:
        return ToolOutcome(ok=False, text=f"read: error reading file: {exc}")

    lines = content.split("\n")
    start = max(0, offset - 1)
    end = (start + limit) if limit is not None else len(lines)
    selected = lines[start:end]
    numbered = "\n".join(f"{i + start + 1:>6}\t{line}" for i, line in enumerate(selected))
    if not selected:
        numbered = "(no lines in requested range)"
    body, meta = truncate_text(numbered)
    return ToolOutcome(ok=True, text=body, meta=meta)


_GREP_MAX_LINE_LEN = 500
_GREP_MAX_MATCHES = 500


def tool_grep(ops: WorkspaceOperations, args: dict[str, Any]) -> ToolOutcome:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return ToolOutcome(ok=False, text="grep: 'pattern' is required")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolOutcome(ok=False, text=f"grep: invalid regex: {exc}")

    rel_path = args.get("path")
    glob = args.get("glob")
    try:
        files = list(ops.iter_files(rel_path, glob))
    except PathEscapeError as exc:
        return ToolOutcome(ok=False, text=f"grep: {exc.reason}")
    except (FileNotFoundError, NotADirectoryError) as exc:
        return ToolOutcome(ok=False, text=f"grep: {exc}")

    matches: list[str] = []
    for file_path in files:
        try:
            rel = file_path.relative_to(ops.root)
        except ValueError:
            continue
        try:
            # Stream line-by-line rather than slurp — the i386 host is
            # memory-constrained (design.md / task constraints).
            with file_path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.rstrip("\n")
                    if regex.search(line):
                        if len(line) > _GREP_MAX_LINE_LEN:
                            line = line[:_GREP_MAX_LINE_LEN] + "…"
                        matches.append(f"{rel}:{lineno}:{line}")
                        if len(matches) >= _GREP_MAX_MATCHES:
                            break
        except OSError:
            continue
        if len(matches) >= _GREP_MAX_MATCHES:
            break

    if not matches:
        return ToolOutcome(ok=True, text="grep: no matches", meta={"truncated": False, "total_lines": 0, "total_bytes": 0})
    body, meta = truncate_text("\n".join(matches))
    return ToolOutcome(ok=True, text=body, meta=meta)


def tool_ls(ops: WorkspaceOperations, args: dict[str, Any]) -> ToolOutcome:
    rel_path = args.get("path") or "."
    try:
        entries = ops.list_dir(str(rel_path))
    except PathEscapeError as exc:
        return ToolOutcome(ok=False, text=f"ls: {exc.reason}")
    except (FileNotFoundError, NotADirectoryError) as exc:
        return ToolOutcome(ok=False, text=f"ls: {exc}")

    lines = [f"{'d' if is_dir else '-'} {name}" for name, is_dir in entries]
    body_source = "\n".join(lines) if lines else "(empty directory)"
    body, meta = truncate_text(body_source)
    return ToolOutcome(ok=True, text=body, meta=meta)


_TOOL_DISPATCH = {TOOL_READ: tool_read, TOOL_GREP: tool_grep, TOOL_LS: tool_ls}


# ---------------------------------------------------------------------------
# Single veto hook (design.md "Single veto hook as the only policy seam")
# ---------------------------------------------------------------------------


@dataclass
class HarnessBudget:
    """Loop-local budget counters, reusing the R2/R11-R13 stop-reason vocabulary.

    The harness does not invent a second budget system (issue #643 resolved
    question 5): ``budget_exceeded``/``derive_stop_reason`` from
    ``nanobot.runtime.stop_guards`` are reused verbatim, only the cap names
    (``max_tool_calls``) and counters (``tool_calls_used``) are harness-local.
    """

    max_iterations: int = 8
    max_tool_calls: int = 24
    iterations_used: int = 0
    tool_calls_used: int = 0

    def tool_call_stop_reason(self) -> str | None:
        # Checked BEFORE executing the next call (>=, not the after-the-fact
        # "> cap" semantics stop_guards.budget_exceeded uses for cycle-level
        # accounting) so the cap is a hard ceiling on calls actually executed.
        # The stop-reason *name* ("tool_calls") still matches R2/R13's
        # existing budget-cap vocabulary — see _BUDGET_CAP_TO_USAGE.
        if self.tool_calls_used < self.max_tool_calls:
            return None
        return _derive_stop_reason(outcome="", stall=None, budget_exceeded="tool_calls", max_iterations_reached=False)

    def max_iterations_stop_reason(self) -> str | None:
        if self.iterations_used < self.max_iterations:
            return None
        return _derive_stop_reason(outcome="", stall=None, budget_exceeded=None, max_iterations_reached=True)


def before_tool_call(
    name: str,
    args: dict[str, Any],
    *,
    ops: WorkspaceOperations,
    budget: HarnessBudget,
) -> tuple[bool, str | None]:
    """The one policy seam between "model asked for a tool call" and execution.

    Checks (a) the tool-call budget is not exhausted, (b) path confinement.
    Tools themselves stay policy-free. A veto is a reason string, never an
    exception — the caller turns it into a normal tool-result message.
    """
    stop_reason = budget.tool_call_stop_reason()
    if stop_reason:
        return False, f"tool-call budget exhausted ({stop_reason})"

    path_key = _PATH_ARG_BY_TOOL.get(name)
    if path_key:
        raw_path = args.get(path_key)
        if raw_path:
            try:
                ops.resolve(str(raw_path))
            except PathEscapeError as exc:
                return False, exc.reason
    return True, None


# ---------------------------------------------------------------------------
# Journal (state/subagents/tool_calls/<request_id>.jsonl)
# ---------------------------------------------------------------------------


def _append_journal(journal_path: Path, entry: dict[str, Any]) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Turn loop (design.md "Loop shape")
# ---------------------------------------------------------------------------


async def run_harness_loop(
    provider: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    ops: WorkspaceOperations,
    budget: HarnessBudget,
    journal_path: Path,
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Drive the bounded tool-call loop. Never raises for tool/veto failures.

    Returns ``{"stop_reason": str, "messages": [...], "tool_calls_count": int}``.
    """
    stop_reason = STOP_REASON_GATE_CLEAN

    while True:
        max_iter_reason = budget.max_iterations_stop_reason()
        if max_iter_reason:
            stop_reason = max_iter_reason
            break
        tool_budget_reason = budget.tool_call_stop_reason()
        if tool_budget_reason:
            stop_reason = tool_budget_reason
            break

        response = await provider.chat_with_retry(
            messages=messages,
            tools=TOOL_SCHEMAS,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        budget.iterations_used += 1

        if getattr(response, "finish_reason", "") == "error":
            # chat_with_retry never raises: after exhausting retries it
            # returns an LLMResponse(finish_reason="error", content=<error
            # text>) instead. Left unchecked, that response looks like a
            # normal no-tool-call turn and the loop below would break with
            # gate_clean — reporting an LLM outage as a completed run. Break
            # immediately instead, keeping the error text as the final
            # message content so it still reaches the journal/result.
            messages.append({"role": "assistant", "content": response.content or "LLM call failed"})
            stop_reason = STOP_REASON_LLM_ERROR
            break

        if not getattr(response, "has_tool_calls", False):
            # Keep reasoning_content alongside content: the production
            # executor model (un/qwen3.6-27b-mtp) is a thinking model whose
            # visible answer can land in LLMResponse.reasoning_content (the
            # OpenAI-compatible provider mapping in litellm_provider.py
            # already extracts it) instead of, or in addition to, `content`.
            # _final_text() below needs it to avoid reporting empty findings
            # on an otherwise-clean run (#649).
            messages.append({
                "role": "assistant",
                "content": response.content or "",
                "reasoning_content": getattr(response, "reasoning_content", None),
            })
            stop_reason = STOP_REASON_GATE_CLEAN
            break

        messages.append({
            "role": "assistant",
            "content": response.content,
            "tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
        })

        budget_hit_mid_turn: str | None = None
        for tool_call in response.tool_calls:
            name = tool_call.name
            args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}

            allowed, veto_reason = before_tool_call(name, args, ops=ops, budget=budget)
            if allowed:
                budget.tool_calls_used += 1
                fn = _TOOL_DISPATCH.get(name)
                if fn is None:
                    outcome = ToolOutcome(ok=False, text=f"unknown tool: {name}")
                else:
                    try:
                        outcome = fn(ops, args)
                    except Exception as exc:  # tools must never crash the loop
                        outcome = ToolOutcome(ok=False, text=f"{name}: unexpected error: {exc}")
                decision = "allow"
                result_text = outcome.text
                meta = outcome.meta
            else:
                decision = "veto"
                result_text = f"tool call vetoed: {veto_reason}"
                meta = {}

            _append_journal(journal_path, {
                "ts": _utc_iso(),
                "tool": name,
                "args": args,
                "decision": decision,
                "veto_reason": veto_reason if decision == "veto" else None,
                "result_bytes": len(result_text.encode("utf-8", errors="replace")),
                "truncated": bool(meta.get("truncated")) if meta else False,
            })

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_text,
            })

            budget_hit_mid_turn = budget.tool_call_stop_reason()
            if budget_hit_mid_turn:
                break

        if budget_hit_mid_turn:
            stop_reason = budget_hit_mid_turn
            break

    return {
        "stop_reason": stop_reason,
        "messages": messages,
        "tool_calls_count": budget.tool_calls_used,
    }


# Same <think>...</think>-stripping shape as nanobot.agent.loop.Agent._strip_think
# (that class already handles this exact model family in the operator-facing
# path). Duplicated rather than imported: agent.loop pulls in the full agent
# stack (tools, sessions, MCP...), which would be a heavyweight, policy-bearing
# dependency for this policy-free, read-only harness module (#649).
_THINK_TAG_RE = re.compile(r"<think>[\s\S]*?</think>")


def _strip_think(text: str | None) -> str | None:
    if not text:
        return None
    return _THINK_TAG_RE.sub("", text).strip() or None


_NO_FINDINGS_TEXT = "(no final findings text returned by model)"


def _final_text(messages: list[dict[str, Any]]) -> str:
    """Extract the model's final visible findings text.

    Thinking models (the production executor is ``un/qwen3.6-27b-mtp``) can
    put their answer in ``reasoning_content`` instead of ``content``, or
    embed it after a ``<think>...</think>`` block inside ``content`` — found
    live (2026-07-05) as an empty ``stdout`` on an otherwise gate_clean run
    (#649). Fallback chain per assistant message, most recent first:
    visible (think-tag-stripped) ``content`` -> ``reasoning_content`` -> a
    fixed diagnostic placeholder so callers never see a silently-empty
    string on a completed run.
    """
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        visible = _strip_think(msg.get("content"))
        if visible:
            return visible
        reasoning = msg.get("reasoning_content")
        if reasoning:
            stripped_reasoning = _strip_think(str(reasoning))
            return stripped_reasoning or str(reasoning)
        return _NO_FINDINGS_TEXT
    return _NO_FINDINGS_TEXT


# ---------------------------------------------------------------------------
# Sync entrypoint used by nanobot.runtime.subagent_materializer
# ---------------------------------------------------------------------------


def _run_async(coro: Any) -> Any:
    """Run *coro* to completion, safe whether or not a loop is already running.

    ``materialize_subagent_requests`` is called both from plain sync code
    (``nanobot cli``) and from inside an already-running asyncio loop
    (``coordinator.run_self_evolving_cycle``); ``asyncio.run()`` would raise
    in the latter case, so a running loop pushes the harness loop onto its
    own dedicated thread/event loop instead.

    The dedicated thread starts with a fresh ``contextvars.Context`` by
    default, which would silently drop the llm_telemetry call_context set by
    the caller (issue #675); ``copy_context()`` carries it across.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}
    ctx = contextvars.copy_context()

    def _runner() -> None:
        try:
            result_box["result"] = ctx.run(asyncio.run, coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            error_box["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box["result"]


def _harness_system_prompt(workspace_root: Path) -> str:
    return (
        "You are a bounded verification subagent for the eeepc self-evolving "
        "runtime, running with a read-only tool harness (phase 1 of #643). "
        f"Your tools are confined to the workspace root: {workspace_root}. "
        "Use `read`, `grep`, and `ls` to inspect the actual files named in the "
        "task before answering. You cannot edit or execute anything in this "
        "profile — report findings only. Stop calling tools once you have "
        "enough information and answer with your findings as your final "
        "message content."
    )


def _harness_task_prompt(request: dict[str, Any]) -> str:
    title = request.get("task_title") or request.get("title") or request.get("task_id") or "subagent verification task"
    source = request.get("source_artifact") or "source artifact unavailable"
    return (
        f"Task: {title}.\n"
        f"Task id: {request.get('task_id') or request.get('taskId')}.\n"
        f"Cycle id: {request.get('cycle_id') or request.get('cycleId')}.\n"
        f"Source artifact: {source}.\n"
        "Use your read-only tools to inspect the relevant files, then report "
        "concise findings. Do not claim a mutation happened — this profile "
        "cannot mutate files."
    )


def run_tool_harness_request(
    request: dict[str, Any],
    *,
    state_root: Path,
    workspace_root: Path | None = None,
    provider: Any | None = None,
    model: str | None = None,
    config: Any | None = None,
    max_iterations: int | None = None,
    max_tool_calls: int | None = None,
) -> dict[str, Any]:
    """Run the phase-1 read-only harness for one materializer request.

    Returns a dict with ``ok``, ``stdout`` (the model's final findings text),
    ``tool_calls_count``, ``tool_call_journal`` (path), and ``stop_reason`` —
    the fields ``subagent_materializer`` folds into the result artifact per
    issue #643 resolved question 2 (bounded result JSON, full detail lives in
    the journal sidecar).
    """
    request_id = str(request.get("request_id") or request.get("id") or uuid.uuid4().hex)
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in request_id).strip("-") or "request"
    journal_path = Path(state_root) / "subagents" / "tool_calls" / f"{safe_id}.jsonl"

    if config is None:
        from nanobot.config.loader import load_config
        config = load_config()
    subagent_cfg = config.tools.subagent
    resolved_model = (
        model
        or os.environ.get("SUBAGENT_BRIDGE_MODEL", "").strip()
        or getattr(subagent_cfg, "model", None)
        or "un/qwen3.6-27b-mtp"
    )

    if provider is None:
        from nanobot.cli.commands import _make_provider
        config.agents.defaults.model = resolved_model
        _apply_bridge_reasoning_effort(config)  # #832
        provider = _make_provider(config)

    resolved_workspace = workspace_root
    if resolved_workspace is None:
        raw_workspace = request.get("workspace_root")
        resolved_workspace = Path(str(raw_workspace)) if raw_workspace else Path(state_root).parent / "eeebot-self-evolving"
    ops = WorkspaceOperations(resolved_workspace)

    budget = HarnessBudget(
        max_iterations=int(max_iterations or getattr(subagent_cfg, "harness_max_iterations", 8)),
        max_tool_calls=int(max_tool_calls or getattr(subagent_cfg, "harness_max_tool_calls", 24)),
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _harness_system_prompt(ops.root)},
        {"role": "user", "content": _harness_task_prompt(request)},
    ]

    # Issue #675: attribute the harness's LLM calls to this request's cycle.
    cycle_id = str(request.get("cycle_id") or request.get("cycleId") or "")
    with call_context(cycle_id, "tool_harness"):
        result = _run_async(run_harness_loop(
            provider,
            model=resolved_model,
            messages=messages,
            ops=ops,
            budget=budget,
            journal_path=journal_path,
        ))

    return {
        "ok": result["stop_reason"] == STOP_REASON_GATE_CLEAN,
        "stdout": _final_text(result["messages"]),
        "tool_calls_count": result["tool_calls_count"],
        "tool_call_journal": str(journal_path),
        "stop_reason": result["stop_reason"],
    }
