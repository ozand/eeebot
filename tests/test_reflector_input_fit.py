"""#1314: the reflector bounds its three prompt inputs structurally, never by slicing JSON text."""
from __future__ import annotations

import json
import re
from pathlib import Path

from nanobot.observability import llm_telemetry
from nanobot.runtime import reflector

_SECTION_RE = re.compile(
    r"\ATRANSCRIPT[^\n]*:\n(?P<transcript>.*)\nLEDGER[^\n]*:\n(?P<ledger>.*)\nPRIOR REFLECTIONS[^\n]*:\n(?P<journal>.*)\Z",
    re.DOTALL,
)


def _sections(messages: list[dict[str, str]]) -> dict[str, str]:
    """Split the user message into its three sections by label, tolerant of a note on the label line."""
    user = messages[1]["content"]
    header, _, body = user.partition("\n")
    assert header == "CYCLE_ID: c1"
    match = _SECTION_RE.match(body)
    assert match, body[:200]
    return match.groupdict()


def _header_lines(messages: list[dict[str, str]]) -> list[str]:
    return [line for line in messages[1]["content"].splitlines() if line.startswith(("TRANSCRIPT", "LEDGER", "PRIOR REFLECTIONS"))]


def _record(turns: int, chars: int, *, content: str = "done") -> dict:
    """A prompt record shaped like ``record_llm_prompt`` writes: system, task, then alternating turns."""
    messages: list[dict] = [
        {"role": "system", "content": "You are the executor."},
        {"role": "user", "content": "Task: fix the thing."},
    ]
    for index in range(turns):
        role = "assistant" if index % 2 == 0 else "tool"
        messages.append({"role": role, "content": f"turn-{index:03d} " + ("x" * chars)})
    return {"ts": "2026-08-26T00:00:00Z", "cycle_id": "c1", "seq": 3, "messages": messages, "content": content, "reasoning_content": None, "truncated": False}


def _ledger(rows: int, chars: int) -> list[dict]:
    body = [{"phase": "proposed", "cycle_id": "c1", "task_title": "Do work", "ts": "2026-08-26T00:00:00Z"}]
    for index in range(rows):
        body.append({"phase": f"step-{index:03d}", "cycle_id": "c1", "ts": f"2026-08-26T00:{index:02d}:00Z", "detail": "y" * chars})
    body.append({"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-08-26T01:00:00Z"})
    return body


def _journal(rows: int, chars: int) -> list[dict]:
    return [{"cycle_id": f"old-{index:03d}", "timestamp": f"2026-08-{index + 1:02d}T00:00:00Z", "summary": "z" * chars, "findings": [], "recommendations": []} for index in range(rows)]


def test_transcript_over_budget_is_bounded_by_whole_messages():
    record = _record(turns=60, chars=1_000)  # ~61K chars, well over the recorder-sized budget
    assert len(json.dumps(record, ensure_ascii=False)) > reflector._MAX_TRANSCRIPT_CHARS
    messages = reflector._messages("c1", record, _ledger(2, 10), [])
    section = _sections(messages)["transcript"]
    parsed = json.loads(section)  # the whole point: the section must be JSON
    assert len(section) <= reflector._MAX_TRANSCRIPT_CHARS
    kept = parsed["messages"]
    assert kept[0]["role"] == "system" and kept[1]["content"] == "Task: fix the thing."  # protected head
    assert kept[-1]["content"].startswith("turn-059")  # newest turn survives; oldest turns were dropped
    assert 2 < len(kept) < 62
    assert parsed["content"] == "done"
    header = _header_lines(messages)[0]
    assert header.startswith("TRANSCRIPT (") and "messages omitted" in header and header.endswith(":")


def test_transcript_fields_are_replaced_not_sliced_when_messages_alone_cannot_fit():
    record = _record(turns=2, chars=10, content="c" * 60_000)
    messages = reflector._messages("c1", record, _ledger(1, 10), [])
    parsed = json.loads(_sections(messages)["transcript"])
    assert parsed["content"] == reflector._OMITTED
    assert parsed["messages"], "small messages are kept once the oversized field is gone"
    assert "content omitted" in _header_lines(messages)[0]


def test_recorder_truncated_record_is_reported_as_lost_content():
    """A record the recorder already cut (#1039) fits and drops nothing here, but the prompt did lose content."""
    record = _record(turns=2, chars=10)
    record["messages"] = [{"role": "info", "content": "…[payload truncated to fit 32KB budget]"}]
    record["truncated"] = True
    messages, fit = reflector._build_prompt("c1", record, _ledger(1, 10), [])
    assert json.loads(_sections(messages)["transcript"]) == record
    assert fit["status"] == "truncated"
    assert fit["transcript"]["recorder_truncated"] is True and fit["transcript"]["dropped"] == 0
    assert "prompt recorder" in _header_lines(messages)[0]
    _, fit = reflector._build_prompt("c1", _record(turns=2, chars=10), _ledger(1, 10), [])
    assert fit["status"] == "complete" and "recorder_truncated" not in fit["transcript"]


def test_recorder_truncated_chars_are_carried_into_the_fit():
    """#1319: once the recorder says how much it removed, the reflector row and the label repeat the number."""
    record = _record(turns=2, chars=10)
    record["truncated"] = True
    record["truncated_chars"] = 123_456
    messages, fit = reflector._build_prompt("c1", record, _ledger(1, 10), [])
    assert fit["status"] == "truncated"
    assert fit["transcript"]["recorder_truncated"] is True
    assert fit["transcript"]["recorder_truncated_chars"] == 123_456
    assert "123456 chars removed" in _header_lines(messages)[0]


def test_ledger_over_budget_keeps_proposed_and_outcome_rows():
    ledger = _ledger(rows=40, chars=500)  # ~21K chars against 12K
    messages = reflector._messages("c1", _record(1, 10), ledger, [])
    parsed = json.loads(_sections(messages)["ledger"])
    assert parsed[0]["phase"] == "proposed"
    assert parsed[-1]["phase"] == "outcome"
    assert 2 < len(parsed) < len(ledger)
    dropped = [row["phase"] for row in ledger if row not in parsed]
    assert dropped == sorted(dropped) and dropped[0] == "step-000"  # oldest first
    assert "rows omitted" in _header_lines(messages)[1]


def test_journal_over_budget_keeps_newest_entries():
    journal = _journal(rows=10, chars=4_000)  # ~40K chars against 30K
    messages = reflector._messages("c1", _record(1, 10), _ledger(1, 10), journal)
    parsed = json.loads(_sections(messages)["journal"])
    assert parsed[-1]["cycle_id"] == "old-009"
    assert parsed[0]["cycle_id"] != "old-000"
    assert 0 < len(parsed) < 10
    assert "entries omitted" in _header_lines(messages)[2]


def test_inputs_under_budget_are_untouched_and_headers_are_bare():
    record, ledger, journal = _record(3, 50), _ledger(3, 50), _journal(3, 50)
    messages, fit = reflector._build_prompt("c1", record, ledger, journal)
    sections = _sections(messages)
    assert json.loads(sections["transcript"]) == record
    assert json.loads(sections["ledger"]) == ledger
    assert json.loads(sections["journal"]) == journal
    assert _header_lines(messages) == ["TRANSCRIPT:", "LEDGER:", "PRIOR REFLECTIONS:"]
    assert fit["status"] == "complete"
    assert all(fit[key]["dropped"] == 0 and fit[key]["chars"] <= fit[key]["budget"] for key in ("transcript", "ledger", "journal"))


def test_budgets_are_derived_from_measured_inputs():
    # The recorder already caps a prompt record at 32 KiB (#1039); anything above that is unreachable.
    assert reflector._MAX_TRANSCRIPT_CHARS == llm_telemetry.MAX_LLM_PROMPT_PAYLOAD_BYTES
    # Ten journal rows at the observed p95 row size (2,896 chars on 2026-09-05) must fit whole.
    assert reflector._MAX_JOURNAL_CHARS >= reflector._JOURNAL_TAIL * 2_900
    assert reflector._MAX_LEDGER_CHARS == 12_000


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _seed_oversized_ledger(state: Path) -> None:
    rows = [{"phase": "proposed", "cycle_id": "c1", "task_title": "Do work"}]
    rows += [{"phase": f"step-{index}", "cycle_id": "c1", "detail": "y" * 900} for index in range(20)]
    rows.append({"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-08-26T00:00:00Z"})
    _write(state / "ledger/cycles.jsonl", rows)
    _write(state / "llm_calls/prompts/2026-08-26.jsonl", [{"cycle_id": "c1", "seq": 1, "messages": [{"role": "user", "content": "task"}]}])


def _answer() -> str:
    return json.dumps({"cycle_id": "c1", "summary": "good", "findings": [], "recommendations": [], "followed_previous": []})


def test_truncation_is_journaled_on_the_success_row(tmp_path: Path):
    _seed_oversized_ledger(tmp_path)
    seen: list[list[dict[str, str]]] = []

    def llm(messages, model):
        seen.append(messages)
        return _answer()

    result = reflector.run_reflector(tmp_path, llm=llm)
    assert result["processed"] == 1 and result["input_truncated"] == 1
    json.loads(_sections(seen[0])["ledger"])
    row = json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])
    fit = row["input_fit"]
    assert fit["status"] == "truncated"
    # 23, not 22: run_reflector re-inserts the proposed row at index 0 on top of the cycle's own rows (pre-existing).
    assert fit["ledger"]["dropped"] >= 1 and fit["ledger"]["dropped_chars"] > 0 and fit["ledger"]["total"] == 23
    assert fit["transcript"]["dropped"] == 0 and fit["journal"]["dropped"] == 0


def test_truncation_is_journaled_on_the_error_row_too(tmp_path: Path):
    _seed_oversized_ledger(tmp_path)
    result = reflector.run_reflector(tmp_path, llm=lambda *_: "not json")
    assert result["errors"] == 1 and result["input_truncated"] == 1
    row = json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])
    assert row["status"] == "error"
    assert row["input_fit"]["status"] == "truncated" and row["input_fit"]["ledger"]["dropped"] >= 1


def test_complete_fit_is_recorded_so_absence_is_distinguishable(tmp_path: Path):
    _write(tmp_path / "ledger/cycles.jsonl", [{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-08-26T00:00:00Z"}])
    _write(tmp_path / "llm_calls/prompts/2026-08-26.jsonl", [{"cycle_id": "c1", "seq": 1, "messages": [{"role": "user", "content": "task"}]}])
    result = reflector.run_reflector(tmp_path, llm=lambda *_: _answer())
    assert result["processed"] == 1 and result["input_truncated"] == 0
    row = json.loads((tmp_path / "reflector/reflections.jsonl").read_text().splitlines()[0])
    assert row["input_fit"]["status"] == "complete"
    assert set(row["input_fit"]) == {"status", "transcript", "ledger", "journal"}
