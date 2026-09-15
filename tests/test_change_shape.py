from __future__ import annotations

from datetime import datetime, timezone

from scripts.change_shape import classify_subject, distribution_from_rows


def test_classify_subjects_is_deterministic_and_model_free():
    assert classify_subject("feat: add a check") == "feature"
    assert classify_subject("fix: repair a check") == "maintenance"
    assert classify_subject("perf: optimize the loop") == "performance"
    assert classify_subject("docs: update the guide") == "documentation"
    assert classify_subject("test: add coverage") == "testing"
    assert classify_subject("chore: update lesson index") == "knowledge"
    assert classify_subject("knowledge: update hypothesis") == "knowledge"
    assert classify_subject("plain subject without a type") == "unclassified"


def test_distribution_from_rows_is_windowed_and_legacy_safe():
    rows = [
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "change_shape": "feature", "ts": "2026-09-14T10:00:00Z"},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "success", "change_shape": "documentation", "ts": "2026-09-14T11:00:00Z"},
        {"phase": "outcome", "cycle_id": "c3", "outcome": "success", "ts": "2026-09-14T12:00:00Z"},
        {"phase": "outcome", "cycle_id": "c4", "outcome": "failed", "change_shape": "feature", "ts": "2026-09-14T13:00:00Z"},
    ]
    result = distribution_from_rows(
        rows,
        now=datetime(2026, 9, 14, 14, tzinfo=timezone.utc),
        days=1,
    )
    assert result["integrated_cycles"] == 3
    assert result["distribution"]["feature"] == 1
    assert result["distribution"]["documentation"] == 1
    assert result["distribution"]["unclassified"] == 1
    assert sum(result["distribution"].values()) == result["integrated_cycles"]
