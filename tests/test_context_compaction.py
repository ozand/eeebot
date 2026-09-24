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
import json
import math
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
    tool_content = _long_content(5_000)
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
