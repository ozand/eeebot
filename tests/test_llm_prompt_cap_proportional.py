"""#1319: the prompt recorder's cap removes about the overage, not a fixed 97% of the record."""
from __future__ import annotations

import json
import re

from nanobot.observability.llm_telemetry import MAX_LLM_PROMPT_PAYLOAD_BYTES, _cap_payload_record

_MARKER_RE = re.compile(r"…\[truncated \d+ chars\]|…\[truncated\]|…\[intermediate messages omitted\]|…\[payload truncated to fit 32KB budget\]")


def _bytes(record: dict) -> int:
    return len(json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"))


def _chars(value) -> int:
    """Sum of string-leaf lengths with marker text removed — the content a reader still has."""
    if isinstance(value, str):
        return len(_MARKER_RE.sub("", value))
    if isinstance(value, dict):
        return sum(_chars(v) for v in value.values())
    if isinstance(value, list):
        return sum(_chars(v) for v in value)
    return 0


def _record(messages: list[dict], content: str = "done") -> dict:
    return {
        "ts": "2026-09-05T12:00:00Z", "model": "m", "cycle_id": "cycle-0123456789ab", "component": "bridge", "seq": 7,
        "prompt_tokens": 30000, "completion_tokens": 100, "finish_reason": "stop",
        "messages": messages, "content": content, "reasoning_content": None,
    }


def test_record_well_over_the_cap_lands_near_the_cap_not_near_the_clamp():
    """Two 20K messages (~40 KB): the result must use the budget, not collapse to 2 × 1,000 chars."""
    record = _record([{"role": "user", "content": "u" * 20_000}, {"role": "assistant", "content": "a" * 20_000}])
    assert _bytes(record) > MAX_LLM_PROMPT_PAYLOAD_BYTES
    capped = _cap_payload_record(record)
    size = _bytes(capped)
    assert size <= MAX_LLM_PROMPT_PAYLOAD_BYTES
    assert size >= MAX_LLM_PROMPT_PAYLOAD_BYTES - 2_048, size  # within 2 KiB of the cap, not ~2 KB total
    assert capped["truncated"] is True
    assert capped["truncated_chars"] == _chars(record) - _chars(capped)


def test_record_one_byte_over_loses_about_one_byte():
    record = _record([{"role": "user", "content": "u" * 20_000}, {"role": "tool", "content": "t" * 5_000}])
    filler = MAX_LLM_PROMPT_PAYLOAD_BYTES + 1 - _bytes(record)
    record["messages"][0]["content"] += "x" * filler
    assert _bytes(record) == MAX_LLM_PROMPT_PAYLOAD_BYTES + 1
    capped = _cap_payload_record(record)
    assert _bytes(capped) <= MAX_LLM_PROMPT_PAYLOAD_BYTES
    # Loses the one byte plus the bookkeeping that records the loss: the `truncated` and
    # `truncated_chars` keys and one "…[truncated N chars]" marker, ~70 bytes together. Not 19,000.
    assert 1 <= capped["truncated_chars"] <= 96, capped["truncated_chars"]
    assert capped["messages"][1]["content"] == "t" * 5_000  # the shorter string is never touched


def test_overage_is_taken_from_the_longest_strings_and_short_ones_stay_whole():
    record = _record(
        [
            {"role": "system", "content": "s" * 30_000},
            {"role": "user", "content": "task " * 100},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})}}]},
            {"role": "tool", "content": "r" * 8_000},
        ],
        content="final answer " * 20,
    )
    capped = _cap_payload_record(record)
    assert _bytes(capped) <= MAX_LLM_PROMPT_PAYLOAD_BYTES
    assert capped["messages"][1] == record["messages"][1]
    assert capped["messages"][2] == record["messages"][2]
    assert capped["content"] == record["content"]
    assert len(capped["messages"][0]["content"]) < 30_000 and capped["messages"][0]["content"].endswith(" chars]")
    # the pool is levelled: the 8K tool result is only cut once the 30K system message is down to its size
    assert len(capped["messages"][3]["content"]) >= min(8_000, len(_MARKER_RE.sub("", capped["messages"][0]["content"])))
    assert capped["truncated_chars"] == _chars(record) - _chars(capped)
    # every message is still present -- the recorder's copy is the only durable one
    assert [m["role"] for m in capped["messages"]] == ["system", "user", "assistant", "tool"]


def test_multibyte_content_still_fits_and_still_uses_most_of_the_budget():
    record = _record([{"role": "user", "content": "ж" * 30_000}, {"role": "assistant", "content": "д" * 30_000}])
    capped = _cap_payload_record(record)
    size = _bytes(capped)
    assert size <= MAX_LLM_PROMPT_PAYLOAD_BYTES
    assert size >= MAX_LLM_PROMPT_PAYLOAD_BYTES // 2  # bytes-per-char 2 => at most 2x over-trim, never 97%


def test_structural_overhead_beyond_the_budget_falls_through_to_the_old_passes():
    """Thousands of tiny messages: no level can fit, so the later passes fire; the bound and the count still hold."""
    record = _record([{"role": "tool", "content": f"r{i:04d}"} for i in range(3_000)])
    capped = _cap_payload_record(record)
    assert _bytes(capped) <= MAX_LLM_PROMPT_PAYLOAD_BYTES
    assert capped["truncated"] is True
    assert capped["truncated_chars"] == _chars(record) - _chars(capped) > 0


def test_caller_messages_are_not_mutated():
    messages = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "write_file", "arguments": "x" * 60_000}}]}]
    before = json.dumps(messages)
    _cap_payload_record(_record(messages))
    assert json.dumps(messages) == before


def test_under_the_cap_is_returned_untouched():
    record = _record([{"role": "user", "content": "small"}])
    assert _cap_payload_record(record) is record
    assert "truncated_chars" not in record
