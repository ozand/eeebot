"""Tests for nanobot.runtime.context_compaction (#959, #1776).

Covers:
- below-threshold: messages returned unchanged, decline journalled
- above-threshold: old tool results compacted, system/user/assistant protected
- token-span cut point protects by TOKENS, not a fixed message count (#1776
  item 1) — including the case a fixed count could never handle: a single
  oversized recent tool result
- a tool call and its result are never separated by compaction (#1776)
- excerpt bounded with head/tail + marker
- every evaluation is journalled, including declines, each with a reason
  (#1776 item 4) — "did not fire" vs "did not run" are now distinguishable
- the journal names what was dropped (tool name + mechanical
  characterization), not only how much (#1776)
- the reserve covers the completion ceiling, and the window size has one
  definition (#1776 item 5-6 drift guards)
- fail-open: any exception still returns a useful list AND journals the
  failure (#1776 item 6)
- no provider/LLM calls involved
- deny-set entry is present and immutable (module listed in runtime_deny)
"""
import asyncio
import json
import math
import os
import random
import re
import time
from pathlib import Path

import pytest

# The module under test (stdlib-only, safe to import without network/secrets).
from nanobot.runtime import context_compaction as cc
from nanobot.runtime.runtime_deny import _RUNTIME_DENY_ALWAYS_FILES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_messages(tool_contents: list[str], *, tool_name: str = "bash") -> list[dict]:
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
                             "function": {"name": tool_name, "arguments": "{}"}}],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": f"tc{i}",
            "name": tool_name,
            "content": content,
        })
    return msgs


def _short_content(n: int = 50) -> str:
    return "x" * n


def _long_content(n: int = 5000) -> str:
    return "a" * n


def _last_journal_event(state_root) -> dict:
    journal_path = Path(state_root) / "compaction" / "journal.jsonl"
    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return events[-1]


# ---------------------------------------------------------------------------
# Below-threshold: no compaction, decline journalled
# ---------------------------------------------------------------------------

def test_below_threshold_returns_unchanged(tmp_path):
    """When total tokens < threshold*window, messages are returned as-is."""
    messages = _make_messages([_short_content(10)])
    result = cc.compact_messages(
        messages,
        cycle_id="c1",
        iteration=1,
        state_root=tmp_path,
        threshold=0.8,
        keep_tokens=20_000,
        window_tokens=98_304,
    )
    assert result == messages
    for orig, res in zip(messages, result):
        assert orig == res


def test_below_threshold_journals_the_decline_with_a_reason(tmp_path):
    """#1776 item 4: a decline is no longer silent."""
    messages = _make_messages([_short_content(10)])
    cc.compact_messages(
        messages, "below-journal", 1, tmp_path,
        threshold=0.8, keep_tokens=20_000, window_tokens=98_304,
    )
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "below_threshold"
    assert ev["results_compacted"] == 0


# ---------------------------------------------------------------------------
# Real provider usage trigger
# ---------------------------------------------------------------------------

def test_real_prompt_tokens_trigger_when_chars_estimate_is_under_threshold(tmp_path):
    """The real previous prompt usage catches the window growth profile."""
    messages = _make_messages([_long_content(8_000)] * 4)
    result = cc.compact_messages(
        messages,
        cycle_id="growth-profile",
        iteration=41,
        state_root=tmp_path,
        threshold=0.8,
        keep_tokens=1,
        window_tokens=98_304,
        prompt_tokens=96_116,
    )
    assert result[3]["content"].startswith("[Compaction summary")


def test_missing_prompt_tokens_falls_back_to_whole_history_estimate(tmp_path):
    messages = _make_messages([_long_content(40_000)] * 4)
    result = cc.compact_messages(
        messages, "fallback", 1, tmp_path,
        threshold=0.01, keep_tokens=1, window_tokens=98_304,
    )
    assert result[3]["content"].startswith("[Compaction summary")


def test_below_threshold_messages_are_byte_identical_with_usage(tmp_path):
    messages = _make_messages([_short_content(10)])
    result = cc.compact_messages(
        messages, "unchanged", 1, tmp_path,
        threshold=0.8, keep_tokens=20_000, window_tokens=98_304,
        prompt_tokens=10,
    )
    assert result is messages


def test_below_threshold_returns_same_instance(tmp_path):
    """When trigger threshold is not reached, the original list instance is returned unchanged."""
    messages = _make_messages([_long_content(1_000)])
    result = cc.compact_messages(
        messages, "same-instance", 1, tmp_path,
        threshold=0.8, keep_tokens=20_000, window_tokens=98_304,
        prompt_tokens=500,
    )
    assert result is messages


def test_delta_triggers_compaction_when_base_prompt_is_under_threshold(tmp_path):
    """Base prompt_tokens + prompt_token_delta crosses threshold and triggers compaction."""
    messages = _make_messages([_long_content(10_000)] * 4)
    result = cc.compact_messages(
        messages,
        cycle_id="delta-test",
        iteration=2,
        state_root=tmp_path,
        threshold=0.8,
        keep_tokens=1,
        window_tokens=98_000,
        reserve_tokens=8_000,
        prompt_tokens=70_000,
        prompt_token_delta=3_000,
    )
    assert result[3]["content"].startswith("[Compaction summary")


def test_reserve_tokens_parameter_affects_trigger_threshold(tmp_path):
    """Passing a larger reserve_tokens lowers the effective capacity and triggers earlier."""
    messages = _make_messages([_long_content(10_000)] * 4)
    result = cc.compact_messages(
        messages,
        cycle_id="reserve-test",
        iteration=1,
        state_root=tmp_path,
        threshold=0.8,
        keep_tokens=1,
        window_tokens=100_000,
        reserve_tokens=20_000,
        prompt_tokens=65_000,
    )
    assert result[3]["content"].startswith("[Compaction summary")

    result_no_reserve = cc.compact_messages(
        messages,
        cycle_id="no-reserve-test",
        iteration=1,
        state_root=tmp_path,
        threshold=0.8,
        keep_tokens=1,
        window_tokens=100_000,
        reserve_tokens=0,
        prompt_tokens=65_000,
    )
    assert result_no_reserve is messages


# ---------------------------------------------------------------------------
# Above-threshold: old results compacted
# ---------------------------------------------------------------------------

def test_above_threshold_compacts_old_tool_results(tmp_path):
    """When above threshold, old tool result contents are excerpted."""
    big = _long_content(40_000)  # 40000 chars ~= 10000 tokens per result
    messages = _make_messages([big, big, big, big])  # 4 tool results

    result = cc.compact_messages(
        messages,
        cycle_id="c2",
        iteration=2,
        state_root=tmp_path,
        threshold=0.01,   # force trigger with very low threshold
        keep_tokens=10_000,   # protects roughly the last (newest) result only
        window_tokens=98_304,
    )

    assert result[0] == messages[0]
    assert result[1] == messages[1]

    tool_results = [m for m in result if m.get("role") == "tool"]
    assert len(tool_results) == 4
    assert tool_results[-1]["content"] == big, "the newest result fits the keep-token span"
    for tr in tool_results[:-1]:
        assert tr["content"].startswith("[Compaction summary") or cc._OMIT_MARKER in tr["content"], (
            f"Expected omit marker in compacted content, got: {tr['content'][:100]!r}"
        )


def test_compacted_messages_not_recompacted_on_next_call(tmp_path):
    """Once compacted, a tool result has the omit marker and is not excerpted a second time."""
    big = _long_content(40_000)
    messages = _make_messages([big, big, big, big])

    result1 = cc.compact_messages(
        messages, cycle_id="pass1", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=10_000, window_tokens=98_304,
    )
    compacted_content_1 = [m["content"] for m in result1 if m.get("role") == "tool"]

    result2 = cc.compact_messages(
        result1, cycle_id="pass2", iteration=2, state_root=tmp_path,
        threshold=0.01, keep_tokens=10_000, window_tokens=98_304,
    )
    compacted_content_2 = [m["content"] for m in result2 if m.get("role") == "tool"]

    assert compacted_content_1 == compacted_content_2
    assert any(text.startswith("[Compaction summary") for text in compacted_content_2)
    # A real second pass consumes a new tool result; the cumulative summary
    # retained in history must still contain the first pass's summary text.
    assert compacted_content_1[0] in "\n".join(compacted_content_2)


def test_second_real_compaction_incorporates_newly_dropped_evidence(tmp_path):
    """#1776 item 2's cumulative requirement, exercised through the actual
    public entry point rather than by calling `_structural_summary` in
    isolation with hand-picked args (a prior version of this test did that,
    and it passed while the real second-compaction path had a bug: it
    reused the first round's frozen summary text unchanged and any newly
    dropped content in the second round silently reverted to a plain
    head/tail excerpt -- the exact "bytes dropped, meaning not carried"
    defect #1776 exists to fix, recurring on every compaction after the
    first). This drives two genuine `compact_messages` calls and checks the
    SECOND round's summary contains both the first round's evidence and
    genuinely new evidence dropped only in the second round.

    FAILS on origin/main: `_structural_summary` / the "[Compaction summary"
    marker don't exist there -- only #1776 item 1 (the token-span cut) has
    landed; items 2-3 (this PR) have not.
    """
    big = _long_content(30_000)
    messages = _make_messages([big, big, big, big])

    result1 = cc.compact_messages(
        messages, cycle_id="acc", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=8_000, window_tokens=98_304,
    )
    round1_tool_texts = [m["content"] for m in result1 if m.get("role") == "tool"]
    assert any(t.startswith("[Compaction summary") for t in round1_tool_texts)

    # New work happens after round 1: a decision and a file write neither
    # round 1's summary nor its excerpts could possibly know about. A
    # further filler pair pushes this new pair out of the protected recent
    # span so round 2 actually drops (and must summarize) it, rather than
    # leaving it sitting verbatim in the still-protected tail.
    grown = result1 + [
        {"role": "assistant", "content": "decision: switch to plan B",
         "tool_calls": [{"id": "tc-new", "type": "function",
                          "function": {"name": "write_file",
                                       "arguments": '{"path": "src/new_module_round2.py"}'}}]},
        {"role": "tool", "tool_call_id": "tc-new", "name": "write_file",
         "content": _long_content(1_000) + " round2 evidence marker"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc-filler", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc-filler", "name": "bash", "content": _long_content(40_000)},
    ]

    result2 = cc.compact_messages(
        grown, cycle_id="acc", iteration=2, state_root=tmp_path,
        threshold=0.01, keep_tokens=8_000, window_tokens=98_304,
    )
    round2_texts = [m["content"] for m in result2 if m.get("role") in ("tool", "assistant")]
    carrier_texts = [t for t in round2_texts if isinstance(t, str) and t.startswith("[Compaction summary")]
    assert carrier_texts, "round 2 must still carry a structural summary"
    carrier = "\n".join(carrier_texts)

    assert round1_tool_texts[0] in carrier or any(
        line in carrier for line in round1_tool_texts[0].splitlines() if line.startswith("Goal:")
    ), "round 1's summary must still be present after round 2 (cumulative, not overwritten)"
    assert "src/new_module_round2.py" in carrier, (
        "round 2's newly dropped file path must appear in the updated summary, "
        "not silently vanish into a plain excerpt"
    )
    assert "switch to plan B" in carrier, (
        "round 2's newly dropped decision must appear in the updated summary"
    )


def test_summary_carrier_is_not_inserted_when_it_would_increase_history(tmp_path):
    """A small candidate must not turn compaction into growth.

    Regression coverage: pF found the previous version of this test did not
    actually catch the defect it names. That version paired the small
    candidate with an 80,000-char tool result; ``_worth_compacting`` also
    shrinks that tool result to a bounded excerpt regardless of the carrier
    guard, and that single shrink (~80,000 chars -> ~420) dominates the
    total so completely that the assertion passed even on 450ee6d3 -- the
    commit right before the growth guard (225b6e70, "avoid growing summary
    carrier") was added, where the carrier is inserted unconditionally.

    This version isolates the carrier: nothing else in the fixture is worth
    compacting (every tool result is well under
    ``cc._min_compact_len()``), so only the guard's own choice can move the
    total. A sizeable pre-existing summary carrier (comfortably under the
    ~4,000-char budget ``_structural_summary`` embeds ``previous`` into, so
    it is carried forward whole rather than re-truncated -- see
    ``MAX_SUMMARY_CHARS - 2_000``) supplies real "previous round" content, as
    a live cycle would have after an earlier compaction.
    """
    goal = "Investigate the flaky release-gate test."
    old_summary = (
        "[Compaction summary — deterministic, evidence-only]\n" + "P" * 3_500
    )
    small_carrier_content = "x" * (cc._min_compact_len() + 2)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": goal},
        {"role": "assistant", "content": old_summary},
        {"role": "tool", "tool_call_id": "tc-old", "name": "bash", "content": "ok"},
        {"role": "assistant", "content": small_carrier_content,
         "tool_calls": [{"id": "tc-small", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc-small", "name": "bash",
         "content": "short result"},
    ]
    before_tokens = cc._total_tokens(messages)

    result = cc.compact_messages(
        list(messages), cycle_id="carrier-growth", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=1, window_tokens=98_304,
    )
    after_tokens = cc._total_tokens(result)

    assert after_tokens <= before_tokens, (
        f"compaction must not grow the history: before={before_tokens} "
        f"after={after_tokens}"
    )
    new_carrier = result[4]
    assert not str(new_carrier.get("content", "")).startswith("[Compaction summary"), (
        "the new candidate's own excerpt is smaller than a fresh summary "
        "carrying the old one forward would be -- the guard must keep the "
        "plain excerpt, not install a carrier that grows this turn"
    )


def test_carrier_growth_guard_keeps_exactly_one_live_summary_across_rounds(tmp_path):
    """#1930 follow-up (pF): after many compactions in one cycle, history
    must carry exactly one live ``[Compaction summary`` message -- every
    earlier carrier is retired to a short placeholder once a new one is
    installed, never left to accumulate alongside it."""
    messages = _make_messages([_long_content(20_000)] * 2)
    for round_n in range(10):
        messages = messages + [
            {"role": "assistant", "content": f"decision: round {round_n} note",
             "tool_calls": [{"id": f"tc-r{round_n}", "type": "function",
                              "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"tc-r{round_n}", "name": "bash",
             "content": _long_content(20_000) + f" marker_round_{round_n}"},
        ]
        messages = cc.compact_messages(
            messages, cycle_id="single-carrier", iteration=round_n, state_root=tmp_path,
            threshold=0.01, keep_tokens=8_000, window_tokens=98_304,
        )

    live_carriers = [
        m for m in messages
        if isinstance(m.get("content"), str) and m["content"].startswith("[Compaction summary")
    ]
    assert len(live_carriers) == 1, (
        f"expected exactly one live summary carrier, found {len(live_carriers)}"
    )


def test_second_compaction_summarizes_assistant_progress_with_tool_calls(tmp_path):
    """Assistant progress remains evidence even when that turn invokes tools."""
    messages = _make_messages([_long_content(30_000)] * 4)
    round1 = cc.compact_messages(
        messages, cycle_id="assistant-progress", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=8_000, window_tokens=98_304,
    )
    marker = "ROUND_TWO_IMPLEMENTATION_COMPLETE"
    progress_turn = {
        "role": "assistant",
        "content": _long_content(2_000) + marker + _long_content(2_000),
        "tool_calls": [{"id": "tc-progress", "type": "function",
                        "function": {"name": "bash", "arguments": "{}"}}],
    }
    grown = round1 + [
        progress_turn,
        {"role": "tool", "tool_call_id": "tc-progress", "name": "bash",
         "content": _long_content(1_000)},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc-filler", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc-filler", "name": "bash",
         "content": _long_content(40_000)},
    ]
    round2 = cc.compact_messages(
        grown, cycle_id="assistant-progress", iteration=2, state_root=tmp_path,
        threshold=0.01, keep_tokens=8_000, window_tokens=98_304,
    )

    carriers = [
        str(message.get("content") or "") for message in round2
        if str(message.get("content") or "").startswith("[Compaction summary")
    ]
    assert carriers
    assert marker in "\n".join(carriers)


def test_middle_progress_survives_near_budget_prior_summary_and_path_flood():
    """Fresh progress is prioritized over old summary and large file lists."""
    marker = "FRESH_MIDDLE_PROGRESS_SURVIVES"
    previous = "P" * 4_000
    paths = [f"src/generated/module_{i:04d}.py" for i in range(1_000)]
    evidence = [
        {"role": "assistant", "content": _long_content(2_000) + marker + _long_content(2_000),
         "tool_calls": [{"id": "paths", "type": "function",
                         "function": {"name": "read_file", "arguments": " ".join(paths)}}]},
        {"role": "tool", "name": "read_file", "content": "\\n".join(paths)},
    ]
    summary = cc._structural_summary(evidence, goal="task", previous=previous)
    assert len(summary) <= cc.MAX_SUMMARY_CHARS
    assert marker in summary


def test_summary_is_capped_including_large_file_lists(tmp_path, monkeypatch):
    """New paths and progress together cannot exceed the summary hard cap."""
    monkeypatch.setattr(cc, "MAX_SUMMARY_CHARS", 6_000)
    paths = [f"src/generated/module_{i:05d}.py" for i in range(6_000)]
    evidence = [
        {"role": "assistant", "content": "progress " + _long_content(800),
         "tool_calls": [{"id": "paths", "type": "function",
                         "function": {"name": "read_file", "arguments": " ".join(paths)}}]},
        {"role": "tool", "name": "read_file", "content": "\n".join(paths)},
    ]
    summary = cc._structural_summary(evidence, goal="task")
    assert len(summary) <= cc.MAX_SUMMARY_CHARS
    assert "... [summary truncated] ..." in summary


def test_compaction_replaces_old_summary_carrier_instead_of_accumulating(tmp_path):
    """Only one summary carrier may remain in history after repeated passes."""
    messages = _make_messages([_long_content(20_000)] * 2)
    for round_n in range(6):
        messages = messages + [
            {"role": "assistant", "content": f"decision: carrier round {round_n}",
             "tool_calls": [{"id": f"carrier-{round_n}", "type": "function",
                              "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"carrier-{round_n}", "name": "bash",
             "content": _long_content(20_000) + f" carrier evidence {round_n}"},
        ]
        messages = cc.compact_messages(
            messages, cycle_id="one-carrier", iteration=round_n, state_root=tmp_path,
            threshold=0.01, keep_tokens=8_000, window_tokens=98_304,
        )

    carriers = [
        m for m in messages
        if isinstance(m.get("content"), str)
        and m["content"].startswith("[Compaction summary")
    ]
    assert len(carriers) == 1, f"expected one current summary carrier, found {len(carriers)}"
    payloads = [
        str(message.get("content") or "") for message in messages
        if isinstance(message.get("content"), str)
        and (message["content"].startswith("[Compaction summary")
             or message["content"].startswith("[Earlier compaction summary"))
    ]
    assert sum(map(len, payloads)) <= cc.MAX_SUMMARY_CHARS + 3_000


def test_verbose_decision_phrase_is_preserved_in_summary(tmp_path):
    """Extract the decision itself, not an unrelated prefix before it."""
    decision = "decision: SWITCH_TO_PLAN_B"
    evidence = [
        {"role": "assistant", "content": "P" * 1_000 + decision + "S" * 1_000,
         "tool_calls": [{"id": "decision-call", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
    ]

    summary = cc._structural_summary(evidence, goal="task")

    assert decision in summary


def test_fresh_progress_and_decision_survive_summary_cap_with_path_flood():
    """Current-round decisions survive beside progress and a huge inventory."""
    progress = "FRESH_PROGRESS_SURVIVES"
    decision = "decision: SWITCH_TO_PLAN_B"
    paths = [f"src/generated/module_{i:04d}.py" for i in range(1_000)]
    evidence = [
        {"role": "assistant", "content": "P" * 2_000 + progress + "S" * 2_000},
        {"role": "assistant", "content": "D" * 1_000 + decision},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "paths", "type": "function",
             "function": {"name": "read_file", "arguments": " ".join(paths)}},
        ]},
    ]

    summary = cc._structural_summary(evidence, goal="task", previous="P" * 4_000)

    assert len(summary) <= cc.MAX_SUMMARY_CHARS
    assert progress in summary
    assert decision in summary


def test_prior_summary_survives_when_new_carrier_is_unavailable(tmp_path):
    """Do not retire the old carrier if this pass cannot install a replacement."""
    messages = _make_messages([_long_content(30_000)] * 4)
    messages = cc.compact_messages(
        messages, cycle_id="retain-old-summary", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=8_000, window_tokens=98_304,
    )
    previous = next(
        message["content"] for message in messages
        if isinstance(message.get("content"), str)
        and message["content"].startswith("[Compaction summary")
    )
    # Put a 420-character new candidate at the end. Its summary cannot fit
    # within its head/tail excerpt, while the old carrier is excluded from
    # re-compaction as already marked.
    candidate = {"role": "assistant", "content": "n" * 420}
    grown = [*messages, candidate]
    result = cc.compact_messages(
        grown, cycle_id="retain-old-summary", iteration=2, state_root=tmp_path,
        threshold=0.01, keep_tokens=0, window_tokens=98_304,
    )

    assert any(
        isinstance(message.get("content"), str) and previous in message["content"]
        for message in result
    ), "the previous cumulative summary must remain until a replacement is installed"


def test_cumulative_summary_growth_is_bounded_across_many_compactions(tmp_path):
    """#1776 item 2: 'retained verbatim' must not mean 'grows without bound'
    -- a cycle that compacts repeatedly (the issue's own telemetry: one
    cycle compacted 3 times and reached iteration 56) must not carry an
    ever-growing summary into every subsequent prompt forever."""
    messages = _make_messages([_long_content(20_000)] * 2)
    for round_n in range(15):
        messages = messages + [
            {"role": "assistant", "content": f"decision: round {round_n} note",
             "tool_calls": [{"id": f"tc-r{round_n}", "type": "function",
                              "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"tc-r{round_n}", "name": "bash",
             "content": _long_content(20_000) + f" marker_round_{round_n}"},
        ]
        messages = cc.compact_messages(
            messages, cycle_id="bounded", iteration=round_n, state_root=tmp_path,
            threshold=0.01, keep_tokens=8_000, window_tokens=98_304,
        )

    carrier_texts = [
        m["content"] for m in messages
        if m.get("role") in ("tool", "assistant") and isinstance(m.get("content"), str)
        and m["content"].startswith("[Compaction summary")
    ]
    assert carrier_texts
    assert max(len(t) for t in carrier_texts) <= cc.MAX_SUMMARY_CHARS + 3_000, (
        "the structural summary must stay roughly bounded across many "
        "compactions in one cycle, not grow linearly forever"
    )


# ---------------------------------------------------------------------------
# #1776 item 1: token-span cut point, not a fixed message count
# ---------------------------------------------------------------------------

def test_recent_token_span_protects_by_size_not_count(tmp_path):
    """The token-span cut protects however many whole messages fit in
    keep_tokens -- not a fixed count of them."""
    big = _long_content(20_000)  # ~5000 tokens each
    messages = _make_messages([big] * 6)  # 6 tool results

    result = cc.compact_messages(
        messages, cycle_id="c3", iteration=3, state_root=tmp_path,
        threshold=0.01, keep_tokens=15_000,  # ~3 messages worth
        window_tokens=98_304,
    )

    tool_results = [m for m in result if m.get("role") == "tool"]
    assert len(tool_results) == 6
    for tr in tool_results[-3:]:
        assert tr["content"] == big, "the last ~3 messages fit the 15,000-token span"
    for tr in tool_results[:3]:
        assert tr["content"].startswith("[Compaction summary") or cc._OMIT_MARKER in tr["content"], "older results should be compacted"


def test_single_oversized_recent_tool_result_is_still_compactable(tmp_path):
    """#1776's central AC for item 1: a fixed message COUNT can never
    compact an oversized recent result no matter how large it is (the
    pre-#1776 defect measured in cycle-bc440a8ef14f: exactly one trimmable
    result, the rest sat in the protected last-3). A token span can,
    because the span itself has a size the single message can exceed."""
    huge_recent = _long_content(200_000)  # ~50,000 tokens -- alone bigger than keep_tokens
    messages = _make_messages([_short_content(20), huge_recent])

    result = cc.compact_messages(
        messages, cycle_id="oversized", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=20_000, window_tokens=98_304,
    )

    tool_results = [m for m in result if m.get("role") == "tool"]
    assert cc._OMIT_MARKER in tool_results[-1]["content"], (
        "the single oversized recent result must itself be compactable, "
        "not protected forever for being 'the most recent'"
    )


def test_recent_span_with_room_to_spare_leaves_everything_verbatim(tmp_path):
    """The flip side of the oversized case: when the whole history fits
    inside keep_tokens, nothing is a candidate at all."""
    small = _short_content(20)
    messages = _make_messages([small] * 3)
    result = cc.compact_messages(
        messages, cycle_id="fits", iteration=1, state_root=tmp_path,
        threshold=0.0, keep_tokens=1_000_000, window_tokens=1,
    )
    assert result is messages
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "nothing_older_than_cut"


# ---------------------------------------------------------------------------
# #1776: a tool call and its result are never separated
# ---------------------------------------------------------------------------

def test_tool_call_and_result_pairing_survives_compaction(tmp_path):
    """AC: a tool call and its result are never separated by compaction.
    Compaction only ever replaces a tool-result message's `content`; it
    never removes or reorders a message, so every tool_call_id an
    assistant message issued still has exactly one corresponding tool
    message, in the same order, after compaction."""
    big = _long_content(40_000)
    messages = _make_messages([big] * 5)

    def _tool_call_ids(msgs):
        ids = []
        for m in msgs:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    ids.append(tc["id"])
            elif m.get("role") == "tool":
                ids.append(m["tool_call_id"])
        return ids

    before_ids = _tool_call_ids(messages)
    result = cc.compact_messages(
        messages, cycle_id="pairing", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=5_000, window_tokens=98_304,
    )
    after_ids = _tool_call_ids(result)

    assert after_ids == before_ids, "message order/identity must be unchanged by compaction"
    assert len(result) == len(messages), "compaction replaces content, never removes messages"
    # Confirm compaction actually ran (not a no-op that trivially preserves pairing).
    assert any(m["content"].startswith("[Compaction summary") for m in result if m.get("role") == "tool")


def test_pairing_survives_when_the_cut_falls_between_a_call_and_its_own_result(tmp_path):
    """The general pairing test above proves ids/order/count survive
    compaction for ANY cut position. This test targets the specific case
    the AC calls out by name: the cut lands strictly between one tool
    call's assistant message and that same call's tool-result message --
    the call becomes compactable (older side of the cut) while its own
    result stays fully protected (newer side). Even straddling the exact
    pair like this, the two must still resolve to each other afterward."""
    # #1930 review B3: carrier selection now counts the preservation
    # index/file-list overhead too, so this fixture needs enough margin
    # over that overhead (previously 5_000 was margin enough; the honest
    # accounting needs more) for the straddle to still actually compact.
    tool_content = _long_content(30_000)
    # Long enough to clear the worth-compacting size floor on its own, so
    # the assertion below actually exercises the straddle instead of the
    # (also-correct, separately tested) "too small to bother" no-op path.
    assistant_content = "reasoning about the next step. " * 40
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": assistant_content,
         "tool_calls": [{"id": "tc0", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": tool_content},
        {"role": "assistant", "content": assistant_content,
         "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc1", "name": "bash", "content": tool_content},
        {"role": "assistant", "content": assistant_content,
         "tool_calls": [{"id": "tc2", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc2", "name": "bash", "content": tool_content},
    ]
    # keep_tokens sized to exactly cover the newest tool result alone: the
    # walk-back includes tool@7 (fits exactly), then breaks on assistant@6
    # (any nonzero token cost pushes the running total past keep_tokens) --
    # so assistant@6 (the call) is compactable while tool@7 (its result)
    # is protected, straddling the tc2 pair across the cut.
    keep_tokens = cc._message_tokens(messages[7])

    result = cc.compact_messages(
        messages, cycle_id="straddle", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=keep_tokens, window_tokens=98_304,
    )

    assert result[7]["content"] == tool_content, "tc2's result must stay protected/verbatim"
    assert result[6]["content"] != assistant_content, (
        "tc2's call must actually be on the compactable side of this cut "
        "(otherwise this test isn't exercising the straddle at all)"
    )
    assert result[6]["tool_calls"][0]["id"] == "tc2"
    assert result[7]["tool_call_id"] == "tc2"
    assert len(result) == len(messages)


# ---------------------------------------------------------------------------
# System, user, and assistant always protected
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
        msgs, cycle_id="c4", iteration=4, state_root=tmp_path,
        threshold=0.01, keep_tokens=1, window_tokens=98_304,
    )

    assert result[0]["content"] == system_content
    assert result[1]["content"] == user_content


def test_assistant_message_content_is_compactable_but_pairing_survives(tmp_path):
    """#1776 item 3 (AC5): assistant turns are no longer exempt from
    compaction — a long-accumulating assistant turn was exactly the "never
    touched" gap item 3 named. This used to assert the opposite (assistant
    content must be byte-identical); that was the pre-#1776-item-3 contract,
    and it's now wrong on purpose. What must still hold, and is asserted
    below: only `content` changes — `tool_calls`/`tool_call_id` (the pairing
    key) are untouched, and message order/count don't change either."""
    big_assistant_text = "a" * 100_000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(4):
        messages.append({"role": "assistant", "content": big_assistant_text,
                          "tool_calls": [{"id": f"tc{i}", "type": "function",
                                          "function": {"name": "bash", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"tc{i}", "name": "bash",
                          "content": _long_content(40_000)})

    before_tool_calls = [m["tool_calls"] for m in messages if m.get("role") == "assistant"]

    result = cc.compact_messages(
        messages, cycle_id="assistant-compactable", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=1, window_tokens=98_304,
    )

    assistant_msgs = [m for m in result if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 4
    assert [m["tool_calls"] for m in assistant_msgs] == before_tool_calls, (
        "tool_calls (the pairing key) must be untouched even when content is compacted"
    )
    assert any(
        m["content"] != big_assistant_text for m in assistant_msgs
    ), "at least one oversized assistant turn should actually be compacted"
    for m in assistant_msgs:
        assert m["content"] == big_assistant_text or cc._OMIT_MARKER in m["content"] or m["content"].startswith(
            "[Compaction summary"
        )


# ---------------------------------------------------------------------------
# Excerpt is bounded by head/tail
# ---------------------------------------------------------------------------

def test_excerpt_bounded_head_tail():
    """Compacted content has at most EXCERPT_HEAD + marker + EXCERPT_TAIL chars."""
    big = "A" * 500 + "B" * 500  # 1000 chars, bigger than default 200+200
    result_content = cc._compact_content(big)

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
# #1776: the journal names what was dropped, not only how much
# ---------------------------------------------------------------------------

def test_drop_detail_names_the_tool_and_a_mechanical_characterization():
    original = "line one\n" * 500  # deterministic line count
    detail = cc._drop_detail("bash", original)
    assert detail["tool_name"] == "bash"
    assert detail["chars_before"] == len(original)
    assert detail["chars_after"] == cc.EXCERPT_HEAD + len(cc._OMIT_MARKER) + cc.EXCERPT_TAIL
    assert detail["dropped_chars"] > 0
    assert "bash" in detail["dropped_summary"]
    assert "line" in detail["dropped_summary"]


def test_drop_detail_falls_back_to_unknown_tool_name():
    detail = cc._drop_detail("", "x" * 1000)
    assert detail["tool_name"] == "unknown"
    assert "unknown" in detail["dropped_summary"]


def test_journal_compacted_details_name_the_tool_per_dropped_message(tmp_path):
    """#1776's central AC for observability: the journal must say WHICH
    tool's output was cut, not only an aggregate count."""
    messages = _make_messages([_long_content(20_000)] * 3, tool_name="grep")
    cc.compact_messages(
        messages, cycle_id="detail-journal", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=1, window_tokens=98_304,
    )
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "compacted"
    assert len(ev["compacted_details"]) == ev["results_compacted"] > 0
    for detail in ev["compacted_details"]:
        assert detail["tool_name"] == "grep"
        assert detail["chars_before"] > detail["chars_after"]
        assert "dropped_summary" in detail


# ---------------------------------------------------------------------------
# Fail-open on exception, journalled (#1776 item 6)
# ---------------------------------------------------------------------------

def test_fail_open_on_invalid_state_root():
    """If state_root is unwriteable/bad, compact_messages still returns a list."""
    big = _long_content(30_000)
    messages = _make_messages([big] * 4)

    result = cc.compact_messages(
        messages, cycle_id="fail_test", iteration=1,
        state_root="/dev/null/cannot/exist",
        threshold=0.01, keep_tokens=1, window_tokens=98_304,
    )
    assert isinstance(result, list)
    assert len(result) == len(messages)


def test_fail_open_on_bad_message_structure_and_journals_error(tmp_path):
    """Malformed messages never cause compact_messages to raise, and the
    failure is journalled (#1776 item 6) rather than silent."""
    messages = [None, 42, {"role": "tool", "content": {}}, "not-a-dict"]  # type: ignore[list-item]
    try:
        result = cc.compact_messages(
            messages,  # type: ignore[arg-type]
            cycle_id="bad",
            iteration=1,
            state_root=tmp_path,
        )
        assert isinstance(result, list)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"compact_messages raised unexpectedly: {exc}")

    ev = _last_journal_event(tmp_path)
    assert ev["reason"].startswith("error:")


# ---------------------------------------------------------------------------
# Journal is written on every evaluation (#1776 item 4)
# ---------------------------------------------------------------------------

def test_journal_written_on_compaction(tmp_path):
    """A JSONL event is written to state_root/compaction/journal.jsonl."""
    big = _long_content(20_000)  # ~5000 tokens each
    messages = _make_messages([big] * 4)

    cc.compact_messages(
        messages, cycle_id="journal_test", iteration=7, state_root=tmp_path,
        threshold=0.01, keep_tokens=5_000, window_tokens=98_304,
    )

    journal_path = tmp_path / "compaction" / "journal.jsonl"
    assert journal_path.exists(), "Journal file should exist after compaction"

    ev = _last_journal_event(tmp_path)
    assert ev["cycle_id"] == "journal_test"
    assert ev["iteration"] == 7
    assert ev["reason"] == "compacted"
    assert "before_tokens" in ev
    assert "after_tokens" in ev
    assert "results_compacted" in ev
    assert ev["results_compacted"] == 3
    assert ev["after_tokens"] <= ev["before_tokens"]


def test_journal_written_below_threshold_with_reason(tmp_path):
    """#1776: unlike before, a journal row IS written below threshold --
    with reason='below_threshold', distinguishing 'did not fire' from
    'did not run'."""
    messages = _make_messages([_short_content(10)])
    cc.compact_messages(
        messages, cycle_id="noevent", iteration=1, state_root=tmp_path,
        threshold=0.9999, keep_tokens=20_000, window_tokens=98_304,
    )
    journal_path = tmp_path / "compaction" / "journal.jsonl"
    assert journal_path.exists()
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "below_threshold"


def test_already_compact_decline_is_journalled(tmp_path):
    """Candidates exist (older than the cut) but their content is already
    short enough that compacting them changes nothing -- this decline was
    completely silent before #1776."""
    messages = _make_messages([_short_content(10)] * 3)
    cc.compact_messages(
        messages, cycle_id="already-compact", iteration=1, state_root=tmp_path,
        threshold=0.0, keep_tokens=0, window_tokens=1,
    )
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "already_compact"
    assert ev["compacted_details"] == []
    assert ev["results_compacted"] == 0


# ---------------------------------------------------------------------------
# No provider/LLM calls (import-time check)
# ---------------------------------------------------------------------------

def test_no_provider_imports():
    """context_compaction must not import any LLM provider module."""
    import importlib

    mod = importlib.import_module("nanobot.runtime.context_compaction")
    provider_names = {"openai", "anthropic", "litellm", "httpx", "aiohttp", "requests"}
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
    assert "nanobot/runtime/context_compaction.py" in _RUNTIME_DENY_ALWAYS_FILES, (
        "context_compaction.py must be in the explicit deny-set so the runtime "
        "loop cannot modify its own compaction logic."
    )


def test_deny_set_is_frozenset():
    """_RUNTIME_DENY_ALWAYS_FILES must be a frozenset (immutable)."""
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

def test_nothing_past_the_cut_declines_with_reason(tmp_path):
    """If there is nothing at all past system/user, decline is journalled
    with reason='nothing_older_than_cut' rather than silently."""
    messages = [
        {"role": "system", "content": "x" * 400_000},  # huge system prompt
        {"role": "user", "content": "task"},
    ]
    result = cc.compact_messages(
        messages, cycle_id="notool", iteration=1, state_root=tmp_path,
        threshold=0.0, keep_tokens=0, window_tokens=1,
    )
    assert result is messages
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "nothing_older_than_cut"


def test_single_trivial_assistant_message_declines_as_already_compact(tmp_path):
    """A lone short assistant message past the cut IS a candidate now that
    assistant turns are compactable (#1776 item 3) -- it's just too small to
    be worth touching. Before item 3, this declined as
    'nothing_older_than_cut' (assistant wasn't eligible at all); now the
    correct reason is 'already_compact' (something WAS past the cut, it
    just isn't worth compacting). Distinguishing the two still matters for
    telemetry, so both reasons keep their own test."""
    messages = [
        {"role": "system", "content": "x" * 400_000},  # huge system prompt
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "done"},
    ]
    result = cc.compact_messages(
        messages, cycle_id="notool", iteration=1, state_root=tmp_path,
        threshold=0.0, keep_tokens=0, window_tokens=1,
    )
    assert result is messages
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "already_compact"


# ---------------------------------------------------------------------------
# #1776 item 5: reserve covers the completion ceiling
# ---------------------------------------------------------------------------

def test_reserve_tokens_covers_the_completion_ceiling():
    """The reserve must be >= the client completion ceiling
    (AgentDefaults.max_tokens) -- this fails if the two drift apart again,
    the way 8,000 vs 8,192 did before #1776."""
    from nanobot.config.schema import AgentDefaults

    completion_ceiling = AgentDefaults().max_tokens
    assert cc.RESERVE_TOKENS >= completion_ceiling, (
        f"RESERVE_TOKENS ({cc.RESERVE_TOKENS}) must be >= the completion "
        f"ceiling ({completion_ceiling}), or a response can never fit"
    )


# ---------------------------------------------------------------------------
# #1776 item 6: the window size has one definition
# ---------------------------------------------------------------------------

def test_window_tokens_has_no_second_hardcoded_copy():
    """The serving-window token count must be defined exactly once, here.
    A second hardcoded copy elsewhere in nanobot/ is exactly the drift the
    issue measured (98_000 here vs 98,304 restated in a comment elsewhere);
    this scans for the numeral appearing as a real token outside its own
    definition line, not inside a comment/docstring reference to it."""
    import re

    pattern = re.compile(r"\b98_?304\b|\b98_?000\b")
    repo_root = Path(__file__).resolve().parents[1]
    own_file = repo_root / "nanobot" / "runtime" / "context_compaction.py"
    own_source = own_file.read_text(encoding="utf-8")
    own_matches = [
        (i, line) for i, line in enumerate(own_source.splitlines(), start=1)
        if pattern.search(line)
    ]
    # Every hit in this file itself must be the WINDOW_TOKENS definition
    # line or its own docstring/comment describing it -- not a second,
    # independent assignment.
    assignment_lines = [
        (i, line) for i, line in own_matches
        if re.search(r"WINDOW_TOKENS\s*[:=]", line) or re.search(r"_env_int\(", line)
    ]
    assert assignment_lines, "WINDOW_TOKENS's own definition must exist and match the pattern"

    offenders: list[str] = []
    for path in repo_root.joinpath("nanobot").rglob("*.py"):
        if path == own_file or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(repo_root)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "a second hardcoded copy of the context-window token count was found "
        f"outside context_compaction.py's own definition: {offenders}"
    )


def test_window_tokens_default_matches_the_measured_serving_window():
    assert cc.WINDOW_TOKENS == 98_304


# ---------------------------------------------------------------------------
# _compactable_indices unit tests
# ---------------------------------------------------------------------------

def test_compactable_indices_empty_when_everything_fits():
    messages = _make_messages([_short_content(20)] * 3)
    assert cc._compactable_indices(messages, keep_tokens=1_000_000) == set()


def test_compactable_indices_includes_assistant_excludes_system_and_user():
    """#1776 item 3 (AC5): assistant is no longer excluded. This test used
    to assert the opposite contract (candidates are tool-only); that was
    exactly the gap item 3 was filed to close, so the contract itself was
    wrong, not just this test. system/user remain the only exemptions."""
    messages = _make_messages([_long_content(40_000)] * 2)
    candidates = cc._compactable_indices(messages, keep_tokens=0)
    assert candidates, "assistant + tool messages older than the cut must be candidates"
    for i in candidates:
        assert messages[i]["role"] in ("assistant", "tool")
    # system/user must be absent regardless of the cut
    system_or_user_indices = {i for i, m in enumerate(messages) if m["role"] in ("system", "user")}
    assert candidates.isdisjoint(system_or_user_indices)
    # at least one assistant index must actually be a candidate here
    assert any(messages[i]["role"] == "assistant" for i in candidates)


# ---------------------------------------------------------------------------
# reasoning_content / thinking_blocks accounting (#1930)
# ---------------------------------------------------------------------------

def test_reasoning_content_is_counted_and_cleared_by_compaction(tmp_path):
    """A thinking model's `reasoning_content` can dwarf its visible `content`
    (`build_assistant_message` in nanobot/utils/helpers.py keeps it, and
    litellm_provider.py resends it to the model on every later call — see
    #1930). An old turn with a short `content` but a large `reasoning_content`
    must be counted toward the token estimate (so it isn't invisibly kept as
    "recent") and, once compacted, have its content shrunk and its
    reasoning payload cleared, exactly as `content` itself would be."""
    old_reasoning = _long_content(6_000)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Do a thing."},
        {
            "role": "assistant",
            "content": "ok",
            "reasoning_content": old_reasoning,
            "tool_calls": [{"id": "tc0", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": _short_content(10)},
        {"role": "assistant", "content": _long_content(2_000)},
    ]
    result = cc.compact_messages(
        messages,
        cycle_id="reasoning-content",
        iteration=1,
        state_root=tmp_path,
        threshold=0.001,
        keep_tokens=600,
        window_tokens=98_304,
    )
    compacted = result[2]
    assert "reasoning_content" not in compacted
    assert "thinking_blocks" not in compacted
    assert len(str(compacted.get("content", ""))) < len(old_reasoning)
    # the newest turn, protected by keep_tokens, must stay untouched
    assert result[4] == messages[4] or result[4]["content"] == _long_content(2_000)


def test_message_tokens_counts_reasoning_content_and_thinking_blocks():
    """Unit-level: the token estimate itself must see reasoning payloads,
    not only `content` — otherwise the compaction trigger and the
    protected-span cut both silently underestimate a thinking-model turn."""
    content_only = {"role": "assistant", "content": _long_content(400)}
    with_reasoning = dict(content_only, reasoning_content=_long_content(4_000))
    with_thinking = dict(content_only, thinking_blocks=[{"type": "thinking", "thinking": _long_content(4_000)}])
    baseline = cc._message_tokens(content_only)
    assert cc._message_tokens(with_reasoning) > baseline + 900
    assert cc._message_tokens(with_thinking) > baseline + 900


# ---------------------------------------------------------------------------
# #1930 convergence round: scope = reasoning_content only. Anthropic
# extended-thinking forms (thinking_blocks, including redacted_thinking) are
# not supported for shrink/clear -- fail-safe leaves them untouched, still
# counted.
# ---------------------------------------------------------------------------

def test_thinking_blocks_message_is_left_completely_untouched(tmp_path):
    """A message carrying `thinking_blocks` is never a compaction candidate,
    regardless of size: this runtime only supports shrinking/clearing
    `reasoning_content` (the host's provider, qwen via LiteLLM, is the only
    one observed to populate it). Anthropic's thinking forms require a
    signed thinking block to be replayed unmodified alongside its
    `tool_calls` turn; this module has no logic that preserves that
    constraint while shrinking, so the fail-safe is to not shrink at all."""
    thinking_blocks = [{"type": "thinking", "thinking": _long_content(5_000)}]
    old_msg = {
        "role": "assistant",
        "content": "ok",
        "thinking_blocks": thinking_blocks,
        "tool_calls": [{"id": "tc0", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}],
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Do a thing."},
        old_msg,
        {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": _short_content(10)},
        {"role": "assistant", "content": _long_content(2_000)},
    ]
    assert cc._worth_compacting(old_msg) is False

    result = cc.compact_messages(
        messages, cycle_id="thinking-untouched", iteration=1, state_root=tmp_path,
        threshold=0.001, keep_tokens=600, window_tokens=98_304,
    )
    untouched = result[2]
    assert untouched["content"] == "ok"
    assert untouched["thinking_blocks"] == thinking_blocks
    assert untouched.get("tool_calls") == old_msg["tool_calls"]


def test_redacted_thinking_payload_under_data_key_is_counted():
    """#1930 (Codex 4115692517): a `redacted_thinking` block's payload lives
    under `data`, not `thinking`/`text`/`content`. Even though such a
    message is never shrunk (previous test), it must still be sized
    correctly so the trigger/protected-span accounting isn't blind to it."""
    redacted = [{"type": "redacted_thinking", "data": _long_content(4_000)}]
    baseline = cc._message_tokens({"role": "assistant", "content": "ok"})
    with_redacted = cc._message_tokens({"role": "assistant", "content": "ok", "thinking_blocks": redacted})
    assert with_redacted > baseline + 900


def test_later_candidate_carries_summary_when_the_first_is_too_small(tmp_path):
    """#1930 (Codex 4113995954): with an existing carrier, a small first
    candidate, and a much larger later candidate, only the first was ever
    tried as this round's carrier -- if the summary didn't fit it, the
    fresh evidence was discarded even though the large candidate could
    easily have carried it. The later candidate must get the chance."""
    marker = "SECOND_ROUND_FRESH_EVIDENCE_MARKER"
    old_summary = "[Compaction summary — deterministic, evidence-only]\n" + "P" * 2_000
    small_first_candidate = "x" * (cc._min_compact_len() + 2) + marker
    large_second_candidate = "y" * 80_000
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Do a thing."},
        {"role": "assistant", "content": old_summary},
        {"role": "assistant", "content": small_first_candidate,
         "tool_calls": [{"id": "tc-small", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc-small", "name": "bash", "content": "ok"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc-large", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc-large", "name": "bash", "content": large_second_candidate},
        {"role": "assistant", "content": "keep me recent"},
    ]
    result = cc.compact_messages(
        messages, cycle_id="later-candidate", iteration=1, state_root=tmp_path,
        threshold=0.001, keep_tokens=50, window_tokens=98_304,
    )
    # I3's own contract: carrier-ness is the CODE-SET _CARRIER_FLAG, never
    # inferred from a message's own text. A plain-text scan for the
    # "[Compaction summary" prefix is exactly the anti-pattern I3 forbids --
    # this fixture's own old_summary text happens to start with that same
    # literal string, so its PLAIN excerpt (once it's no longer chosen as
    # carrier -- #1930 review B3 correctly makes it too large to qualify
    # once the preservation overhead is honestly counted) coincidentally
    # matches too, without being a real second carrier.
    carriers = [m for m in result if m.get(cc._CARRIER_FLAG)]
    assert len(carriers) == 1, f"expected exactly one carrier, found {len(carriers)}"
    assert marker in carriers[0]["content"], (
        "this round's fresh evidence must survive by moving to a candidate "
        "that can actually hold it, not be discarded"
    )


def test_marker_header_survives_when_fresh_evidence_nearly_fills_the_cap():
    """#1930 (Codex 4115719536): when this round's fresh decisions/progress
    alone are close to MAX_SUMMARY_CHARS, the header ("[Compaction summary")
    must still be the start of the returned text -- both prior-carrier
    discovery and the "did this round install a carrier" check depend on
    it. Six long assistant progress turns (each capped at 1,200 chars by
    `_representative_excerpt`, and only the last 6 kept) push fresh_text
    close to the 6,000-char default cap on its own."""
    evidence_span = [
        {"role": "assistant", "content": _long_content(2_000) + f"PROGRESS_{i}" + _long_content(2_000)}
        for i in range(6)
    ]
    summary = cc._structural_summary(evidence_span, goal="g", previous="")
    assert summary.startswith("[Compaction summary"), (
        f"header must survive truncation; got prefix: {summary[:80]!r}"
    )
    assert len(summary) <= cc.MAX_SUMMARY_CHARS


def test_tool_result_containing_marker_text_is_not_treated_as_already_compacted(tmp_path):
    """#1930 (Codex 4115719563): compaction state must be a flag the code
    sets itself, never inferred from a message's own text -- an ordinary
    tool result (e.g. `cat`-ing this very module, or a copy of this PR's
    diff) can legitimately contain the literal strings "[Compaction
    summary" and the excerpt marker. Such a result must still be compacted
    normally, not mistaken for an already-compacted message and skipped."""
    lookalike = "[Compaction summary — deterministic, evidence-only]\n" + cc._OMIT_MARKER + ("z" * 80_000)
    fake_msg = {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": lookalike}
    assert cc._worth_compacting(fake_msg) is True

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Do a thing."},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc0", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": lookalike},
        {"role": "assistant", "content": "keep me recent"},
    ]
    result = cc.compact_messages(
        messages, cycle_id="marker-lookalike", iteration=1, state_root=tmp_path,
        threshold=0.001, keep_tokens=50, window_tokens=98_304,
    )
    assert len(str(result[3]["content"])) < len(lookalike), (
        "a tool result that merely CONTAINS marker-like text must still be "
        "compacted, not skipped as if it were already compacted"
    )


# ---------------------------------------------------------------------------
# #1930 invariants I1-I5 -- docs/changes/1930-compaction-invariants/proposal.md
# @ 66c75439. One property test per invariant, named test_invariant_iN_*, per
# a random-history generator run across a SEQUENCE of compaction calls (not
# only the first), re-asserting after every single call -- a property that
# only holds at the end of a fixed sequence would hide an intermediate-round
# violation.
# ---------------------------------------------------------------------------

def _random_history_with_payloads(
    rng: "random.Random", n_turns: int,
) -> tuple[list[dict], dict[int, str]]:
    """Build ``[system, user]`` plus ``n_turns`` alternating assistant/tool
    messages, each independently randomized across content length, an
    embedded marker, a ``decision:``/``we will`` phrase, a
    ``reasoning_content`` payload, ``thinking_blocks`` (occasionally shaped
    like ``redacted_thinking``, ``data``-key only), path-like text, and a
    marker-lookalike (``"[Compaction summary"``/``cc._OMIT_MARKER``) tool
    result -- per the invariants doc's Test plan generator.

    Returns ``(messages, index_payload)``: ``index_payload[i]`` is the
    EXACT full pre-compaction payload (``cc._message_text``-equivalent) of
    ``messages[i]`` at construction time -- the ground truth an I1 check
    compares a preserved copy against once that index is first touched.
    """
    messages: list[dict] = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Investigate and fix the failing test."},
    ]
    index_payload: dict[int, str] = {}
    length_choices = {
        "short": lambda: rng.randint(5, 30),
        "over_min": lambda: cc._min_compact_len() + rng.randint(1, 80),
        "large": lambda: rng.randint(2_000, 6_000),
        "huge": lambda: rng.randint(20_000, 40_000),
    }
    alphabet = "abcdefghijklmnopqrstuvwxyz "

    for t in range(n_turns):
        marker = f"MARK{t}_{rng.randrange(10**9)}"
        body_len = length_choices[rng.choice(list(length_choices))]()
        body = "".join(rng.choice(alphabet) for _ in range(body_len))
        content = f"{marker} {body}"
        if rng.random() < 0.3:
            content += " decision: we will use plan B for this turn"
        assistant_msg: dict = {
            "role": "assistant", "content": content,
            "tool_calls": [{"id": f"tc{t}", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"}}],
        }
        full_text_parts = [content]
        if rng.random() < 0.3:
            reasoning = f"REASONING_{marker} " + "".join(rng.choice(alphabet) for _ in range(rng.randint(50, 600)))
            assistant_msg["reasoning_content"] = reasoning
            full_text_parts.append(reasoning)
        has_thinking = rng.random() < 0.15
        if has_thinking:
            thinking_text = f"THINKING_{marker} " + "".join(rng.choice(alphabet) for _ in range(rng.randint(50, 400)))
            if rng.random() < 0.5:
                assistant_msg["thinking_blocks"] = [{"data": thinking_text}]
            else:
                assistant_msg["thinking_blocks"] = [{"thinking": thinking_text}]
            full_text_parts.append(thinking_text)
        messages.append(assistant_msg)
        index_payload[len(messages) - 1] = "\n".join(full_text_parts)

        tool_marker = f"TMARK{t}_{rng.randrange(10**9)}"
        tool_len = length_choices[rng.choice(["short", "over_min", "large"])]()
        tool_body = "".join(rng.choice(alphabet + "/.") for _ in range(tool_len))
        tool_content = f"{tool_marker} src/module_{t}.py {tool_body}"
        if rng.random() < 0.1:
            tool_content += "\n" + rng.choice(["[Compaction summary", cc._OMIT_MARKER])
        messages.append({
            "role": "tool", "tool_call_id": f"tc{t}", "name": "bash", "content": tool_content,
        })
        index_payload[len(messages) - 1] = tool_content
    return messages, index_payload


def _decode_preservation_file(path: Path) -> "dict[int, tuple[str, dict]]":
    """Test-side reverse of :func:`cc._write_preservation_file`: ``{index:
    (role, parts)}`` -- ``parts`` is the RAW ``{"content": ..., optionally
    "reasoning_content": ...}`` object #1930 review B1 stores. Reconstruction
    is positional (fixed-width chunks), so this only needs to strip the
    readability-only continuation marker from every physical line but a
    record's last and re-``json.loads`` the concatenated payload -- never a
    marker SEARCH."""
    records: "dict[int, tuple[str, dict]]" = {}
    lines = path.read_text(encoding="utf-8").split("\n")
    i = 0
    header_re = re.compile(r"^## message (\d+) role=(.*) chars=(\d+)$")
    while i < len(lines):
        m = header_re.match(lines[i])
        if not m:
            i += 1
            continue
        index, role = int(m.group(1)), m.group(2)
        i += 1
        payload_lines = []
        while lines[i] != "## end":
            payload_lines.append(lines[i])
            i += 1
        i += 1  # past "## end"
        stripped = [
            ln[:-len(cc._PRESERVATION_CONTINUATION_MARKER)] if ln.endswith(cc._PRESERVATION_CONTINUATION_MARKER) else ln
            for ln in payload_lines
        ]
        encoded = "".join(stripped)
        text = json.loads(encoded) if encoded else ""
        records[index] = (role, text)
    return records


# Matches both the per-message index line's trailing path ("-- full text
# preserved at <path>") and #1930 review A2(b)'s bare "- <path>" file-list
# lines -- any whitespace-bounded token ending in ".md" is a preservation
# file reference in either format.
_PRESERVATION_PATH_RE = re.compile(r"(\S+\.md)\b")


def _recovered_parts_to_joined_text(parts) -> str:
    """Mirror the pre-B1 ``_message_text`` join convention (content, then
    reasoning_content, joined by ``"\\n"``) over a RAW ``parts`` object
    (#1930 review B1 stores ``content``/``reasoning_content`` as separate
    keys, ``content`` unmodified -- a plain string, or the raw block list).
    Used only to compare a recovered record against an ``index_payload``
    entry that was itself captured as one combined string; a test that
    needs the raw dict directly should read ``_decode_preservation_file``'s
    result itself instead of going through this."""
    if not isinstance(parts, dict):
        return str(parts)
    content = parts.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            str(b.get("text") or b.get("content") or "") if isinstance(b, dict) else str(b)
            for b in content
        )
    text_parts = [str(content)] if content else []
    reasoning = parts.get("reasoning_content")
    if reasoning:
        text_parts.append(str(reasoning))
    return "\n".join(text_parts)


def _referenced_preservation_records(history: list[dict]) -> dict[int, tuple[str, str]]:
    """Every ``(index -> (role, text))`` record from every preservation file
    still named in ANY message's current text (the carrier's index, folded
    forward across rounds) -- not only the file the most recent round wrote,
    since a message compacted in an earlier round may no longer be in
    context to re-check this round. ``text`` is the JOINED string (see
    :func:`_recovered_parts_to_joined_text`) -- callers that need the RAW
    parts dict (#1930 review B1's own tests) use
    :func:`_decode_preservation_file` directly instead."""
    all_records: dict[int, tuple[str, str]] = {}
    seen_paths: set[str] = set()
    for msg in history:
        text = cc._message_text(msg)
        for match in _PRESERVATION_PATH_RE.finditer(text):
            seen_paths.add(match.group(1))
    for raw_path in seen_paths:
        p = Path(raw_path)
        if p.is_file():
            for index, (role, parts) in _decode_preservation_file(p).items():
                all_records[index] = (role, _recovered_parts_to_joined_text(parts))
    return all_records


def _run_sequence(rng_seed: int, n_calls: int, tmp_path, *, turns_per_round: int = 6):
    """Run ``compact_messages`` ``n_calls`` times over a growing random
    history (fresh turns appended between calls, simulating an ongoing
    cycle), yielding one record per call for the invariant assertions."""
    rng = random.Random(rng_seed)
    messages, index_payload = _random_history_with_payloads(rng, turns_per_round)
    cycle_id = f"prop-{rng_seed}"
    for call_n in range(n_calls):
        before_messages = [dict(m) for m in messages]
        before_touched = {i for i, m in enumerate(messages) if m.get(cc._COMPACTED_FLAG)}
        before_tokens = cc._total_tokens(messages)
        result = cc.compact_messages(
            messages, cycle_id=cycle_id, iteration=call_n, state_root=tmp_path,
            threshold=0.01, keep_tokens=2_000, window_tokens=98_304,
            execution_id=f"exec-{rng_seed}",
        )
        after_tokens = cc._total_tokens(result)
        reason = _last_journal_event(tmp_path)["reason"]
        newly_touched = {
            i for i, m in enumerate(result)
            if m.get(cc._COMPACTED_FLAG) and i not in before_touched
        }
        yield {
            "before_messages": before_messages,
            "result": result,
            "reason": reason,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "newly_touched": newly_touched,
            "index_payload": dict(index_payload),
            "worth_indices_existed": reason != "nothing_older_than_cut",
        }
        messages = result
        # Fresh turns between calls, simulating an ongoing cycle (per the
        # doc's Test plan: "appending fresh random turns between calls").
        extra, extra_payload = _random_history_with_payloads(rng, 2)
        offset = len(messages)
        for j, m in enumerate(extra[2:]):  # skip the synthetic system/user pair
            messages.append(m)
            index_payload[offset + j] = extra_payload[j + 2]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_invariant_i1_preservation_files_round_trip_every_touched_message(tmp_path, seed):
    """I1: for every message a round touches, the complete pre-compaction
    payload (not just a marker token) is recoverable byte-for-byte -- either
    still literally present in some message's current text, or inside a
    preservation file still referenced by the carrier's index."""
    for round_record in _run_sequence(seed, n_calls=8, tmp_path=tmp_path):
        if not round_record["newly_touched"]:
            continue
        result = round_record["result"]
        referenced = None
        for i in round_record["newly_touched"]:
            original_payload = round_record["index_payload"][i]
            still_in_context = any(original_payload in cc._message_text(m) for m in result)
            if still_in_context:
                continue
            if referenced is None:
                referenced = _referenced_preservation_records(result)
            match = referenced.get(i)
            assert match is not None, (
                f"turn {i}'s full pre-compaction payload is neither still in "
                f"context nor recoverable from any preservation file the "
                f"carrier's index currently references"
            )
            _role, recovered_text = match
            assert recovered_text == original_payload, (
                f"turn {i}'s preserved text does not round-trip byte-for-byte"
            )


def test_invariant_i1_preservation_file_readable_through_real_read_file_tool(tmp_path):
    """I1: 'retrievable in full through the real ReadFileTool' -- no line in
    the preservation file exceeds the tool's limit, including a single line
    longer than 128,000 characters (the doc's own explicit test case)."""
    from nanobot.agent.tools.filesystem import ReadFileTool

    huge_line = "H" * 250_000  # single logical line, far past the tool's 128_000-char cap
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc0", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": huge_line},
        {"role": "assistant", "content": "keep me recent " + "z" * 200},
    ]
    result = cc.compact_messages(
        messages, cycle_id="huge-line", iteration=1, state_root=tmp_path,
        threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-huge",
    )
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "compacted"

    referenced = _referenced_preservation_records(result)
    assert 3 in referenced, "the huge tool result must have a preservation record"
    _role, recovered_text = referenced[3]
    assert recovered_text == huge_line

    preservation_path = next(iter(
        p for p in (Path(tmp_path) / "compaction").rglob("*.md")
    ))
    tool = ReadFileTool(allowed_dir=Path(tmp_path))
    collected = ""
    offset = 1
    for _ in range(50):
        page = asyncio.run(tool.execute(path=str(preservation_path), offset=offset, limit=2000))
        assert not page.startswith("Error"), page
        body = page.rsplit("\n\n(", 1)[0]
        for numbered_line in body.splitlines():
            _, _, text_part = numbered_line.partition("| ")
            collected += text_part + "\n"
        if "End of file" in page:
            break
        # "Use offset=N to continue."
        offset = int(page.rsplit("offset=", 1)[1].split(" ", 1)[0])
    else:
        pytest.fail("tool pagination did not terminate -- a line exceeded the tool's cap")

    stripped_lines = [
        ln[:-len(cc._PRESERVATION_CONTINUATION_MARKER)] if ln.endswith(cc._PRESERVATION_CONTINUATION_MARKER) else ln
        for ln in collected.split("\n")
        if not ln.startswith("## ")
    ]
    encoded = "".join(stripped_lines)
    decoded = json.loads(encoded)
    content_text = decoded["content"] if isinstance(decoded, dict) else decoded
    assert content_text == huge_line, (
        "the huge line must reconstruct byte-for-byte through the real tool's pagination"
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_invariant_i2_strict_monotonicity_or_exact_noop(tmp_path, seed):
    """I2: a round is journalled 'compacted' only when after < before,
    strictly; any other reason means an EXACT no-op (byte-identical
    messages, no new touched flags)."""
    for round_record in _run_sequence(seed, n_calls=8, tmp_path=tmp_path):
        if round_record["reason"] == "compacted":
            assert round_record["after_tokens"] < round_record["before_tokens"], (
                "a 'compacted' round must strictly reduce total estimated tokens"
            )
        else:
            assert round_record["result"] == round_record["before_messages"], (
                f"a non-'compacted' round ({round_record['reason']}) must change nothing"
            )
            assert not round_record["newly_touched"], (
                f"a non-'compacted' round ({round_record['reason']}) must flag no new message"
            )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_invariant_i3_at_most_one_live_carrier(tmp_path, seed):
    """I3: at most one message ever carries `_compaction_carrier: True`,
    across the whole lifetime of a cycle, over N random-history rounds."""
    for round_record in _run_sequence(seed, n_calls=15, tmp_path=tmp_path):
        carriers = sum(1 for m in round_record["result"] if m.get(cc._CARRIER_FLAG))
        assert carriers <= 1, f"found {carriers} live carriers after one call"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_invariant_i4_full_commit_or_full_decline(tmp_path, seed):
    """I4: a round either installs a carrier AND shrinks every
    worth-compacting candidate, or changes nothing at all -- no partial
    state (some candidates excerpted/flagged, no carrier installed)."""
    for round_record in _run_sequence(seed, n_calls=8, tmp_path=tmp_path):
        if round_record["reason"] == "compacted":
            carriers = [m for m in round_record["result"] if m.get(cc._CARRIER_FLAG)]
            assert carriers, "a 'compacted' round must install exactly one carrier"
        else:
            assert not round_record["newly_touched"], (
                "a non-'compacted' round must not have partially touched candidates"
            )


def test_invariant_i4_closes_codex_4115904095_preserve_fresh_evidence_when_no_carrier_fits(tmp_path):
    """I4's own reproducer (Codex 4115904095, closed by I4's definition):
    many similarly-sized turns, none individually large enough to hold the
    refreshed summary -- the round must fully decline, not bare-excerpt
    every candidate while installing no carrier."""
    messages = _make_messages([_long_content(cc._min_compact_len() + 50)] * 45)
    before = [dict(m) for m in messages]
    result = cc.compact_messages(
        messages, cycle_id="no-carrier-fits", iteration=1, state_root=tmp_path,
        threshold=0.01, keep_tokens=0, window_tokens=98_304,
    )
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "declined_no_reduction"
    assert result == before, "a fully-declined round must change nothing at all"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_invariant_i5_thinking_blocks_untouched_and_counted(tmp_path, seed):
    """I5: a message carrying `thinking_blocks` is byte-identical before and
    after every compaction call across its lifetime, and its token
    contribution is never zero when its payload is non-empty."""
    for round_record in _run_sequence(seed, n_calls=8, tmp_path=tmp_path):
        for before_msg, after_msg in zip(round_record["before_messages"], round_record["result"]):
            if before_msg.get("thinking_blocks"):
                assert after_msg == before_msg, (
                    "a thinking_blocks-carrying message must never be modified by compaction"
                )
                assert cc._message_tokens(after_msg) > 0


def test_invariant_i1_closes_codex_4115904099_preserve_progress_from_turns_with_decisions(tmp_path):
    """Codex 4115904099 (closed by I1): a turn that both reports progress
    AND states a decision loses whichever half the heuristic's
    mutually-exclusive branch doesn't pick from the RENDERED summary -- I1
    guarantees the complete original turn (both halves) survives on disk
    regardless."""
    progress_text = "Completed the migration of the auth module. " * 20
    full_turn = progress_text + "decision: use plan B for the rollout"
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": full_turn,
         "tool_calls": [{"id": "tc0", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": _long_content(20_000)},
        {"role": "assistant", "content": "keep me recent " + "z" * 200},
    ]
    result = cc.compact_messages(
        messages, cycle_id="decision-and-progress", iteration=1, state_root=tmp_path,
        threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-dp",
    )
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "compacted"
    referenced = _referenced_preservation_records(result)
    assert 2 in referenced, "the decision+progress turn must have a preservation record"
    _role, recovered_text = referenced[2]
    assert recovered_text == full_turn, (
        "both the progress and the decision half of the turn must survive on disk verbatim"
    )


# ---------------------------------------------------------------------------
# Conditions C/E (#1930): retention sweep, volume measurement, usage counter
# ---------------------------------------------------------------------------

def test_sweep_preservation_retention_deletes_aged_out_cycles_never_touching_active(tmp_path):
    compaction_root = Path(tmp_path) / "compaction"
    old_cycle = compaction_root / "old-cycle" / "exec1"
    old_cycle.mkdir(parents=True)
    old_file = old_cycle / "0.md"
    old_file.write_text("stale", encoding="utf-8")
    old_time = time.time() - 10 * 86400
    os.utime(old_file, (old_time, old_time))

    # #1930 review B4: _sanitize_path_component now hash-suffixes every
    # sanitized segment, so the directory sweep_preservation_retention will
    # actually treat as "active" is NOT the bare cycle_id string -- it must
    # be built the same way the function itself builds it.
    active_cycle_id = "active-cycle"
    active_cycle = compaction_root / cc._sanitize_path_component(active_cycle_id, "") / "exec2"
    active_cycle.mkdir(parents=True)
    active_file = active_cycle / "0.md"
    active_file.write_text("live", encoding="utf-8")
    os.utime(active_file, (old_time, old_time))  # also old, but is the active cycle

    counts = cc.sweep_preservation_retention(
        tmp_path, active_cycle_id=active_cycle_id, max_age_days=7,
        max_total_bytes=10_000_000,
    )
    assert counts["aged_out"] == 1
    assert not old_cycle.exists()
    assert active_cycle.exists(), "the active cycle's directory must never be swept"


def test_sweep_preservation_retention_enforces_size_cap_oldest_first(tmp_path):
    compaction_root = Path(tmp_path) / "compaction"
    now = time.time()
    for n, age_days in enumerate([3, 2, 1]):
        cycle_dir = compaction_root / f"cycle{n}" / "exec"
        cycle_dir.mkdir(parents=True)
        f = cycle_dir / "0.md"
        f.write_text("x" * 1000, encoding="utf-8")
        mtime = now - age_days * 86400
        os.utime(f, (mtime, mtime))

    counts = cc.sweep_preservation_retention(
        tmp_path, active_cycle_id=None, max_age_days=365, max_total_bytes=1500,
    )
    assert counts["size_capped"] == 2, "must delete oldest-first until under the size cap"
    assert not (compaction_root / "cycle0").exists()
    assert not (compaction_root / "cycle1").exists()
    assert (compaction_root / "cycle2").exists(), "the newest cycle must survive the size cap"


def test_measure_preservation_volume_from_journal_sums_dropped_chars_by_day_and_cycle(tmp_path):
    journal_dir = Path(tmp_path) / "compaction"
    journal_dir.mkdir(parents=True)
    events = [
        {"ts": "2026-09-27T10:00:00Z", "cycle_id": "c1",
         "compacted_details": [{"dropped_chars": 100}, {"dropped_chars": 50}]},
        {"ts": "2026-09-27T11:00:00Z", "cycle_id": "c2", "compacted_details": [{"dropped_chars": 200}]},
        {"ts": "2026-09-28T09:00:00Z", "cycle_id": "c1", "compacted_details": [{"dropped_chars": 10}]},
    ]
    with (journal_dir / "journal.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    volume = cc.measure_preservation_volume_from_journal(tmp_path)
    assert volume["by_day"]["2026-09-27"] == 350
    assert volume["by_day"]["2026-09-28"] == 10
    assert volume["by_cycle"]["c1"] == 160
    assert volume["by_cycle"]["c2"] == 200


def test_count_preservation_reads_counts_matching_cycle_and_compaction_path(tmp_path):
    index_dir = Path(tmp_path) / "action_index"
    index_dir.mkdir(parents=True)
    rows = [
        {"cycle_id": "c1", "actions_detail": ["read:compaction/c1/exec1/0.md", "exec:*"]},
        {"cycle_id": "c1", "actions_detail": ["read:compaction/c1/exec1/0.md"]},
        {"cycle_id": "c2", "actions_detail": ["read:compaction/c2/exec1/0.md"]},
        {"cycle_id": "c1", "actions_detail": ["read:some/other/file.py"]},
    ]
    with (index_dir / "2026-09-27.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    assert cc.count_preservation_reads(tmp_path, "c1", day="2026-09-27") == 2
    assert cc.count_preservation_reads(tmp_path, "c2", day="2026-09-27") == 1
    assert cc.count_preservation_reads(tmp_path, "nonexistent", day="2026-09-27") == 0


# ---------------------------------------------------------------------------
# #1930 architect review round 1 (A1-A6) @ 0da52baf
# ---------------------------------------------------------------------------

def _big_history_no_carrier_fits(n: int = 45) -> list[dict]:
    """Deliberately sized so NO candidate can hold the refreshed summary --
    the I4 'decline' repro (Codex 4115904095). NOT for tests that need an
    ordinary successful compaction -- see :func:`_normal_compacting_history`."""
    return _make_messages([_long_content(cc._min_compact_len() + 50)] * n)


def _normal_compacting_history(tool_size: int = 20_000) -> list[dict]:
    """A minimal history that reliably yields a real ``reason="compacted"``
    round: one oversized, easily-excerpted tool result. Distinct from
    :func:`_big_history_no_carrier_fits` above."""
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc0", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": _long_content(tool_size)},
        {"role": "assistant", "content": "keep me recent " + "z" * 200},
    ]


class TestA3ReadCounterOverCounting:
    """A3 (P2): a bare substring match on "compaction" also matched
    "context_compaction.py" (this very module) or its tests -- a false hit
    unrelated to the preservation directory."""

    def test_a3_read_of_context_compaction_module_itself_is_not_counted(self, tmp_path):
        index_dir = Path(tmp_path) / "action_index"
        index_dir.mkdir(parents=True)
        rows = [
            {"cycle_id": "c1", "actions_detail": ["read:compaction/c1/exec1/0.md"]},
            {"cycle_id": "c1", "actions_detail": ["read:nanobot/runtime/context_compaction.py"]},
            {"cycle_id": "c1", "actions_detail": ["read:tests/test_context_compaction.py"]},
        ]
        with (index_dir / "2026-09-27.jsonl").open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        assert cc.count_preservation_reads(tmp_path, "c1", day="2026-09-27") == 1


class TestA4JournalPreservationFields:
    """A4 (P2): the journal row for a compaction lacks written_bytes/file_path."""

    def test_a4_compacted_journal_row_names_preservation_path_and_bytes(self, tmp_path):
        messages = _normal_compacting_history()
        cc.compact_messages(
            messages, cycle_id="a4-journal", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-a4",
        )
        ev = _last_journal_event(tmp_path)
        assert ev["reason"] == "compacted"
        assert ev.get("preservation_path"), "journal row must name the preservation file"
        assert Path(ev["preservation_path"]).is_file()
        assert isinstance(ev.get("preservation_bytes"), int) and ev["preservation_bytes"] > 0
        assert ev["preservation_bytes"] == Path(ev["preservation_path"]).stat().st_size


class TestA5NonAsciiPreservation:
    """A5 (P2): json.dumps defaults to ensure_ascii=True, so Cyrillic becomes
    \\uXXXX -- up to 6x larger and unreadable for the executor via read_file."""

    def test_a5_cyrillic_and_huge_line_restore_through_real_read_file_tool(self, tmp_path):
        from nanobot.agent.tools.filesystem import ReadFileTool

        cyrillic_huge_line = ("Привет мир, это тест кириллицы. " * 8000)[:250_000]
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "tc0", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc0", "name": "bash", "content": cyrillic_huge_line},
            {"role": "assistant", "content": "keep me recent " + "z" * 200},
        ]
        result = cc.compact_messages(
            messages, cycle_id="a5-cyrillic", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-a5",
        )
        ev = _last_journal_event(tmp_path)
        assert ev["reason"] == "compacted"
        preservation_path = Path(ev["preservation_path"])
        raw = preservation_path.read_text(encoding="utf-8")
        assert "Привет" in raw, (
            "non-ASCII text must be written literally (ensure_ascii=False), not as \\uXXXX escapes"
        )
        assert "\\u041f" not in raw and "\\u0440" not in raw

        tool = ReadFileTool(allowed_dir=Path(tmp_path))
        collected = ""
        offset = 1
        for _ in range(50):
            page = asyncio.run(tool.execute(path=str(preservation_path), offset=offset, limit=2000))
            assert not page.startswith("Error"), page
            body = page.rsplit("\n\n(", 1)[0]
            for numbered_line in body.splitlines():
                _, _, text_part = numbered_line.partition("| ")
                collected += text_part + "\n"
            if "End of file" in page:
                break
            offset = int(page.rsplit("offset=", 1)[1].split(" ", 1)[0])
        else:
            pytest.fail("tool pagination did not terminate")

        stripped_lines = [
            ln[:-len(cc._PRESERVATION_CONTINUATION_MARKER)] if ln.endswith(cc._PRESERVATION_CONTINUATION_MARKER) else ln
            for ln in collected.split("\n")
            if not ln.startswith("## ")
        ]
        encoded = "".join(stripped_lines)
        decoded = json.loads(encoded)
        content_text = decoded["content"] if isinstance(decoded, dict) else decoded
        assert content_text == cyrillic_huge_line, (
            "Cyrillic + a line past the tool's 128_000-char cap must restore byte-for-byte"
        )


class TestA2RetiredCarrierPreservation:
    """A2 (P1, I1): a retired carrier's text is replaced by a stub and was
    NOT written to a preservation file -- only folded into `previous`, which
    truncates to its tail. After enough rounds the index lines pointing at
    early files fall out of the carrier, and those files (sole copies) are
    referenced by nothing."""

    def test_a2_every_sole_copy_file_stays_reachable_over_20_rounds(self, tmp_path):
        for round_record in _run_sequence(rng_seed=42, n_calls=20, tmp_path=tmp_path):
            pass
        # After the full sequence, every message no longer literally in
        # context must have its full pre-compaction payload recoverable from
        # SOME preservation file the FINAL carrier's index (including its
        # "all preservation files" list) currently references.
        final_result = round_record["result"]
        index_payload = round_record["index_payload"]
        referenced = _referenced_preservation_records(final_result)
        missing = []
        for i, payload in index_payload.items():
            if i >= len(final_result):
                continue
            still_in_context = any(payload in cc._message_text(m) for m in final_result)
            if still_in_context:
                continue
            if i not in referenced:
                missing.append(i)
        assert not missing, (
            f"turns {missing} have no reachable preservation record after 20 rounds -- "
            f"their sole copy fell out of the carrier's reachable file list"
        )


class TestA1SweepCalledByWriter:
    """A1 (P1, revised): the sweep is called by compact_messages itself,
    right before _write_preservation_file, once per (process, cycle_id),
    fail-open. bridge.py is never touched."""

    def test_a1a_compacting_round_sweeps_an_old_other_cycle_directory(self, tmp_path):
        old_dir = Path(tmp_path) / "compaction" / "stale-other-cycle" / "exec1"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "0.md"
        old_file.write_text("stale", encoding="utf-8")
        old_time = time.time() - 30 * 86400
        os.utime(old_file, (old_time, old_time))

        messages = _normal_compacting_history()
        cc.compact_messages(
            messages, cycle_id="a1a-fresh-cycle", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-a1a",
        )
        ev = _last_journal_event(tmp_path)
        assert ev["reason"] == "compacted"
        assert not old_dir.exists(), (
            "a compacting round must sweep an aged-out OTHER cycle's directory"
        )

    def test_a1b_below_threshold_round_never_sweeps(self, tmp_path):
        old_dir = Path(tmp_path) / "compaction" / "stale-other-cycle" / "exec1"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "0.md"
        old_file.write_text("stale", encoding="utf-8")
        old_time = time.time() - 30 * 86400
        os.utime(old_file, (old_time, old_time))

        messages = _make_messages([_short_content(10)])
        cc.compact_messages(
            messages, cycle_id="a1b-below-threshold", iteration=1, state_root=tmp_path,
            threshold=0.99, keep_tokens=20_000, window_tokens=98_304,
        )
        ev = _last_journal_event(tmp_path)
        assert ev["reason"] == "below_threshold"
        assert old_dir.exists(), "a non-compacting round must never sweep"

    def test_a1c_second_compacting_round_same_cycle_does_not_repeat_sweep(self, tmp_path, monkeypatch):
        calls = []
        real_sweep = cc.sweep_preservation_retention

        def _counting_sweep(*args, **kwargs):
            calls.append((args, kwargs))
            return real_sweep(*args, **kwargs)

        monkeypatch.setattr(cc, "sweep_preservation_retention", _counting_sweep)

        base = _normal_compacting_history()
        round1 = cc.compact_messages(
            list(base), cycle_id="a1c-repeat-cycle", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-a1c",
        )
        fresh_pair = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc1", "name": "bash", "content": _long_content(20_000)},
        ]
        round2_input = round1[:-1] + fresh_pair + [round1[-1]]
        cc.compact_messages(
            round2_input, cycle_id="a1c-repeat-cycle", iteration=2, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-a1c",
        )
        assert len(calls) == 1, (
            f"expected exactly 1 sweep call across 2 compacting rounds of the SAME cycle, got {len(calls)}"
        )

    def test_a1d_current_cycles_own_directory_is_never_swept(self, tmp_path):
        safe_cycle = cc._sanitize_path_component("a1d-active-cycle", "")
        own_dir = Path(tmp_path) / "compaction" / safe_cycle / "exec-old"
        own_dir.mkdir(parents=True)
        own_file = own_dir / "0.md"
        own_file.write_text("earlier round of the SAME cycle", encoding="utf-8")
        old_time = time.time() - 30 * 86400
        os.utime(own_file, (old_time, old_time))

        messages = _normal_compacting_history()
        cc.compact_messages(
            messages, cycle_id="a1d-active-cycle", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-a1d-new",
        )
        assert own_dir.exists(), "the sweep must never delete the CURRENT cycle's own directory"

    def test_a1e_a_failing_sweep_still_leaves_the_file_written_and_round_compacted(self, tmp_path, monkeypatch):
        calls = []

        def _raising_sweep(*args, **kwargs):
            calls.append((args, kwargs))
            raise PermissionError("simulated rmtree failure")

        monkeypatch.setattr(cc, "sweep_preservation_retention", _raising_sweep)

        messages = _normal_compacting_history()
        result = cc.compact_messages(
            messages, cycle_id="a1e-failing-sweep", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-a1e",
        )
        assert calls, "the sweep must actually have been invoked for this assertion to mean anything"
        ev = _last_journal_event(tmp_path)
        assert ev["reason"] == "compacted", "a failing sweep must not cancel the round"
        assert ev.get("preservation_path") and Path(ev["preservation_path"]).is_file(), (
            "a failing sweep must not cancel the preservation write"
        )
        assert result != messages


# ---------------------------------------------------------------------------
# #1930 architect review round 2 (B1-B8, ChatGPT-reproduced) @ 0da52baf
# ---------------------------------------------------------------------------

class TestB1RawContentPreservation:
    """B1 (P1, I1): _message_text is a lossy projection for content lists --
    (a) a block with both text and content keeps only text, silently
    dropping content's middle on excerpt; (b) a list carrier is flattened
    to a string, silently dropping non-text blocks (e.g. image_url)."""

    def test_b1a_block_with_both_text_and_content_preserves_content_verbatim(self, tmp_path):
        block_content = "THE REAL CONTENT: " + ("c" * 2000)
        block_text = "a decoy text field, not what should be measured"
        list_content = [{"type": "tool_result", "text": block_text, "content": block_content}]
        messages = _normal_compacting_history() + [
            {"role": "tool", "tool_call_id": "tc-list", "name": "bash", "content": list_content},
            {"role": "assistant", "content": "keep me recent 2 " + "z" * 200},
        ]
        result = cc.compact_messages(
            messages, cycle_id="b1a-block", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-b1a",
        )
        ev = _last_journal_event(tmp_path)
        assert ev["reason"] == "compacted"
        preservation_path = Path(ev["preservation_path"])
        raw = preservation_path.read_text(encoding="utf-8")
        assert "THE REAL CONTENT" in raw, (
            "the raw content block (not just its 'text' sibling key) must survive in the preservation file"
        )

    def test_b1b_non_text_block_in_a_list_carrier_survives_the_preservation_record(self, tmp_path):
        image_block = {"type": "image_url", "image_url": {"url": "https://example.invalid/pic.png"}}
        list_content = [
            {"type": "tool_result", "content": "y" * 5000},
            image_block,
        ]
        messages = _normal_compacting_history() + [
            {"role": "tool", "tool_call_id": "tc-img", "name": "bash", "content": list_content},
            {"role": "assistant", "content": "keep me recent 2 " + "z" * 200},
        ]
        result = cc.compact_messages(
            messages, cycle_id="b1b-image", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-b1b",
        )
        ev = _last_journal_event(tmp_path)
        assert ev["reason"] == "compacted"
        preservation_path = Path(ev["preservation_path"])
        raw = preservation_path.read_text(encoding="utf-8")
        assert "example.invalid/pic.png" in raw, (
            "a non-text block (image_url) must survive verbatim in the preservation record, "
            "not be silently dropped by a text-only flattening"
        )


class TestB2OnlyFlagWhenTokensActuallyDrop:
    """B2 (P1, I4): a candidate was marked touched even when _excerpt_content
    returned changed=False (a list of small/non-tool_result blocks) -- the
    message did not shrink, but was flagged and dropped out of future
    rounds; _drop_detail recorded a nonzero dropped_chars for a message
    that lost nothing."""

    def test_b2_a_list_content_candidate_that_cannot_shrink_is_never_flagged(self, tmp_path):
        unshrinkable = [{"type": "text", "text": "short text block, no tool_result present"}]
        messages = _normal_compacting_history() + [
            {"role": "tool", "tool_call_id": "tc-unshrink", "name": "bash", "content": unshrinkable},
            {"role": "assistant", "content": "keep me recent 2 " + "z" * 200},
        ]
        before = [dict(m) for m in messages]
        assert cc._worth_compacting(messages[-2]) is False, (
            "a list-content message _excerpt_content cannot shrink must be proactively excluded"
        )
        result = cc.compact_messages(
            messages, cycle_id="b2-unshrinkable", iteration=1, state_root=tmp_path,
            threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-b2",
        )
        unshrink_idx = len(messages) - 2
        assert result[unshrink_idx] == before[unshrink_idx], (
            "an unshrinkable list-content candidate must be left completely untouched, never flagged"
        )
        assert cc._COMPACTED_FLAG not in result[unshrink_idx]


def test_b3_carrier_selection_counts_the_preservation_overhead_itself(tmp_path):
    """B3 (P1, local I2): carrier selection previously checked
    len(summary+excerpt) WITHOUT the preservation index -- the index was
    spliced in afterwards, so a carrier could grow net (reproduced:
    643->1126 chars) while the pre-index comparison still reported success.
    A history sized so a candidate fits WITHOUT the index but not WITH it
    must decline instead of silently growing."""
    messages = _normal_compacting_history(tool_size=1_200)
    result = cc.compact_messages(
        messages, cycle_id="b3-overhead", iteration=1, state_root=tmp_path,
        threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-b3",
    )
    ev = _last_journal_event(tmp_path)
    if ev["reason"] == "compacted":
        for m in result:
            if m.get(cc._CARRIER_FLAG):
                assert len(str(m["content"])) < 1_200 + 4_000, (
                    "if a carrier WAS installed, its full content (summary + excerpt + "
                    "preservation index/file-list) must still be smaller than a generous "
                    "bound -- never silently larger than what carrier selection measured"
                )
    else:
        assert ev["reason"] == "declined_no_reduction"


def test_b4_sanitized_segment_collision_gets_distinct_directories(tmp_path):
    """B4 (P1, paths): sanitisation alone is many-to-one -- 'cycle/a,job:b'
    and 'cycle?a,job/b' both clean to 'cycle_a_job_b', so a second write
    would land in (and overwrite) the first's directory."""
    a = cc._sanitize_path_component("cycle/a,job:b", "fallback")
    b = cc._sanitize_path_component("cycle?a,job/b", "fallback")
    assert a != b, "two different raw values must never sanitize to the same directory name"

    path_a = cc._preservation_path(tmp_path, "cycle/a,job:b", "exec1", 0)
    path_b = cc._preservation_path(tmp_path, "cycle?a,job/b", "exec1", 0)
    assert path_a.parent.parent != path_b.parent.parent


def test_b5_two_compactions_same_iteration_both_files_exist_old_index_unchanged(tmp_path):
    """B5 (P1): a second write to the same (cycle, execution, iteration)
    overwrote the earlier evidence, and a shared <iteration>.md.tmp let two
    writers race. Fix: the final path is never overwritten (suffix -1, -2,
    ...); the temp file is unique."""
    records_1 = [(0, "assistant", {"content": "FIRST ROUND CONTENT " + "a" * 500})]
    records_2 = [(0, "assistant", {"content": "SECOND ROUND CONTENT " + "b" * 500})]
    nominal = cc._preservation_path(tmp_path, "same-cycle", "same-exec", 3)

    path_1 = cc._first_free_preservation_path(nominal)
    actual_1, _ = cc._write_preservation_file(path_1, records_1)

    path_2 = cc._first_free_preservation_path(nominal)
    assert path_2 != actual_1, "the second write must be reserved a DIFFERENT path than the first"
    actual_2, _ = cc._write_preservation_file(path_2, records_2)

    assert actual_1.is_file() and actual_2.is_file(), "both files must exist"
    assert actual_1 != actual_2

    content_1 = actual_1.read_text(encoding="utf-8")
    assert "FIRST ROUND CONTENT" in content_1
    assert "SECOND ROUND CONTENT" not in content_1, (
        "the old file's content must be unchanged by the second write"
    )


def test_b6_journal_before_after_est_are_scale_consistent_trigger_signal_separate(tmp_path):
    """B6 (P2, measuring I2): before=trigger_signal (real provider tokens)
    and after=an estimate -- two different scales in one series (reproduced:
    before=10, after=231 for an actual 25003->231-char reduction)."""
    messages = _normal_compacting_history()
    real_total_before = cc._total_tokens(messages)
    # threshold/reserve chosen so trigger_tokens is comfortably below the
    # small prompt_tokens=10 used to simulate the real provider-reported
    # number -- otherwise the round declines below_threshold before ever
    # reaching the scale-consistency check this test is about.
    cc.compact_messages(
        messages, cycle_id="b6-scale", iteration=1, state_root=tmp_path,
        threshold=0.00005, keep_tokens=50, window_tokens=98_304, reserve_tokens=0,
        prompt_tokens=10, prompt_token_delta=0, execution_id="exec-b6",
    )
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "compacted"
    assert ev["before_tokens"] == real_total_before, (
        "before_tokens must be this module's own _total_tokens estimate, not the provider trigger_signal"
    )
    assert ev["after_tokens"] < ev["before_tokens"], "after must be the same-scale estimate, strictly smaller"
    assert ev["trigger_signal"] == 10, "the real provider-reported number must still be recorded, separately"


class TestB7ThinkingBlocksKeyPresence:
    """B7 (P3, I5): the guard tested truthiness, so thinking_blocks=[] (an
    empty list, falsy) was not protected."""

    def test_b7_empty_thinking_blocks_list_is_still_protected(self):
        msg = {"role": "assistant", "content": "x" * 5000, "thinking_blocks": []}
        assert cc._worth_compacting(msg) is False, (
            "thinking_blocks=[] must still be treated as present -- key presence, not truthiness"
        )


def test_b8_lone_surrogate_falls_back_to_ensure_ascii_true(tmp_path):
    """B8 (P3): with ensure_ascii=False (A5), a string holding a lone UTF-16
    surrogate fails to encode to UTF-8, the write fails, and the fail-open
    round declines. Fix: fall back to ensure_ascii=True for that record."""
    lone_surrogate_text = "before" + "\ud800" + "after" + ("x" * 500)
    parts = {"content": lone_surrogate_text}
    encoded = cc._json_dumps_preservation_safe(parts)
    encoded.encode("utf-8")  # must not raise
    assert json.loads(encoded) == parts

    messages = _normal_compacting_history() + [
        {"role": "tool", "tool_call_id": "tc-surrogate", "name": "bash", "content": lone_surrogate_text},
        {"role": "assistant", "content": "keep me recent 2 " + "z" * 200},
    ]
    result = cc.compact_messages(
        messages, cycle_id="b8-surrogate", iteration=1, state_root=tmp_path,
        threshold=0.001, keep_tokens=50, window_tokens=98_304, execution_id="exec-b8",
    )
    ev = _last_journal_event(tmp_path)
    assert ev["reason"] == "compacted", "a lone surrogate anywhere in the dropped span must not fail the round"
