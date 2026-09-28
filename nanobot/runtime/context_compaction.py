"""Context compaction for the subagent loop. #959, #1776, #1930

Reduces the token size of ``messages`` when a running conversation exceeds a
configurable fraction of the model's context window.  The module is stdlib-only
(no provider calls, no third-party imports) and fail-open: every public entry
point returns an unmodified history on any error.

#1930 invariants I1-I5 (docs/changes/1930-compaction-invariants/proposal.md)
-----------------------------------------------------------------------------
A structural guarantee replacing the pre-#1930 heuristic-only design (where
anything the summary heuristic didn't recognize was gone for good):

- **I1 (preservation):** the full pre-compaction text of every message a
  round touches is written, before that message's in-context content is
  replaced, to ``{state_root}/compaction/<cycle_id>/<execution_id>/
  <iteration>.md`` (see :func:`_preservation_path`, :func:`_write_preservation_file`)
  — retrievable byte-for-byte through the real ``ReadFileTool`` (long lines
  are wrapped, see :data:`_PRESERVATION_LINE_WIDTH`) — and referenced by an
  index spliced into the installed carrier (:func:`_preservation_index_lines`).
- **I2 (monotonicity):** a round is journalled ``"compacted"`` only when
  ``after < before`` strictly; equality or worse declines
  (``"declined_no_reduction"``) exactly like "nothing to compact."
- **I3 (one carrier):** at most one message ever carries
  ``_compaction_carrier: True``, set/read only by this module
  (:data:`_CARRIER_FLAG`).
- **I4 (atomicity):** a round either installs a carrier AND shrinks every
  worth-compacting candidate, or changes nothing at all — no half-outcome
  where candidates are excerpted/flagged but no carrier was installed.
- **I5 (unsupported fields incompressible):** a message carrying
  ``thinking_blocks`` is never a compaction candidate, regardless of size —
  left byte-identical, still counted toward the token estimate.

Conditions C (retention: :func:`sweep_preservation_retention`,
:func:`measure_preservation_volume_from_journal`) and E (usage counter:
:func:`count_preservation_reads`) ship alongside I1. Condition D (privacy) is
structural — preservation files live under ``state_root``, formalized on
ADR-036's private-only inputs list; the dashboard-repo canary test proving a
planted marker never reaches a public sink is a separate follow-up in that
repo, not this module.

Char ÷ 4 heuristic
------------------
Token estimation uses ``ceil(len(text) / 4)``.  This is a deliberate
approximation — tight enough for the compaction threshold decision and fast
enough to call on every iteration.  For the default window the heuristic is
conservative (actual token counts are often 20-30 % lower for English prose),
which means compaction fires slightly early rather than late, keeping a
safety margin.

#1776 scope: token-span compaction plus deterministic structural summary
--------------------------------------------------------------------------
The dropped span is replaced with an evidence-only summary: task, explicit
assistant progress/decisions, and paths observed in file calls/results. No
summarization model call is made: that would add latency and queue cost, while
heuristic extraction must not infer unstated intent. Each compaction
re-derives the summary from the messages it is dropping THIS round (not only
the first round in a cycle) and folds the previous summary in verbatim
(bounded — see ``SELFEVO_COMPACT_MAX_SUMMARY_CHARS`` below), so repeated
compaction in one cycle is genuinely cumulative, not a one-shot that goes
back to silent byte-dropping on the second and later passes.

- The cut point is a token span walked back from the newest message, not a
  fixed count of recent messages (:func:`_compactable_indices`) — a single
  oversized recent tool result is no longer permanently protected just for
  being recent.
- Messages of every role before the cut point are compactable, assistant
  turns included — only ``system`` messages and the initial ``user`` task
  are exempt. Compaction only ever replaces a message's ``content``; a
  message's other fields (notably ``tool_calls`` and ``tool_call_id``) are
  never touched, so a tool call and its result are never separated by
  compaction, whichever side of the cut each one falls on.
- Every evaluation is journalled, including declines, each with an explicit
  ``reason`` (:data:`_write_journal`'s ``reason`` field): ``below_threshold``,
  ``nothing_older_than_cut``, ``already_compact``, or ``error``. Before
  #1776 two early returns and the exception handler wrote nothing at all —
  "did not fire" and "did not run" were indistinguishable in the data.
- Each compacted message's journal entry names the tool and a mechanical
  (line/char count, not content-interpreted) characterization of what was
  dropped — before #1776 the journal recorded only aggregate counts, never
  which tool's output was cut or how much of it.
- ``RESERVE_TOKENS`` is now >= the client completion ceiling
  (``AgentDefaults.max_tokens``), and a test pins that inequality so a
  future drift between the two fails CI instead of silently reintroducing
  the #1776 shortfall (8,000 reserved against an 8,192-token ceiling).
- ``WINDOW_TOKENS`` corrected to the measured 98,304-token serving window
  (was 98,000) and is this module's one authoritative definition; a test
  greps the tree for a second hardcoded copy of the literal.

Environment knobs (all optional, applied once at import time)
-------------------------------------------------------------
``SELFEVO_COMPACT_THRESHOLD``   float 0–1  fraction of window that triggers
                                compaction (default 0.8)
``SELFEVO_COMPACT_KEEP_TOKENS`` int ≥ 0    estimated-token span, walked back
                                from the newest message, that is always kept
                                verbatim (default 20 000; #1776 replaces the
                                old message-COUNT ``KEEP_RESULTS`` knob —
                                see the module docstring's #1776 section)
``SELFEVO_COMPACT_WINDOW_TOKENS`` int > 0  total context-window size in tokens
                                used for threshold calculation
                                (default 98 304 — the measured serving
                                window; #1776 corrected this from 98 000)
``SELFEVO_COMPACT_RESERVE_TOKENS`` int ≥ 0  reserve for tool schemas,
                                thinking, and completion (default 8 192 —
                                #1776 raised this from 8 000 to be >= the
                                client completion ceiling,
                                ``AgentDefaults.max_tokens``)
``SELFEVO_COMPACT_EXCERPT_HEAD`` int ≥ 1  chars kept from the start of a
                                compacted tool-result body (default 200)
``SELFEVO_COMPACT_EXCERPT_TAIL`` int ≥ 1  chars kept from the end of a
                                compacted tool-result body (default 200)
``SELFEVO_COMPACT_MAX_SUMMARY_CHARS`` int ≥ 0  soft cap on the structural
                                summary's total size; when a growing summary
                                would exceed it, the embedded prior-summary
                                text is truncated (keeping its most recent
                                tail) so cumulative compaction cannot grow
                                the summary without bound across many
                                compactions in one cycle (default 6 000)

Deny-set
--------
This module is listed in ``_RUNTIME_DENY_ALWAYS_FILES`` in
``nanobot/runtime/runtime_deny.py``.  The instance must never be able to
weaken the compaction logic or remove the deny-set entry itself.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Environment-driven configuration (evaluated once at import time)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


#: Fraction of the context window at which compaction fires (0–1).
THRESHOLD: float = _env_float("SELFEVO_COMPACT_THRESHOLD", 0.8)

#: Estimated-token span (walked back from the newest message) always kept
#: verbatim. #1776: replaces the message-count ``KEEP_RESULTS`` knob — a
#: fixed count of recent messages could never compact a single oversized
#: recent result; a token span can, once that one message alone exceeds it.
KEEP_TOKENS: int = max(0, _env_int("SELFEVO_COMPACT_KEEP_TOKENS", 20_000))

#: Assumed context-window size in tokens. #1776: THE single authoritative
#: definition of the serving window — see ``test_context_compaction.py``'s
#: ``test_window_tokens_has_no_second_hardcoded_copy`` for the drift guard.
#: Corrected from 98_000 to the measured 98,304.
WINDOW_TOKENS: int = max(1, _env_int("SELFEVO_COMPACT_WINDOW_TOKENS", 98_304))

#: Reserved window space for tool schemas, thinking, and completion. #1776:
#: raised from 8_000 to 8_192 so it is >= AgentDefaults.max_tokens (the
#: client completion ceiling) — see
#: ``test_reserve_tokens_covers_the_completion_ceiling``.
RESERVE_TOKENS: int = max(0, _env_int("SELFEVO_COMPACT_RESERVE_TOKENS", 8_192))

#: Characters kept from the head of a compacted tool-result body.
EXCERPT_HEAD: int = max(1, _env_int("SELFEVO_COMPACT_EXCERPT_HEAD", 200))

#: Characters kept from the tail of a compacted tool-result body.
EXCERPT_TAIL: int = max(1, _env_int("SELFEVO_COMPACT_EXCERPT_TAIL", 200))

#: Soft cap on the structural summary's total size; bounds cumulative growth
#: across repeated compactions in one cycle (see module docstring).
MAX_SUMMARY_CHARS: int = max(0, _env_int("SELFEVO_COMPACT_MAX_SUMMARY_CHARS", 6_000))

# Byte marker inserted between head and tail excerpts.
_OMIT_MARKER = "\n[…compacted…]\n"


# ---------------------------------------------------------------------------
# I1 preservation (#1930): full pre-compaction text of every dropped message,
# written to a per-cycle/per-execution file before any in-context mutation.
# ---------------------------------------------------------------------------

#: Physical-line width for a preservation file, comfortably under
#: ``ReadFileTool._MAX_CHARS`` (128_000, nanobot/agent/tools/filesystem.py)
#: plus its own ``"N| "`` line-number prefix -- a single physical line at or
#: near that cap makes the tool's own pagination unable to advance past it
#: (see ``test_preservation_line_width_fits_under_read_file_tool_cap``).
_PRESERVATION_LINE_WIDTH = 100_000

#: Readability-only suffix marking a physical line as continued on the next.
#: Reconstruction is POSITIONAL (fixed-width chunks), never marker-search
#: based, so this string coinciding with real content is harmless.
_PRESERVATION_CONTINUATION_MARKER = "↵"

#: cycle_id/execution_id arrive here verbatim from the request/spawn call
#: (bridge.py never sanitizes either) -- restrict both to a safe
#: single-path-component charset before they are ever used to build a path.
_SAFE_PATH_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_path_component(value: str, fallback: str) -> str:
    """A safe, single-path-component version of ``value`` -- never contains
    ``/``, ``\\``, or ``..`` -- for use as one segment of a filesystem path.
    Falls back to ``fallback`` when nothing safe remains (empty, all
    separators/dots)."""
    cleaned = _SAFE_PATH_COMPONENT_RE.sub("_", str(value or "")).strip("_.")
    return cleaned or fallback


def _is_under(path: Path, directory: Path) -> bool:
    """True iff ``path`` resolves to somewhere inside ``directory``."""
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _preservation_path(
    state_root: "Path | str", cycle_id: str, execution_id: "str | None", iteration: int,
) -> Path:
    """The per-call preservation file path (I1):
    ``{state_root}/compaction/<cycle_id>/<execution_id>/<iteration>.md`` --
    both id segments restricted to a safe single-path-component charset, and
    the fully joined path checked to resolve inside
    ``{state_root}/compaction`` before any write, never interpolated raw.
    Raises ``ValueError`` on a containment failure (the caller's existing
    fail-open contract turns that into a declined/errored round, never a
    write outside the intended root)."""
    root = (Path(state_root) / "compaction").resolve()
    safe_cycle = _sanitize_path_component(cycle_id, "unknown-cycle")
    safe_execution = _sanitize_path_component(execution_id or "", "unknown-execution")
    path = root / safe_cycle / safe_execution / f"{int(iteration)}.md"
    if not _is_under(path.parent, root):
        raise ValueError(f"preservation path escaped compaction root: {path}")
    return path


def _wrap_preservation_payload(encoded: str) -> list[str]:
    """Split one already-escaped (no raw newline) payload into fixed-width
    physical lines, each but the last carrying a readability-only
    continuation suffix. Reconstruction is positional: concatenate the
    lines in order with the suffix stripped from every line but the last."""
    width = _PRESERVATION_LINE_WIDTH
    if not encoded:
        return [""]
    lines = []
    for i in range(0, len(encoded), width):
        chunk = encoded[i:i + width]
        is_last = i + width >= len(encoded)
        lines.append(chunk if is_last else chunk + _PRESERVATION_CONTINUATION_MARKER)
    return lines


def _preservation_record_lines(index: int, role: str, text: str) -> list[str]:
    """Lines for one message's preservation record: a header naming the
    message, the JSON-encoded (so no raw newline needs escaping -- the
    encoding alone makes the payload one logical line before wrapping) full
    text wrapped to :data:`_PRESERVATION_LINE_WIDTH`, and a footer."""
    lines = [f"## message {index} role={role} chars={len(text)}"]
    lines.extend(_wrap_preservation_payload(json.dumps(text)))
    lines.append("## end")
    return lines


def _write_preservation_file(path: Path, records: "list[tuple[int, str, str]]") -> None:
    """Write every ``(index, role, text)`` record to ``path`` atomically:
    ``<path>.tmp``, ``fsync``, then ``os.replace`` -- an existing file (from a
    retried call) is fully replaced or untouched, never partially written.
    Raises on any I/O error; the caller's fail-open contract handles it, so
    no message is ever returned mutated without this call having succeeded
    first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, role, text in records:
        lines.extend(_preservation_record_lines(index, role, text))
    content = "\n".join(lines) + "\n"
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def _preservation_index_lines(
    records: "list[tuple[int, str, str]]", path: Path,
) -> list[str]:
    """One line per preserved message (I1's carrier index): turn index, role,
    first line (<=80 chars), size, and the path where the full text lives --
    stating explicitly that the complete text is retrievable from there."""
    lines = []
    for index, role, text in records:
        first_line = text.splitlines()[0] if text else ""
        lines.append(
            f"- turn {index} ({role}): {first_line[:80]!r} ({len(text)} chars) "
            f"-- full text preserved at {path}"
        )
    return lines


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Estimate token count using the chars÷4 heuristic (ceil)."""
    return math.ceil(len(text) / 4)


def _message_tokens(msg: dict[str, Any]) -> int:
    """Estimate tokens for a single message dict, #1930: including
    ``reasoning_content``/``thinking_blocks`` — see :func:`_message_text`."""
    content = msg.get("content") or ""
    if isinstance(content, list):
        # Anthropic-style block list
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += _estimate_tokens(str(block.get("text") or block.get("content") or ""))
            else:
                total += _estimate_tokens(str(block))
    else:
        total = _estimate_tokens(str(content))
    reasoning = msg.get("reasoning_content")
    if reasoning:
        total += _estimate_tokens(str(reasoning))
    thinking_text = _thinking_blocks_text(msg.get("thinking_blocks"))
    if thinking_text:
        total += _estimate_tokens(thinking_text)
    return total


def _total_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_message_tokens(m) for m in messages)


# ---------------------------------------------------------------------------
# Compaction helpers
# ---------------------------------------------------------------------------

def _is_system_or_user_task(msg: dict[str, Any]) -> bool:
    """True for system prompts and the initial user task message."""
    return msg.get("role") in ("system", "user")


def _compactable_indices(messages: list[dict[str, Any]], keep_tokens: int) -> set[int]:
    """#1776: token-span cut point, replacing the old fixed recent-message
    count. Walk back from the newest message accumulating estimated tokens;
    the first message whose inclusion would push the running total past
    ``keep_tokens`` is excluded from the protected span (and so is
    everything before it) — including when that single message alone
    already exceeds ``keep_tokens``. That is deliberate: a fixed message
    COUNT can never compact an oversized recent result no matter how large
    it is; a token span can, because the span itself has a size. Do NOT
    reassign ``keep_from`` to the breaking index: leaving it at its last
    successfully-accumulated value is what puts the breaking message itself
    into the unprotected span below — an oversized message anywhere in the
    walk (not only the newest one) is compactable this way, with no special
    case needed for "the last message happens to be huge".

    Messages of every role in the unprotected (older) span are candidates,
    except ``system`` and the initial ``user`` task — assistant turns are
    no longer exempt (#1776 item 3). Only a message's ``content`` is ever
    replaced by the caller; ``tool_calls``/``tool_call_id`` and message
    order/count are untouched, so a tool call and its result are never
    separated by this, regardless of which side of the cut either lands on.
    """
    cumulative = 0
    keep_from = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        tokens = _message_tokens(messages[i])
        if cumulative + tokens > keep_tokens:
            break
        cumulative += tokens
        keep_from = i
    return {i for i in range(keep_from) if not _is_system_or_user_task(messages[i])}


def _thinking_blocks_text(blocks: list[dict[str, Any]] | None) -> str:
    """Flatten Anthropic-style ``thinking_blocks`` into plain text, #1930
    (Codex 4115692517): a ``redacted_thinking`` block's payload lives under
    ``data``, not ``thinking``/``text``/``content`` -- omitting it means
    that block contributes zero to the token estimate even though its
    payload is real and gets resent on every later call."""
    if not blocks:
        return ""
    return "\n".join(
        str(block.get("thinking") or block.get("text") or block.get("content")
            or block.get("data") or "")
        if isinstance(block, dict) else str(block)
        for block in blocks
    )


def _message_text(msg: dict[str, Any]) -> str:
    """Every message's visible text plus, per #1930, its reasoning payload:
    ``build_assistant_message`` (nanobot/utils/helpers.py) stores model
    ``reasoning_content``/``thinking_blocks`` on assistant turns, and
    ``litellm_provider.py`` sends ``reasoning_content`` back to the model on
    every later call. Left out of this, compaction neither counts nor
    shrinks the fastest-growing part of a thinking model's context.
    """
    content = msg.get("content") or ""
    if isinstance(content, list):
        text = "\n".join(
            str(block.get("text") or block.get("content") or "")
            if isinstance(block, dict) else str(block)
            for block in content
        )
    else:
        text = str(content)
    parts = [text] if text else []
    reasoning = msg.get("reasoning_content")
    if reasoning:
        parts.append(str(reasoning))
    thinking_text = _thinking_blocks_text(msg.get("thinking_blocks"))
    if thinking_text:
        parts.append(thinking_text)
    return "\n".join(parts)


_PATH_RE = re.compile(
    r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}(?![\w.-])"
)


def _representative_excerpt(text: str, limit: int) -> str:
    """Retain representative text from the head, middle, and tail."""
    if len(text) <= limit:
        return text
    marker = "\n[…middle omitted…]\n"
    remaining = max(0, limit - len(marker))
    head = remaining // 3
    tail = remaining // 3
    middle = remaining - head - tail
    midpoint = len(text) // 2
    return (
        text[:head] + marker
        + text[max(head, midpoint - middle // 2): midpoint + (middle + 1) // 2]
        + marker + text[-tail:]
    )


def _bounded_summary(text: str, limit: int) -> str:
    """Enforce one hard character cap over the complete summary payload."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n... [summary truncated] ...\n"
    if limit <= len(marker):
        return text[:limit]
    remaining = limit - len(marker)
    head = remaining // 2
    tail = remaining - head
    return text[:head] + marker + text[-tail:]


def _structural_summary(
    evidence_span: list[dict[str, Any]], *, goal: str = "", previous: str = "",
) -> str:
    """Summarize explicit evidence from THIS round's dropped span; infer no
    intent. ``evidence_span`` is the messages being newly compacted this
    call — not the whole history — so a second (or later) compaction reads
    fresh evidence, not a re-scan of content already folded into
    ``previous``. #1776's cumulative requirement: ``previous`` (the prior
    round's full summary text, if any) is embedded verbatim, bounded by
    ``MAX_SUMMARY_CHARS`` so repeated compaction in a long cycle can't grow
    the summary without bound.
    """
    progress: list[str] = []
    decisions: list[str] = []
    read_files: set[str] = set()
    modified_files: set[str] = set()
    for msg in evidence_span:
        text = _message_text(msg).strip()
        if not text or text.startswith("[Compaction summary"):
            continue
        role = msg.get("role")
        if role == "assistant":
            if "decision:" in text.lower() or "we will " in text.lower():
                # Capture the matched decision phrase rather than an
                # unrelated prefix that can hide the actual choice.
                decision_match = re.search(r"decision:|we will ", text, re.IGNORECASE)
                decision_start = decision_match.start() if decision_match else 0
                decisions.append(
                    _representative_excerpt(text[decision_start:], 500)
                )
            else:
                # Assistant messages often carry both progress narration and
                # tool_calls. The calls are metadata; do not discard the
                # accompanying explicit text merely because calls are present.
                # Keep a head, a bounded middle window, and the tail; the
                # progress evidence may be anywhere in a long assistant turn.
                progress.append(_representative_excerpt(text, 1_200))
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                args = fn.get("arguments") if isinstance(fn, dict) else None
                if isinstance(args, str):
                    fn_name = str(fn.get("name", "")).lower() if isinstance(fn, dict) else ""
                    bucket = modified_files if any(w in fn_name for w in ("write", "edit", "patch")) else read_files
                    for path in _PATH_RE.findall(args):
                        bucket.add(path)
        elif role == "tool":
            name = str(msg.get("name") or "").lower()
            bucket = modified_files if any(w in name for w in ("write", "edit", "patch")) else read_files
            for path in _PATH_RE.findall(text):
                bucket.add(path)

    if previous:
        budget = max(0, MAX_SUMMARY_CHARS - 2_000)
        if len(previous) > budget:
            previous = "...[earlier summary truncated]...\n" + previous[-budget:]

    # Reserve space for current-round decisions and progress BEFORE laying
    # out older cumulative context or unbounded path inventories. Those fresh
    # signals must not be cut away by the final whole-summary cap.
    fresh_sections = ["Key decisions (explicit statements only):"]
    fresh_sections.extend(f"- {x}" for x in decisions[-6:])
    fresh_sections.append("Progress:")
    fresh_sections.extend(f"- {x}" for x in progress[-6:])
    fresh_text = _bounded_summary("\n".join(fresh_sections), MAX_SUMMARY_CHARS)

    header_line = "[Compaction summary — deterministic, evidence-only]"
    prefix = [header_line, f"Goal: {goal[:1000] or 'not available'}"]
    if previous:
        prefix.extend(["Earlier summary (preserved):", previous])
    prefix.append("Files read (paths observed in tool calls/results):")
    prefix.extend(f"- {x}" for x in sorted(read_files))
    prefix.append("Files modified (paths observed in write/edit/patch tools):")
    prefix.extend(f"- {x}" for x in sorted(modified_files))
    prefix_text = "\n".join(prefix)
    if len(prefix_text) + len(fresh_text) <= MAX_SUMMARY_CHARS:
        return prefix_text + "\n" + fresh_text if prefix_text else fresh_text

    # #1930 (Codex 4115719536): the header marker must survive truncation --
    # both "is this message the installed carrier" and "is this an earlier
    # carrier" key off the returned text starting with `header_line`. When
    # this round's fresh decisions/progress alone are close to
    # MAX_SUMMARY_CHARS, budgeting the rest of the prefix (goal/previous/
    # files) as "whatever's left" could previously drive that share to zero
    # and, with it, the header itself -- silently turning the installed
    # carrier into a message compaction can no longer recognise as one.
    # Reserve the header's own length first; only what remains is split
    # between the rest of the prefix and this round's fresh evidence
    # (which still gets first claim on that remaining budget).
    separator = "\n"
    rest_of_prefix = "\n".join(prefix[1:])
    fixed_overhead = len(header_line) + 2 * len(separator)
    usable = max(0, MAX_SUMMARY_CHARS - fixed_overhead)
    fresh_budget = min(len(fresh_text), usable)
    prefix_budget = max(0, usable - fresh_budget)
    bounded_fresh = _bounded_summary(fresh_text, fresh_budget)
    bounded_rest = _bounded_summary(rest_of_prefix, prefix_budget)
    return header_line + separator + bounded_rest + separator + bounded_fresh


def _compact_content(content: str) -> str:
    """Legacy bounded excerpt helper retained for compatibility/tests."""
    min_length = EXCERPT_HEAD + len(_OMIT_MARKER) + EXCERPT_TAIL
    if len(content) <= min_length:
        return content
    return content[:EXCERPT_HEAD] + _OMIT_MARKER + content[-EXCERPT_TAIL:]


def _drop_detail(tool_name: str, original: str) -> dict[str, Any]:
    """#1776: a mechanical (never content-interpreted) characterization of
    what a compaction dropped — the tool name, the char counts, and the
    line count of the dropped middle span. This is deliberately NOT the
    same thing as :func:`_structural_summary` (which does interpret
    content, as evidence); this one only answers "which tool, how much,
    roughly what shape" so the journal stops being silent about WHAT was
    cut, only how much.
    """
    dropped_start = min(EXCERPT_HEAD, len(original))
    dropped_end = max(dropped_start, len(original) - EXCERPT_TAIL)
    dropped = original[dropped_start:dropped_end]
    dropped_lines = dropped.count("\n") + (1 if dropped else 0)
    name = tool_name or "unknown"
    return {
        "tool_name": name,
        "chars_before": len(original),
        "chars_after": EXCERPT_HEAD + len(_OMIT_MARKER) + EXCERPT_TAIL,
        "dropped_chars": len(dropped),
        "dropped_summary": f"{dropped_lines} line(s), {len(dropped)} char(s) dropped from {name} output",
    }


def _min_compact_len() -> int:
    return EXCERPT_HEAD + len(_OMIT_MARKER) + EXCERPT_TAIL


# #1930 (Codex 4115719563): compaction state lives on flags the code itself
# sets on the message dict -- never inferred from the message's own text.
# Both `_ALLOWED_MSG_KEYS`/`_ANTHROPIC_EXTRA_KEYS` in litellm_provider.py
# strip any key not on their allowlist before a request is sent, so these
# never reach a provider.
_COMPACTED_FLAG = "_compaction_touched"
_CARRIER_FLAG = "_compaction_carrier"


def _already_marked(msg: dict[str, Any]) -> bool:
    """True once THIS code has already compacted ``msg`` -- carrier install,
    plain excerpt, or retirement placeholder. Keyed on :data:`_COMPACTED_FLAG`,
    not on text: a tool result can legitimately contain the literal string
    "[Compaction summary" or the excerpt marker (this very module's own
    source is one example an agent could `cat`), and matching on that text
    would wrongly treat such a result as already compacted -- silently
    disabling space recovery for it and anything counted alongside it.
    Used to make repeat compaction idempotent (#1776: a second call with no
    new content must not re-touch, or re-grow, what an earlier call already
    produced)."""
    return bool(msg.get(_COMPACTED_FLAG))


def _worth_compacting(msg: dict[str, Any]) -> bool:
    """A candidate is only physically replaced if doing so actually shrinks
    it and it hasn't already been compacted — otherwise compacting a
    trivially small or already-compacted message would only grow it.

    #1930: a message carrying ``thinking_blocks`` is never a candidate here,
    regardless of size. Only ``reasoning_content`` is supported for
    shrinking/clearing in this runtime (the host's provider, qwen via
    LiteLLM, is the only one observed to populate it — 590/637 prompts).
    Anthropic's extended-thinking forms (``thinking_blocks``, including
    ``redacted_thinking``) are not: a signed thinking block that
    accompanies a `tool_calls` turn must be replayed unmodified on the next
    request or the provider can reject the call, and this module has no
    logic that preserves that constraint while shrinking. Fail-safe: such a
    message is left completely untouched by compaction (still counted
    toward the token estimate via ``_message_tokens`` -- see
    :func:`_thinking_blocks_text` -- as incompressible, never itself
    shrunk)."""
    if msg.get("thinking_blocks"):
        return False
    text = _message_text(msg)
    return bool(text) and not _already_marked(msg) and len(text) > _min_compact_len()


def _excerpt_content(content: Any) -> tuple[Any, bool]:
    """Head/tail-excerpt ``content``, preserving Anthropic block-list shape
    when present. Returns ``(new_content, changed)``."""
    if isinstance(content, list):
        changed = False
        new_blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                inner = block.get("content") or ""
                if isinstance(inner, str):
                    compacted = _compact_content(inner)
                    if compacted != inner:
                        changed = True
                        new_blocks.append(dict(block, content=compacted))
                        continue
                new_blocks.append(block)
            else:
                new_blocks.append(block)
        return new_blocks, changed
    text = str(content or "")
    compacted = _compact_content(text)
    return compacted, compacted != text


# ---------------------------------------------------------------------------
# Journal helper
# ---------------------------------------------------------------------------

def _write_journal(
    state_root: Path | str,
    cycle_id: str,
    iteration: int,
    before_tokens: int,
    after_tokens: int,
    results_compacted: int,
    *,
    reason: str,
    real_prev_prompt: int | None = None,
    compacted_details: "list[dict[str, Any]] | None" = None,
) -> None:
    """Append one JSONL event to state_root/compaction/journal.jsonl.

    #1776: called for EVERY evaluation, not only a successful compaction —
    ``reason`` says which of the possible outcomes this row is
    (``compacted``, ``below_threshold``, ``nothing_older_than_cut``,
    ``already_compact``, or ``error``), so "did not fire" and "did not run"
    are distinguishable in the data for the first time. ``compacted_details``
    (only non-empty when ``reason == "compacted"``) names, per compacted
    message, the tool and a mechanical characterization of what was dropped.

    Fail-open: any I/O error is silently swallowed — this must never be the
    reason a cycle fails, including when it is reporting that compaction
    itself failed.
    """
    try:
        journal_dir = Path(state_root) / "compaction"
        journal_dir.mkdir(parents=True, exist_ok=True)
        journal_path = journal_dir / "journal.jsonl"
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cycle_id": cycle_id,
            "iteration": iteration,
            "reason": reason,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "results_compacted": results_compacted,
            "tokens_before_est": before_tokens,
            "real_prev_prompt": real_prev_prompt,
            "tokens_after_est": after_tokens,
            "messages_trimmed": results_compacted,
            "compacted_details": compacted_details or [],
        }
        with journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compact_messages(
    messages: list[dict[str, Any]],
    cycle_id: str,
    iteration: int,
    state_root: Path | str,
    *,
    threshold: float | None = None,
    keep_tokens: int | None = None,
    window_tokens: int | None = None,
    prompt_tokens: int | None = None,
    prompt_token_delta: int = 0,
    reserve_tokens: int | None = None,
    execution_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compact ``messages`` if their estimated token count exceeds the threshold.

    Protected messages (never compacted):
    - All ``system`` and ``user`` messages (system prompt + initial task).
    - Every message within ``keep_tokens`` estimated tokens of the newest
      message (#1776: a token span, not a fixed count — see
      :func:`_compactable_indices`).

    Messages of every other role before the cut point are candidates,
    assistant turns included (#1776 item 3). Among this round's candidates,
    the first one whose content is long enough to be worth compacting
    (:func:`_worth_compacting`) becomes the summary carrier: its ``content``
    is replaced by a fresh :func:`_structural_summary` — built from this
    round's newly-dropped evidence plus the previous summary, if any,
    carried forward — followed by its own bounded head/tail excerpt. Any
    remaining worth-compacting candidates this round get the plain
    head/tail excerpt. A candidate that is already trivially short, or was
    already compacted by an earlier call, is left untouched, so a repeat
    call with no new content is a no-op (``reason="already_compact"``).

    Parameters
    ----------
    messages:
        The current conversation history (mutated copy is returned).
    cycle_id:
        Bridge cycle identifier — written to the journal event.
    iteration:
        Current subagent loop iteration — written to the journal event.
    state_root:
        Runtime state directory; journal written under
        ``{state_root}/compaction/journal.jsonl``.
    threshold:
        Override ``THRESHOLD`` for this call (used in tests).
    keep_tokens:
        Override ``KEEP_TOKENS`` for this call (used in tests).
    window_tokens:
        Override ``WINDOW_TOKENS`` for this call (used in tests).
    prompt_tokens:
        Provider-reported prompt tokens from the previous response.
    prompt_token_delta:
        Estimated tokens appended since that provider measurement.
    reserve_tokens:
        Override ``RESERVE_TOKENS`` for this call (used in tests).
    execution_id:
        The executing task's own id (e.g. a spawned subagent's task id) --
        #1930/I1: ``cycle_id`` alone is not unique (a repeated cycle, or two
        concurrent executions sharing a state root, can share it), so a
        preservation file is namespaced by both. ``None``/absent falls back
        to a fixed placeholder segment (legacy callers, tests).

    Returns
    -------
    list[dict]
        The (possibly compacted) message list.  Always returns a valid list;
        returns the original ``messages`` unchanged on any error or decline.
    """
    try:
        _threshold = threshold if threshold is not None else THRESHOLD
        _keep_tokens = keep_tokens if keep_tokens is not None else KEEP_TOKENS
        _window = window_tokens if window_tokens is not None else WINDOW_TOKENS

        before_tokens = _total_tokens(messages)
        _reserve = reserve_tokens if reserve_tokens is not None else RESERVE_TOKENS
        trigger_tokens = max(1, math.ceil(_threshold * max(1, _window - _reserve)))
        real_prompt = prompt_tokens if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool) and prompt_tokens >= 0 else None
        delta = prompt_token_delta if isinstance(prompt_token_delta, int) and not isinstance(prompt_token_delta, bool) else 0
        trigger_signal = real_prompt + max(0, delta) if real_prompt is not None else before_tokens

        if trigger_signal < trigger_tokens:
            _write_journal(
                state_root, cycle_id, iteration, trigger_signal, trigger_signal, 0,
                reason="below_threshold", real_prev_prompt=real_prompt,
            )
            return messages

        # #1776: token-span cut point (see _compactable_indices) replaces
        # the old fixed-count "last _keep tool results" rule.
        compactable = _compactable_indices(messages, _keep_tokens)

        if not compactable:
            reason = "nothing_older_than_cut"
            _write_journal(
                state_root, cycle_id, iteration, trigger_signal, trigger_signal, 0,
                reason=reason, real_prev_prompt=real_prompt,
            )
            return messages

        sorted_candidates = sorted(compactable)
        worth_indices = [i for i in sorted_candidates if _worth_compacting(messages[i])]

        if not worth_indices:
            # Every candidate is either already compacted or too small to
            # be worth touching — a genuine no-op, distinct from
            # "nothing_older_than_cut" (there WAS something past the cut,
            # it just doesn't need changing).
            _write_journal(
                state_root, cycle_id, iteration, trigger_signal, trigger_signal, 0,
                reason="already_compact", real_prev_prompt=real_prompt,
            )
            return messages

        # #1776: cumulative summary. `previous` is whichever earlier-round
        # carrier text still exists anywhere in history (there is at most
        # one, flagged with `_CARRIER_FLAG` -- #1930, never inferred from
        # text). `evidence_span` covers the WHOLE unprotected span (not only
        # worth_indices) so a short-but-fresh message — e.g. a one-line
        # "decision: ..." too small to be worth excerpting on its own —
        # still contributes evidence, while messages already compacted by
        # an earlier round (recognisable by `_already_marked`) are excluded
        # so their content isn't re-scanned as if it were new.
        previous = "\n".join(
            _message_text(m) for m in messages if m.get(_CARRIER_FLAG)
        )
        evidence_span = [
            messages[i] for i in sorted_candidates
            if not _already_marked(messages[i])
        ]
        goal = _message_text(messages[1]) if len(messages) > 1 else ""
        summary = _structural_summary(evidence_span, goal=goal, previous=previous)

        # #1930 (Codex 4113995954): try every worth-compacting candidate for
        # the refreshed summary, not just the first. A small first candidate
        # can be too small to hold it (the guard below then falls back to a
        # bare excerpt) even when a later, larger candidate easily could --
        # discarding this round's fresh evidence for no reason. The first
        # candidate whose combined (summary + its own excerpt) is smaller
        # than its own original text becomes the carrier; if none qualify,
        # no carrier is installed this round (every candidate gets a plain
        # excerpt, same as before).
        carrier: int | None = None
        carrier_content: str | None = None
        for i in worth_indices:
            msg = messages[i]
            old_text = _message_text(msg)
            excerpt, _ = _excerpt_content(msg.get("content"))
            excerpt_text = excerpt if isinstance(excerpt, str) else _message_text(dict(msg, content=excerpt))
            candidate_content = f"{summary}\n{excerpt_text}"
            # A summary is useful only if adding it does not defeat
            # compaction. If it would grow this carrier, another candidate
            # (or none) must carry it instead.
            if len(candidate_content) < len(old_text):
                carrier = i
                carrier_content = candidate_content
                break

        # I4 (#1930, Codex 4115904095): "installs a carrier AND shrinks every
        # candidate, or changes nothing at all" -- no half-outcome. When no
        # candidate can hold the refreshed summary without growing, the whole
        # round declines here, before anything is touched: no candidate is
        # excerpted, no message is flagged, and this round's fresh evidence
        # is simply re-derived (from the still-untouched messages) the next
        # time compaction runs, rather than being discarded with nothing to
        # show for it. Shares I2's decline reason -- this is the same
        # "would not actually buy back anything" outcome.
        if carrier is None:
            _write_journal(
                state_root, cycle_id, iteration, trigger_signal, trigger_signal, 0,
                reason="declined_no_reduction", real_prev_prompt=real_prompt,
            )
            return messages

        new_messages = list(messages)
        results_compacted = 0
        compacted_details: list[dict[str, Any]] = []
        # I1: the full pre-compaction text of every candidate this round
        # touches, captured BEFORE any mutation below -- the sole input to
        # the preservation write further down. Nothing here is written to
        # disk or returned to the caller yet: everything up to the I2 check
        # is a purely local, side-effect-free tentative build (`messages`
        # and its message dicts are never mutated in place), so this whole
        # candidate can still be discarded intact if I2's strict check fails.
        preservation_records: list[tuple[int, str, str]] = []
        for i in worth_indices:
            msg = messages[i]
            old_text = _message_text(msg)
            preservation_records.append((i, str(msg.get("role") or ""), old_text))
            tool_name = str(msg.get("name") or "tool")
            if i == carrier:
                new_content = carrier_content
            else:
                new_content, _ = _excerpt_content(msg.get("content"))
            new_msg = dict(msg, content=new_content)
            # #1930: an old turn's reasoning payload is exactly as compacted
            # as its content — leaving it in place would keep resending the
            # fastest-growing part of a thinking model's context untouched
            # (litellm_provider.py sends reasoning_content back on every
            # later call). Messages carrying thinking_blocks never reach
            # this loop at all (_worth_compacting excludes them).
            new_msg.pop("reasoning_content", None)
            new_msg[_COMPACTED_FLAG] = True
            new_msg[_CARRIER_FLAG] = (i == carrier)
            new_messages[i] = new_msg
            compacted_details.append(_drop_detail(tool_name, old_text))
            results_compacted += 1

        # Retire the prior carrier now that a new summary has been installed
        # (carrier is guaranteed not-None past the I4 guard above).
        for i, msg in enumerate(messages):
            if i != carrier and msg.get(_CARRIER_FLAG):
                retired = dict(
                    msg, content="[Earlier compaction summary incorporated below.]"
                )
                retired[_COMPACTED_FLAG] = True
                retired[_CARRIER_FLAG] = False
                new_messages[i] = retired

        # I1/B: splice the preservation index into the installed carrier's
        # own content BEFORE measuring `after_tokens` -- the index is real
        # returned-history bytes and must count against the same budget I2
        # enforces, not be a free addition outside it. Path computation has
        # no I/O (pure string/containment logic); the actual write happens
        # only once I2 confirms this round is worth keeping, below.
        preservation_path = _preservation_path(state_root, cycle_id, execution_id, iteration)
        index_lines = _preservation_index_lines(preservation_records, preservation_path)
        carrier_with_index = new_messages[carrier]
        new_messages[carrier] = dict(
            carrier_with_index,
            content=str(carrier_with_index["content"]) + "\n"
            + "\n".join(["Preserved evidence (full original text, read via read_file):", *index_lines]),
        )

        after_tokens = _total_tokens(new_messages)

        # I2: strict monotonicity. Equality (or worse) is not a compaction --
        # it spent carrier/index overhead without buying back any headroom,
        # while still marking its candidates touched and excluding their
        # evidence from later rounds (I4's flag-based marking) -- so it must
        # not be allowed to pass as a success. Declines exactly like I4's
        # "no candidate fits" case: nothing is returned mutated, nothing is
        # flagged, and I1 writes nothing for a round that never happened.
        if after_tokens >= before_tokens:
            _write_journal(
                state_root, cycle_id, iteration, trigger_signal, trigger_signal, 0,
                reason="declined_no_reduction", real_prev_prompt=real_prompt,
            )
            return messages

        # I1: write the FULL pre-compaction text of every touched message to
        # its per-cycle/per-execution file BEFORE returning the mutated
        # history. Any failure here (I/O error, path containment failure)
        # propagates to the outer fail-open handler below -- so no message is
        # ever returned mutated without an on-disk copy of what it is about
        # to lose already having succeeded.
        _write_preservation_file(preservation_path, preservation_records)

        _write_journal(
            state_root,
            cycle_id,
            iteration,
            trigger_signal,
            after_tokens,
            results_compacted,
            reason="compacted",
            real_prev_prompt=real_prompt,
            compacted_details=compacted_details,
        )
        return new_messages

    except Exception as exc:  # noqa: BLE001
        # Fail-open: return original history untouched, but make the
        # failure visible (#1776 item 6) rather than silent.
        _write_journal(
            state_root, cycle_id, iteration, 0, 0, 0,
            reason=f"error:{type(exc).__name__}",
        )
        return messages


# ---------------------------------------------------------------------------
# Condition C (#1930): retention/rotation for I1's preservation files.
# ---------------------------------------------------------------------------

#: Default total-size cap across all cycles' preservation files. No
#: production volume measurement exists yet -- see
#: :func:`measure_preservation_volume_from_journal`, condition C's own "first
#: implementation step" -- so this is a conservative placeholder pending a
#: real measurement, not a value derived from observed usage.
_DEFAULT_PRESERVATION_MAX_TOTAL_BYTES = 500 * 1024 * 1024

_DEFAULT_PRESERVATION_MAX_AGE_DAYS = 7


def measure_preservation_volume_from_journal(state_root: "Path | str") -> dict[str, dict[str, int]]:
    """Condition C's "first implementation step": total
    ``compacted_details[].dropped_chars`` (the exact character count of each
    dropped middle span -- a much closer proxy for on-disk preservation bytes
    than a net *token* delta) from the existing journal, summed per day
    (``ts``'s date) and per ``cycle_id``. Sizes the retention cap in
    :func:`sweep_preservation_retention` from real usage instead of a guess.
    Fail-open: an unreadable/missing/malformed journal yields ``{}``."""
    result: dict[str, dict[str, int]] = {"by_day": {}, "by_cycle": {}}
    try:
        journal_path = Path(state_root) / "compaction" / "journal.jsonl"
        if not journal_path.is_file():
            return result
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            details = event.get("compacted_details") or []
            if not isinstance(details, list):
                continue
            dropped = sum(
                int(d.get("dropped_chars") or 0) for d in details if isinstance(d, dict)
            )
            if not dropped:
                continue
            day = str(event.get("ts") or "")[:10]
            cycle = str(event.get("cycle_id") or "")
            if day:
                result["by_day"][day] = result["by_day"].get(day, 0) + dropped
            if cycle:
                result["by_cycle"][cycle] = result["by_cycle"].get(cycle, 0) + dropped
    except Exception:
        pass
    return result


def sweep_preservation_retention(
    state_root: "Path | str",
    *,
    active_cycle_id: "str | None" = None,
    max_age_days: int = _DEFAULT_PRESERVATION_MAX_AGE_DAYS,
    max_total_bytes: int = _DEFAULT_PRESERVATION_MAX_TOTAL_BYTES,
) -> dict[str, int]:
    """Bound I1's preservation directory (#1930 condition C): run once per
    cycle start.

    1. Age sweep: every cycle dir directly under ``{state_root}/compaction/``
       other than ``active_cycle_id`` whose newest file's mtime is older than
       ``max_age_days`` is deleted whole.
    2. Size cap: across whatever cycle dirs remain (again, never
       ``active_cycle_id``), oldest-mtime-first, delete entire cycle dirs
       until the total size of what's left is at or under
       ``max_total_bytes``.

    ``active_cycle_id``'s own directory is never touched by either pass, so
    a currently-running cycle's own preservation files are always safe.
    Fail-open: returns ``{"aged_out": 0, "size_capped": 0}`` (or partial
    counts) on any error, never raises.
    """
    counts = {"aged_out": 0, "size_capped": 0}
    try:
        compaction_root = Path(state_root) / "compaction"
        if not compaction_root.is_dir():
            return counts
        safe_active = (
            _sanitize_path_component(active_cycle_id, "") if active_cycle_id else ""
        )

        def _newest_mtime(cycle_dir: Path) -> float:
            newest = 0.0
            for f in cycle_dir.rglob("*"):
                if f.is_file():
                    try:
                        newest = max(newest, f.stat().st_mtime)
                    except OSError:
                        continue
            return newest

        def _dir_size(cycle_dir: Path) -> int:
            total = 0
            for f in cycle_dir.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        continue
            return total

        cycle_dirs = [d for d in compaction_root.iterdir() if d.is_dir()]
        cutoff = time.time() - max(0, max_age_days) * 86400
        remaining: list[Path] = []
        for cycle_dir in cycle_dirs:
            if safe_active and cycle_dir.name == safe_active:
                remaining.append(cycle_dir)
                continue
            if _newest_mtime(cycle_dir) < cutoff:
                shutil.rmtree(cycle_dir, ignore_errors=True)
                counts["aged_out"] += 1
            else:
                remaining.append(cycle_dir)

        sized = [
            (d, _newest_mtime(d), _dir_size(d)) for d in remaining
            if not (safe_active and d.name == safe_active)
        ]
        sized.sort(key=lambda t: t[1])  # oldest mtime first
        total_size = sum(size for _, _, size in sized) + sum(
            _dir_size(d) for d in remaining if safe_active and d.name == safe_active
        )
        for cycle_dir, _mtime, size in sized:
            if total_size <= max_total_bytes:
                break
            shutil.rmtree(cycle_dir, ignore_errors=True)
            total_size -= size
            counts["size_capped"] += 1
    except Exception:
        pass
    return counts


# ---------------------------------------------------------------------------
# Condition E (#1930): usage counter -- is the carrier's I1 index ever used?
# ---------------------------------------------------------------------------

def count_preservation_reads(state_root: "Path | str", cycle_id: str, *, day: "str | None" = None) -> int:
    """How many ``read_file`` actions this cycle's action index recorded
    against a path under ``compaction/`` (#1930 condition E) -- read-side
    only; ``nanobot/runtime/action_index.py`` already records a generic
    read/write/edit target path per tool call, so this needs no new live
    instrumentation, only a query over its existing per-day JSONL output.

    A low count doesn't argue for dropping I1 (the file is still a
    correctness guarantee) -- it argues the carrier's index isn't doing its
    job, and becomes the next hypothesis to test (see the module docstring's
    condition E). Fail-open: ``0`` on any error, including a missing index.

    Queries ``{state_root}/action_index/<day>.jsonl`` directly (the exact
    per-day path/row shape ``nanobot/runtime/action_index.py`` writes:
    ``cycle_id``, ``actions_detail`` -- a ``read:<path>``-templated list,
    same length/order as ``actions``) rather than importing that module, so
    this stays a pure read-side query with no coupling to its internals.
    """
    try:
        index_dir = Path(state_root) / "action_index"
        target_day = day or time.strftime("%Y-%m-%d", time.gmtime())
        path = index_dir / f"{target_day}.jsonl"
        if not path.is_file():
            return 0
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("cycle_id") != cycle_id:
                continue
            for detail in row.get("actions_detail") or []:
                if isinstance(detail, str) and detail.startswith("read:") and "compaction" in detail:
                    count += 1
        return count
    except Exception:
        return 0
