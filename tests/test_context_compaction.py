"""Tests for nanobot.runtime.context_compaction (#959).

Covers:
- below-threshold: messages returned unchanged
- above-threshold: old tool results compacted, system/user protected
- last-K tool results protected verbatim
- excerpt bounded with head/tail + marker
- fail-open: any exception still returns a useful list
- no provider/LLM calls involved
- deny-set entry is present and immutable (module listed in runtime_deny)
"""
import json
import math
from pathlib import Path

import pytest

# The module under test (stdlib-only, safe to import without network/secrets).
from nanobot.runtime import context_compaction as cc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_messages(tool_contents: list[str]) -> list[dict]:
    """Build a realistic message list: system, user/task, then alternating
    assistant-with-tool-call + tool-result for each entry in tool_contents."""
    msgs: list[dict] = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Do a thing."},
    ]
    for i, content in enumerate(tool_contents):
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"tc{i}", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": f"tc{i}",
            "name": "bash",
            "content": content,
        })
    return msgs


def _short_content(n: int = 50) -> str:
    return "x" * n


def _long_content(n: int = 5000) -> str:
    return "a" * n


# ---------------------------------------------------------------------------
# Below-threshold: no compaction
# ---------------------------------------------------------------------------

def test_below_threshold_returns_unchanged():
    """When total tokens < threshold*window, messages are returned as-is."""
    messages = _make_messages([_short_content(10)])
    original_ids = [id(m) for m in messages]
    result = cc.compact_messages(
        messages,
        cycle_id="c1",
        iteration=1,
        state_root="/tmp/cc_test_below",
        threshold=0.8,
        keep_results=3,
        window_tokens=98_000,
    )
    # Returned list is the same object (no copy needed) or equal contents
    assert result == messages
    # No messages were mutated
    for orig, res in zip(messages, result):
        assert orig == res


# ---------------------------------------------------------------------------
# Above-threshold: old results compacted
# ---------------------------------------------------------------------------

def test_above_threshold_compacts_old_tool_results(tmp_path):
    """When above threshold, old tool result contents are excerpted."""
    big = _long_content(40_000)  # 40000 chars ≈ 10000 tokens per result
    messages = _make_messages([big, big, big, big])  # 4 tool results

    result = cc.compact_messages(
        messages,
        cycle_id="c2",
        iteration=2,
        state_root=tmp_path,
        threshold=0.01,   # force trigger with very low threshold
        keep_results=1,   # protect only the last 1
        window_tokens=98_000,
    )

    # System and user messages must be untouched
    assert result[0] == messages[0]
    assert result[1] == messages[1]

    # Collect tool results from result
    tool_results = [m for m in result if m.get("role") == "tool"]
    assert len(tool_results) == 4

    # Last 1 must be verbatim
    assert tool_results[-1]["content"] == big

    # Earlier results should be compacted (contain the omit marker)
    for tr in tool_results[:-1]:
        assert cc._OMIT_MARKER in tr["content"], (
            f"Expected omit marker in compacted content, got: {tr['content'][:100]!r}"
        )


# ---------------------------------------------------------------------------
# Last-K tool results protected
# ---------------------------------------------------------------------------

def test_last_k_tool_results_protected(tmp_path):
    """The ``keep_results`` most-recent tool results are never compacted."""
    big = _long_content(20_000)
    messages = _make_messages([big] * 6)  # 6 tool results

    result = cc.compact_messages(
        messages,
        cycle_id="c3",
        iteration=3,
        state_root=tmp_path,
        threshold=0.01,  # force trigger
        keep_results=3,
        window_tokens=98_000,
    )

    tool_results = [m for m in result if m.get("role") == "tool"]
    assert len(tool_results) == 6

    # Last 3 must be verbatim
    for tr in tool_results[-3:]:
        assert tr["content"] == big, "Last-K results should be untouched"

    # First 3 should be compacted
    for tr in tool_results[:3]:
        assert cc._OMIT_MARKER in tr["content"], "Older results should be compacted"


# ---------------------------------------------------------------------------
# System and user task always protected
# ---------------------------------------------------------------------------

def test_system_and_user_always_protected(tmp_path):
    """System and user messages are never touched by compaction."""
    big = _long_content(30_000)
    system_content = "SYSTEM PROMPT MUST SURVIVE"
    user_content = "USER TASK MUST SURVIVE"

    msgs: list[dict] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    for i in range(5):
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"tc{i}", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}],
        })
        msgs.append({"role": "tool", "tool_call_id": f"tc{i}",
                     "name": "bash", "content": big})

    result = cc.compact_messages(
        msgs,
        cycle_id="c4",
        iteration=4,
        state_root=tmp_path,
        threshold=0.01,
        keep_results=1,
        window_tokens=98_000,
    )

    assert result[0]["content"] == system_content
    assert result[1]["content"] == user_content


# ---------------------------------------------------------------------------
# Excerpt is bounded by head/tail
# ---------------------------------------------------------------------------

def test_excerpt_bounded_head_tail():
    """Compacted content has at most EXCERPT_HEAD + marker + EXCERPT_TAIL chars."""
    big = "A" * 500 + "B" * 500  # 1000 chars, bigger than default 200+200
    original_content = big
    result_content, changed = cc._compact_content(big), True

    head = cc.EXCERPT_HEAD
    tail = cc.EXCERPT_TAIL
    marker = cc._OMIT_MARKER

    assert result_content.startswith(big[:head])
    assert result_content.endswith(big[-tail:])
    assert marker in result_content
    assert len(result_content) == head + len(marker) + tail


def test_excerpt_not_compacted_when_small():
    """Content smaller than head+marker+tail is returned as-is."""
    small = "x" * 50
    result = cc._compact_content(small)
    assert result == small


# ---------------------------------------------------------------------------
# Fail-open on exception
# ---------------------------------------------------------------------------

def test_fail_open_on_invalid_state_root():
    """If state_root is unwriteable/bad, compact_messages still returns a list."""
    big = _long_content(30_000)
    messages = _make_messages([big] * 4)

    # Pass a nonsensical path that will fail to mkdir
    result = cc.compact_messages(
        messages,
        cycle_id="fail_test",
        iteration=1,
        state_root="/dev/null/cannot/exist",
        threshold=0.01,
        keep_results=1,
        window_tokens=98_000,
    )
    # Must return a list (either compacted or original)
    assert isinstance(result, list)
    assert len(result) == len(messages)


def test_fail_open_on_bad_message_structure():
    """Malformed messages never cause compact_messages to raise."""
    messages = [None, 42, {"role": "tool", "content": {}}, "not-a-dict"]  # type: ignore[list-item]
    try:
        result = cc.compact_messages(
            messages,  # type: ignore[arg-type]
            cycle_id="bad",
            iteration=1,
            state_root="/tmp/cc_failopen",
        )
        # Either returned the original or a list; must not raise
        assert isinstance(result, list)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"compact_messages raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Journal is written on compaction
# ---------------------------------------------------------------------------

def test_journal_written_on_compaction(tmp_path):
    """A JSONL event is written to state_root/compaction/journal.jsonl."""
    big = _long_content(20_000)
    messages = _make_messages([big] * 4)

    cc.compact_messages(
        messages,
        cycle_id="journal_test",
        iteration=7,
        state_root=tmp_path,
        threshold=0.01,
        keep_results=1,
        window_tokens=98_000,
    )

    journal_path = tmp_path / "compaction" / "journal.jsonl"
    assert journal_path.exists(), "Journal file should exist after compaction"

    events = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    assert len(events) >= 1

    ev = events[-1]
    assert ev["cycle_id"] == "journal_test"
    assert ev["iteration"] == 7
    assert "before_tokens" in ev
    assert "after_tokens" in ev
    assert "results_compacted" in ev
    assert ev["after_tokens"] <= ev["before_tokens"]


def test_journal_not_written_below_threshold(tmp_path):
    """No journal entry when compaction does not fire."""
    messages = _make_messages([_short_content(10)])
    cc.compact_messages(
        messages,
        cycle_id="noevent",
        iteration=1,
        state_root=tmp_path,
        threshold=0.9999,
        keep_results=3,
        window_tokens=98_000,
    )
    journal_path = tmp_path / "compaction" / "journal.jsonl"
    assert not journal_path.exists(), "Journal should not be written below threshold"


# ---------------------------------------------------------------------------
# No provider/LLM calls (import-time check)
# ---------------------------------------------------------------------------

def test_no_provider_imports():
    """context_compaction must not import any LLM provider module."""
    import importlib
    import sys

    # Reload the module to inspect its dependencies
    mod = importlib.import_module("nanobot.runtime.context_compaction")
    # The module's own __file__ imports should not include provider packages
    provider_names = {"openai", "anthropic", "litellm", "httpx", "aiohttp", "requests"}
    imported = set(sys.modules.keys())
    # Only flag if they were imported *by* context_compaction (approximate check:
    # if they weren't in sys.modules before, they shouldn't be there after a bare import).
    # This is a smoke-level check: the module itself uses only stdlib.
    module_source = Path(mod.__file__).read_text()
    for name in provider_names:
        assert f"import {name}" not in module_source, (
            f"context_compaction.py must not import {name}"
        )


# ---------------------------------------------------------------------------
# Deny-set: module is listed in runtime_deny (immutability check)
# ---------------------------------------------------------------------------

def test_deny_set_has_context_compaction_entry():
    """context_compaction.py must be in _RUNTIME_DENY_ALWAYS_FILES."""
    from nanobot.runtime.runtime_deny import _RUNTIME_DENY_ALWAYS_FILES  # type: ignore[attr-defined]

    assert "nanobot/runtime/context_compaction.py" in _RUNTIME_DENY_ALWAYS_FILES, (
        "context_compaction.py must be in the explicit deny-set so the runtime "
        "loop cannot modify its own compaction logic."
    )


def test_deny_set_is_frozenset():
    """_RUNTIME_DENY_ALWAYS_FILES must be a frozenset (immutable)."""
    from nanobot.runtime.runtime_deny import _RUNTIME_DENY_ALWAYS_FILES  # type: ignore[attr-defined]

    assert isinstance(_RUNTIME_DENY_ALWAYS_FILES, frozenset), (
        "_RUNTIME_DENY_ALWAYS_FILES must be frozenset so the deny set is immutable."
    )


# ---------------------------------------------------------------------------
# Token estimation sanity check
# ---------------------------------------------------------------------------

def test_estimate_tokens_heuristic():
    """_estimate_tokens uses ceil(len/4)."""
    text = "x" * 100
    assert cc._estimate_tokens(text) == math.ceil(100 / 4)
    assert cc._estimate_tokens("") == 0
    assert cc._estimate_tokens("a") == 1
    assert cc._estimate_tokens("ab") == 1


# ---------------------------------------------------------------------------
# Edge: all messages are system/user only (no tool results to compact)
# ---------------------------------------------------------------------------

def test_no_tool_results_returns_unchanged(tmp_path):
    """If there are no tool results, compaction is a no-op."""
    messages = [
        {"role": "system", "content": "x" * 400_000},  # huge system prompt
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "done"},
    ]
    result = cc.compact_messages(
        messages,
        cycle_id="notool",
        iteration=1,
        state_root=tmp_path,
        threshold=0.0,  # always trigger
        keep_results=3,
        window_tokens=1,
    )
    # Returns original list unchanged because there are no tool results to compact
    assert result == messages
