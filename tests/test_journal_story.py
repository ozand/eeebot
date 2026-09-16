from __future__ import annotations

import gzip
import json

import pytest

from scripts.journal_story import (
    LEDGER_SOURCE,
    StoryValidationError,
    assemble_story_artifact,
    build_story_prompt,
    load_day_journal,
    select_beats,
    validate_narration,
)


def _ledger_row(ts: str, cycle: str, outcome: str = "success") -> str:
    return json.dumps({"phase": "outcome", "cycle_id": cycle, "outcome": outcome, "task_title": cycle, "ts": ts})


def _write_ledger(state_dir, archive_lines: list[str], live_lines: list[str], *, archive_day: str = "2026-09-15"):
    ledger = state_dir / "ledger"
    ledger.mkdir(parents=True)
    with gzip.open(ledger / f"cycles-{archive_day}.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write("\n".join(archive_lines) + "\n")
    (ledger / "cycles.jsonl").write_text("\n".join(live_lines) + "\n", encoding="utf-8")
    return ledger


def test_day_spanning_rotation_boundary_counts_the_day_not_the_live_file(tmp_path):
    _write_ledger(
        tmp_path,
        [_ledger_row("2026-09-15T08:00:00Z", "cycle-a"), _ledger_row("2026-09-15T20:00:00Z", "cycle-b", "failed")],
        [_ledger_row("2026-09-15T23:59:40Z", "cycle-c"), _ledger_row("2026-09-16T00:10:00Z", "cycle-d")],
    )
    journal = load_day_journal(tmp_path, "2026-09-15")
    assert journal["source"] == LEDGER_SOURCE
    assert journal["status"] == "complete"
    assert journal["files_read"] == ["cycles-2026-09-15.jsonl.gz", "cycles.jsonl"]
    beats = select_beats(journal["rows"], day="2026-09-15")
    assert [beat["source"]["cycle_id"] for beat in beats] == ["cycle-a", "cycle-b", "cycle-c"]
    assert beats[0]["source"] == {"file": "cycles-2026-09-15.jsonl.gz", "line": 1, "cycle_id": "cycle-a", "phase": "outcome"}
    assert beats[2]["source"]["file"] == "cycles.jsonl"
    # The live file alone holds one row of this day; the day has three.
    assert sum(1 for row in journal["rows"] if row["_source_file"] == "cycles.jsonl") == 1


def test_partly_unreadable_day_is_marked_incomplete_not_thin(tmp_path):
    _write_ledger(
        tmp_path,
        [_ledger_row("2026-09-15T08:00:00Z", "cycle-a"), "{not json", _ledger_row("2026-09-15T09:00:00Z", "cycle-b")],
        [],
    )
    journal = load_day_journal(tmp_path, "2026-09-15")
    assert journal["status"] == "incomplete"
    assert journal["unreadable"] == [{"file": "cycles-2026-09-15.jsonl.gz", "line": 2, "reason": "malformed_json"}]
    assert len(select_beats(journal["rows"])) == 2
    thin = load_day_journal(tmp_path, "2026-09-13")
    assert thin["status"] == "complete"
    assert select_beats(thin["rows"]) == []


def test_corrupt_archive_is_reported_not_skipped_silently(tmp_path):
    ledger = _write_ledger(tmp_path, [_ledger_row("2026-09-15T08:00:00Z", "cycle-a")], [])
    (ledger / "cycles-2026-09-14.jsonl.gz").write_bytes(b"\x1f\x8b\x08\x00garbage")
    journal = load_day_journal(tmp_path, "2026-09-15")
    assert journal["status"] == "incomplete"
    assert journal["notes"] == ["unreadable_file:cycles-2026-09-14.jsonl.gz"]
    assert len(journal["rows"]) == 1
    assert load_day_journal(tmp_path / "nowhere", "2026-09-15")["notes"] == ["ledger_dir_missing"]


def _row(line: int, *, cycle: str = "cycle-a", outcome: str = "success", title: str = "Ship change"):
    return {
        "phase": "outcome", "cycle_id": cycle, "outcome": outcome,
        "task_title": title, "ts": "2026-09-15T12:00:00Z",
        "_source_file": "cycles.jsonl", "_source_line": line,
    }


def test_selection_is_deterministic_and_carries_sources_and_sign():
    rows = [_row(2, cycle="cycle-b", outcome="failed"), _row(1, cycle="cycle-a")]
    beats = select_beats(rows)
    assert [beat["beat_id"] for beat in beats] == ["beat-001", "beat-002"]
    assert [beat["sign"] for beat in beats] == ["worked", "failed"]
    assert beats[0]["source"] == {"file": "cycles.jsonl", "line": 1, "cycle_id": "cycle-a", "phase": "outcome"}


def test_retried_cycle_takes_sign_from_its_last_outcome_row_and_title_from_proposed():
    rows = [
        {"phase": "proposed", "cycle_id": "cycle-a", "task_title": "Add palette lookup", "ts": "2026-09-15T11:00:00Z",
         "_source_file": "cycles.jsonl", "_source_line": 1},
        {**_row(2, outcome="success", title=""), "ts": "2026-09-15T12:00:00Z"},
        {**_row(3, outcome="failed", title=""), "ts": "2026-09-15T12:30:00Z"},
    ]
    beats = select_beats(rows)
    assert len(beats) == 1
    assert beats[0]["sign"] == "failed"
    assert beats[0]["source"]["line"] == 3
    assert beats[0]["event"] == "Add palette lookup"


def test_one_event_and_quiet_day_do_not_get_padded():
    assert len(select_beats([_row(1)])) == 1
    assert select_beats([]) == []


def test_prompt_contains_only_selected_beats_and_fixed_sign_instruction():
    beats = select_beats([_row(1)])
    prompt = build_story_prompt(beats, {"ledger": "A journal record of what happened."})
    payload = json.loads(prompt)
    assert payload["beats"] == beats
    assert "preserve it exactly" in payload["sign_rule"]


def test_wrong_sign_is_rejected_even_when_prose_is_confident():
    beats = select_beats([_row(1, outcome="failed")])
    with pytest.raises(StoryValidationError, match="sign drift"):
        validate_narration([{"beat_id": "beat-001", "text": "I finally fixed it.", "sign": "worked", "terms": []}], beats)


def test_failed_beat_rejects_positive_prose_and_unknown_is_explicit():
    failed = select_beats([_row(1, outcome="failed")])
    with pytest.raises(StoryValidationError, match="positive wording"):
        validate_narration([{"beat_id": "beat-001", "text": "It succeeded.", "sign": "failed", "terms": []}], failed)
    unknown = select_beats([_row(1, outcome="partial")])
    with pytest.raises(StoryValidationError, match="unknown"):
        validate_narration([{"beat_id": "beat-001", "text": "I finally fixed it.", "sign": "unknown", "terms": []}], unknown)


def test_invented_beat_and_undefined_term_are_rejected():
    beats = select_beats([_row(1)])
    with pytest.raises(StoryValidationError, match="no selected beat"):
        validate_narration([{"beat_id": "beat-999", "text": "New event.", "sign": "worked", "terms": []}], beats)
    with pytest.raises(StoryValidationError, match="undefined glossary"):
        validate_narration([{"beat_id": "beat-001", "text": "It worked.", "sign": "worked", "terms": ["unknown"]}], beats, glossary={"ledger": "..."})


def test_artifact_carries_prompt_model_output_and_citation_set():
    beats = select_beats([_row(3)])
    artifact = assemble_story_artifact(
        beats,
        [{"beat_id": "beat-001", "text": "The change worked.", "sign": "worked", "terms": []}],
        prompt="prompt", model_output={"raw": "output"},
    )
    assert artifact["schema_version"] == "journal-story-v1"
    assert artifact["citation_set"]["beat-001"]["line"] == 3
