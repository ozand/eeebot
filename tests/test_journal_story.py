from __future__ import annotations

import json

import pytest

from scripts.journal_story import (
    StoryValidationError,
    assemble_story_artifact,
    build_story_prompt,
    select_beats,
    validate_narration,
)


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
