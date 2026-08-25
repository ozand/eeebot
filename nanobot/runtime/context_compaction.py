"""Context compaction for the subagent loop. #959

Reduces the token size of ``messages`` when a running conversation exceeds a
configurable fraction of the model's context window.  The module is stdlib-only
(no provider calls, no third-party imports) and fail-open: every public entry
point returns an unmodified history and/or writes no journal on any error.

Char ÷ 4 heuristic
------------------
Token estimation uses ``ceil(len(text) / 4)``.  This is a deliberate
approximation — tight enough for the compaction threshold decision and fast
enough to call on every iteration.  For the default 98 000-token window the
heuristic is conservative (actual token counts are often 20-30 % lower for
English prose), which means compaction fires slightly early rather than late,
keeping a safety margin.

Environment knobs (all optional, applied once at import time)
-------------------------------------------------------------
``SELFEVO_COMPACT_THRESHOLD``   float 0–1  fraction of window that triggers
                                compaction (default 0.8)
``SELFEVO_COMPACT_KEEP_RESULTS`` int ≥ 1  number of most-recent tool-result
                                messages that are always kept verbatim
                                (default 3)
``SELFEVO_COMPACT_WINDOW_TOKENS`` int > 0  total context-window size in tokens
                                used for threshold calculation
                                (default 98 000)
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

#: Number of most-recent tool-result messages always kept verbatim.
KEEP_RESULTS: int = max(1, _env_int("SELFEVO_COMPACT_KEEP_RESULTS", 3))

#: Assumed context-window size in tokens.
WINDOW_TOKENS: int = max(1, _env_int("SELFEVO_COMPACT_WINDOW_TOKENS", 98_000))

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


def _compact_content(content: str) -> str:
    """Replace long content with a bounded head/tail excerpt."""
    min_length = EXCERPT_HEAD + len(_OMIT_MARKER) + EXCERPT_TAIL
    if len(content) <= min_length:
        return content
    return content[:EXCERPT_HEAD] + _OMIT_MARKER + content[-EXCERPT_TAIL:]


def _compact_message(msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (compacted_msg, was_changed) for a tool-result message."""
    content = msg.get("content") or ""
    if isinstance(content, list):
        # Anthropic block list — compact text blocks
        changed = False
        new_blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                new_block = dict(block)
                inner = block.get("content") or ""
                if isinstance(inner, str):
                    compacted = _compact_content(inner)
                    if compacted != inner:
                        new_block["content"] = compacted
                        changed = True
                new_blocks.append(new_block)
            else:
                new_blocks.append(block)
        new_msg = dict(msg, content=new_blocks)
        return new_msg, changed
    else:
        text = str(content)
        compacted = _compact_content(text)
        if compacted == text:
            return msg, False
        return dict(msg, content=compacted), True


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
) -> None:
    """Append one JSONL event to state_root/compaction/journal.jsonl.

    Fail-open: any I/O error is silently swallowed.
    """
    try:
        journal_dir = Path(state_root) / "compaction"
        journal_dir.mkdir(parents=True, exist_ok=True)
        journal_path = journal_dir / "journal.jsonl"
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cycle_id": cycle_id,
            "iteration": iteration,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "results_compacted": results_compacted,
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
    keep_results: int | None = None,
    window_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Compact ``messages`` if their estimated token count exceeds the threshold.

    Protected messages (never compacted):
    - All ``system`` and ``user`` messages (system prompt + initial task).
    - The ``keep_results`` most-recent tool-result messages.

    Older tool-result messages have their ``content`` replaced with a bounded
    head/tail excerpt separated by ``_OMIT_MARKER``.  Assistant messages are
    never compacted.

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
    keep_results:
        Override ``KEEP_RESULTS`` for this call (used in tests).
    window_tokens:
        Override ``WINDOW_TOKENS`` for this call (used in tests).

    Returns
    -------
    list[dict]
        The (possibly compacted) message list.  Always returns a valid list;
        returns the original ``messages`` unchanged on any error.
    """
    try:
        _threshold = threshold if threshold is not None else THRESHOLD
        _keep = keep_results if keep_results is not None else KEEP_RESULTS
        _window = window_tokens if window_tokens is not None else WINDOW_TOKENS

        before_tokens = _total_tokens(messages)
        trigger_tokens = math.ceil(_threshold * _window)

        if before_tokens < trigger_tokens:
            # Below threshold — nothing to do.
            return messages

        # Identify compactable tool-result indices (excluding last _keep).
        tool_result_indices = [
            i for i, m in enumerate(messages) if _is_tool_result(m)
        ]
        # Keep the last _keep verbatim.
        compactable_indices = set(tool_result_indices[: max(0, len(tool_result_indices) - _keep)])

        if not compactable_indices:
            # No old tool results to compact — return original.
            return messages

        # Build compacted copy.
        new_messages: list[dict[str, Any]] = []
        results_compacted = 0
        for i, msg in enumerate(messages):
            if i in compactable_indices:
                compacted, changed = _compact_message(msg)
                new_messages.append(compacted)
                if changed:
                    results_compacted += 1
            else:
                new_messages.append(msg)

        after_tokens = _total_tokens(new_messages)
        _write_journal(state_root, cycle_id, iteration, before_tokens, after_tokens, results_compacted)
        return new_messages

    except Exception:  # noqa: BLE001
        # Fail-open: return original history untouched.
        return messages
