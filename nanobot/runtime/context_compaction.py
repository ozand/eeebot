"""Context compaction for the subagent loop. #959, #1776

Reduces the token size of ``messages`` when a running conversation exceeds a
configurable fraction of the model's context window.  The module is stdlib-only
(no provider calls, no third-party imports) and fail-open: every public entry
point returns an unmodified history on any error.

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
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Estimate token count using the chars÷4 heuristic (ceil)."""
    return math.ceil(len(text) / 4)


def _message_tokens(msg: dict[str, Any]) -> int:
    """Estimate tokens for a single message dict."""
    content = msg.get("content") or ""
    if isinstance(content, list):
        # Anthropic-style block list
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += _estimate_tokens(str(block.get("text") or block.get("content") or ""))
            else:
                total += _estimate_tokens(str(block))
        return total
    return _estimate_tokens(str(content))


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


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content") or ""
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or block.get("content") or "")
            if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


_PATH_RE = re.compile(
    r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}(?![\w.-])"
)


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
                decisions.append(text[:500])
            elif not msg.get("tool_calls"):
                progress.append(text[:500])
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

    lines = ["[Compaction summary — deterministic, evidence-only]", f"Goal: {goal[:1000] or 'not available'}"]
    if previous:
        lines.extend(["Earlier summary (preserved):", previous])
    lines.append("Progress:")
    lines.extend(f"- {x}" for x in progress[-6:])
    lines.append("Key decisions (explicit statements only):")
    lines.extend(f"- {x}" for x in decisions[-6:])
    lines.append("Files read (paths observed in tool calls/results):")
    lines.extend(f"- {x}" for x in sorted(read_files))
    lines.append("Files modified (paths observed in write/edit/patch tools):")
    lines.extend(f"- {x}" for x in sorted(modified_files))
    return "\n".join(lines)


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


def _already_marked(text: str) -> bool:
    """True once a message has already been through compaction — either it
    carries the structural summary or a prior legacy excerpt. Used to make
    repeat compaction idempotent (#1776: a second call with no new content
    must not re-touch, or re-grow, what an earlier call already produced)."""
    return text.startswith("[Compaction summary") or _OMIT_MARKER in text


def _worth_compacting(msg: dict[str, Any]) -> bool:
    """A candidate is only physically replaced if doing so actually shrinks
    it and it hasn't already been compacted — otherwise compacting a
    trivially small or already-compacted message would only grow it."""
    text = _message_text(msg)
    return bool(text) and not _already_marked(text) and len(text) > _min_compact_len()


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
        # one). `evidence_span` covers the WHOLE unprotected span (not only
        # worth_indices) so a short-but-fresh message — e.g. a one-line
        # "decision: ..." too small to be worth excerpting on its own —
        # still contributes evidence, while messages already compacted by
        # an earlier round (recognisable by `_already_marked`) are excluded
        # so their content isn't re-scanned as if it were new.
        previous = "\n".join(
            _message_text(m) for m in messages
            if _message_text(m).startswith("[Compaction summary")
        )
        evidence_span = [
            messages[i] for i in sorted_candidates
            if not _already_marked(_message_text(messages[i]))
        ]
        goal = _message_text(messages[1]) if len(messages) > 1 else ""
        summary = _structural_summary(evidence_span, goal=goal, previous=previous)

        new_messages = list(messages)
        results_compacted = 0
        compacted_details: list[dict[str, Any]] = []
        carrier = worth_indices[0]
        for i in worth_indices:
            msg = messages[i]
            old_text = _message_text(msg)
            tool_name = str(msg.get("name") or "tool")
            if i == carrier:
                excerpt, _ = _excerpt_content(msg.get("content"))
                excerpt_text = excerpt if isinstance(excerpt, str) else _message_text(dict(msg, content=excerpt))
                new_content = f"{summary}\n{excerpt_text}"
            else:
                new_content, _ = _excerpt_content(msg.get("content"))
            new_messages[i] = dict(msg, content=new_content)
            compacted_details.append(_drop_detail(tool_name, old_text))
            results_compacted += 1

        after_tokens = _total_tokens(new_messages)
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
