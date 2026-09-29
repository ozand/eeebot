"""Regression tests for Issue #1999 ADR-034 Completed parsing edge cases."""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.operator_documents import resolve_operator_priorities


def _resolve(tmp_path: Path, text: str):
    goals = tmp_path / "goals"
    goals.mkdir(parents=True, exist_ok=True)
    (goals / "goal_text.json").write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": "g", "text": text}),
        encoding="utf-8",
    )
    return resolve_operator_priorities(tmp_path)


def test_explicit_completed_entry_overrides_stale_open_number(tmp_path: Path):
    result = _resolve(
        tmp_path,
        "Current priority targets:\n(A) Priority 4 — Stale title: do work.\n"
        "Completed (do not repeat): Priority 4 — Finished title.",
    )
    assert [entry.number for entry in result.open_entries] == []
    assert [(entry.number, entry.title) for entry in result.completed_entries] == [
        (4, "Finished title")
    ]


def test_parenthesized_completed_title_preserves_periods(tmp_path: Path):
    result = _resolve(
        tmp_path,
        "Completed (do not repeat): Priority 4 (Fix parser. Preserve punctuation).",
    )
    assert [(entry.number, entry.title) for entry in result.completed_entries] == [
        (4, "Fix parser. Preserve punctuation")
    ]


def test_completed_parser_stops_at_any_following_section(tmp_path: Path):
    result = _resolve(
        tmp_path,
        "Completed (do not repeat): Priority 4 — Finished.\n"
        "## Notes\nPriority 9 — This is not completed.",
    )
    assert [(entry.number, entry.title) for entry in result.completed_entries] == [
        (4, "Finished")
    ]
