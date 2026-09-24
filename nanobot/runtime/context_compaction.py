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

#1776 scope: the observable half only
--------------------------------------
#1776 summary half: replace the dropped span with a deterministic structural
summary. No model call is made: model summarization would add latency and
queue cost to every compaction, while heuristic extraction can be honest if it
quotes observed messages and never infers unstated intent. The summary carries
the original task, recent assistant progress, explicitly marked decisions,
and paths evidenced by file tools/commands; unknown fields stay empty. Earlier
compaction summaries are retained verbatim so repeated compaction is cumulative.


- The cut point is a token span walked back from the newest message, not a
  fixed count of recent messages (:func:`_compactable_indices`) — a single
  oversized recent tool result is no longer permanently protected just for
  being recent.
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

Still true (item 3, deferred with items 1-2's summary question): assistant
messages are never compacted.

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


def _is_tool_result(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "tool"


def _compactable_indices(messages: list[dict[str, Any]], keep_tokens: int) -> set[int]:
    """#1776: token-span cut point, replacing the old fixed recent-message
    count. Walk back from the newest message accumulating estimated tokens;
    the first message whose inclusion would push the running total past
    ``keep_tokens`` is excluded from the protected span (and so is
    everything before it) — including when that single message alone
    already exceeds ``keep_tokens``. That is deliberate: a fixed message
    COUNT can never compact an oversized recent result no matter how large
    it is; a token span can, because the span itself has a size.

    Messages of every role in the unprotected (older) span are candidates,
    except system and the initial user task. Tool-call metadata is retained
    when assistant content is compacted, preserving call/result pairing.
    Message order and count never change.
    """
    cumulative = 0
    keep_from = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        tokens = _message_tokens(messages[i])
        if cumulative + tokens > keep_tokens:
            break
        cumulative += tokens
        keep_from = i
    return {i for i in range(keep_from) if i >= 2 and _is_tool_result(messages[i])}


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content") or ""
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or block.get("content") or "")
            if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


def _structural_summary(messages: list[dict[str, Any]], previous: str = "") -> str:
    """Summarize only explicit evidence; never invent unstated intent."""
    goal = _message_text(messages[1]) if len(messages) > 1 else ""
    progress: list[str] = []
    decisions: list[str] = []
    read_files: set[str] = set()
    modified_files: set[str] = set()
    path_re = __import__("re").compile(r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}(?![\w.-])")
    for msg in messages[2:]:
        text = _message_text(msg).strip()
        if not text:
            continue
        role = msg.get("role")
        if text.startswith("[Compaction summary"):
            continue
        if role == "assistant":
            if "decision:" in text.lower() or "we will " in text.lower():
                decisions.append(text[:500])
            elif not msg.get("tool_calls"):
                progress.append(text[:500])
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                args = fn.get("arguments") if isinstance(fn, dict) else None
                if isinstance(args, str):
                    for path in path_re.findall(args):
                        (modified_files if any(w in str(fn.get("name", "")).lower() for w in ("write", "edit", "patch")) else read_files).add(path)
        elif role == "tool":
            name = str(msg.get("name") or "").lower()
            for path in path_re.findall(text):
                (modified_files if any(w in name for w in ("write", "edit", "patch")) else read_files).add(path)
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
    line count of the dropped middle span. This is deliberately NOT a
    summary of what the content meant (that is the deferred, expensive
    half — see the module docstring); it only answers "which tool, how
    much, roughly what shape" so the journal stops being silent about
    WHAT was cut, only how much.
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


def _compact_message(msg: dict[str, Any], summary: str | None = None) -> tuple[dict[str, Any], "dict[str, Any] | None"]:
    """Replace a tool-result body with a structural summary."""
    tool_name = str(msg.get("name") or "")
    content = msg.get("content") or ""
    if summary is not None:
        return dict(msg, content=summary), _drop_detail(tool_name or "tool", _message_text(msg))
    if isinstance(content, list):
        # Anthropic block list — compact text blocks
        detail: dict[str, Any] | None = None
        new_blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                new_block = dict(block)
                inner = block.get("content") or ""
                if isinstance(inner, str):
                    compacted = _compact_content(inner)
                    if compacted != inner:
                        new_block["content"] = compacted
                        detail = _drop_detail(tool_name, inner)
                new_blocks.append(new_block)
            else:
                new_blocks.append(block)
        new_msg = dict(msg, content=new_blocks)
        return new_msg, detail
    else:
        text = str(content)
        compacted = _compact_content(text)
        if compacted == text:
            return msg, None
        return dict(msg, content=compacted), _drop_detail(tool_name, text)


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
    - All ``assistant`` messages (item 3 of #1776 is deferred; see the
      module docstring).

    Older tool-result messages have their ``content`` replaced with a bounded
    head/tail excerpt separated by ``_OMIT_MARKER``.

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
            oversized = next((i for i in range(len(messages) - 1, 1, -1)
                              if _is_tool_result(messages[i])
                              and _message_tokens(messages[i]) > _keep_tokens), None)
            if oversized is not None:
                compactable = {oversized}
            else:
                _write_journal(
                    state_root, cycle_id, iteration, trigger_signal, trigger_signal, 0,
                    reason="nothing_older_than_cut", real_prev_prompt=real_prompt,
                )
                return messages

        dropped_messages = [messages[i] for i in sorted(compactable)]
        previous = "\n".join(
            _message_text(m) for m in messages
            if _message_text(m).startswith("[Compaction summary")
        )
        summary = previous or _structural_summary(messages, previous=previous)
        new_messages: list[dict[str, Any]] = []
        results_compacted = 0
        compacted_details: list[dict[str, Any]] = []
        summary_inserted = False
        for i, msg in enumerate(messages):
            if i in compactable:
                if not summary_inserted:
                    if _message_text(msg).startswith("[Compaction summary"):
                        compacted, detail = msg, None
                    elif len(_message_text(msg)) <= EXCERPT_HEAD + len(_OMIT_MARKER) + EXCERPT_TAIL:
                        compacted, detail = _compact_message(msg)
                        if detail is None:
                            compacted, detail = dict(msg, content=summary), _drop_detail(
                                str(msg.get("name") or "tool"), _message_text(msg),
                            )
                    else:
                        compacted, detail = _compact_message(msg, summary)
                    summary_inserted = True
                else:
                    compacted, detail = _compact_message(msg)
                new_messages.append(compacted)
                if detail is not None:
                    results_compacted += 1
                    compacted_details.append(detail)
            else:
                new_messages.append(msg)

        if all(
            len(_message_text(m)) <= EXCERPT_HEAD + len(_OMIT_MARKER) + EXCERPT_TAIL
            for m in dropped_messages
        ):
            _write_journal(
                state_root, cycle_id, iteration, trigger_signal, trigger_signal, 0,
                reason="already_compact", real_prev_prompt=real_prompt,
            )
            return messages

        if results_compacted == 0 or all(
            _message_text(m).startswith("[Compaction summary") for m in dropped_messages
        ):
            _write_journal(
                state_root, cycle_id, iteration, trigger_signal, trigger_signal, 0,
                reason="already_compact", real_prev_prompt=real_prompt,
            )
            return messages

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
