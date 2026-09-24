"""ADR-034 (docs/adr/ADR-034-two-operator-documents-one-root-each.md) rules 2
and 3: one resolver per operator document, and the operator priority list's
four rule-3 states. Issue #1937 lands the resolver half of the ADR-034 Test
Contract only — no reader is migrated here (that is A2, a separate PR).
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import demand
from nanobot.runtime.operator_documents import (
    DOCUMENT_SIZE_CAP_BYTES,
    PRIORITY_ALL_COMPLETED,
    PRIORITY_EMPTY,
    PRIORITY_PRESENT,
    PRIORITY_UNAVAILABLE,
    STATE_ABSENT,
    STATE_TEXT,
    resolve_charter,
    resolve_derived_priorities,
    resolve_operator_priorities,
)


def _goal_text_json(state_dir: Path, raw_text: str) -> None:
    goals_dir = state_dir / "goals"
    goals_dir.mkdir(parents=True, exist_ok=True)
    (goals_dir / "goal_text.json").write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": "g", "text": raw_text}),
        encoding="utf-8",
    )


def _mark_completed(state_dir: Path, num: str, title: str, instructions: str) -> None:
    """Write a synthetic completed-demand sidecar (#773) marking one priority
    entry done, the same id ``filter_completed_priorities_from_goal_text``
    derives from it — no git repo needed."""
    item = demand._make_item("priority", f"Priority {num} — {title}", instructions)
    demand_dir = state_dir / "demand"
    demand_dir.mkdir(parents=True, exist_ok=True)
    (demand_dir / "completed.json").write_text(
        json.dumps({"schema_version": "demand-completed-v1", "entries": {item["id"]: {"kind": "priority"}}}),
        encoding="utf-8",
    )


def test_each_document_resolves_from_its_one_root(tmp_path: Path):
    # Charter: <release root>/goals.md only.
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("the charter text", encoding="utf-8")

    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    (state_dir / "goals.md").write_text("decoy legacy charter, must never be read", encoding="utf-8")

    charter = resolve_charter(release_root)
    assert charter.state == STATE_TEXT
    assert charter.text == "the charter text"

    # A release root with no goals.md never falls back to the state dir's decoy.
    empty_release_root = tmp_path / "release-empty"
    empty_release_root.mkdir()
    assert resolve_charter(empty_release_root).state == STATE_ABSENT

    # Derived priorities: state/goals/derived_priorities.json only.
    (state_dir / "goals" / "derived_priorities.json").write_text(
        json.dumps({"schema_version": "derived-v1", "priorities": []}), encoding="utf-8"
    )
    (state_dir / "derived_priorities.json").write_text("decoy at the wrong root", encoding="utf-8")
    derived = resolve_derived_priorities(state_dir)
    assert derived.state == STATE_TEXT

    # Operator priorities: state/goals/goal_text.json only, ignoring the
    # legacy state_dir/goals.md decoy planted above.
    _goal_text_json(state_dir, "Current priority targets:\n(A) Priority 1 — Do a thing: write scripts/x.py.")
    priorities = resolve_operator_priorities(state_dir)
    assert priorities.state == PRIORITY_PRESENT


def test_four_priority_states_are_distinct(tmp_path: Path):
    state_present = tmp_path / "present"
    _goal_text_json(
        state_present,
        "Current priority targets:\n(A) Priority 1 — Do a thing: write scripts/x.py.",
    )
    result_present = resolve_operator_priorities(state_present)
    assert result_present.state == PRIORITY_PRESENT
    assert result_present.open_count == 1

    state_done = tmp_path / "done"
    instructions = "write scripts/x.py and commit."
    _goal_text_json(
        state_done,
        f"Current priority targets:\n(A) Priority 1 — Do a thing: {instructions}",
    )
    _mark_completed(state_done, "1", "Do a thing", instructions)
    result_done = resolve_operator_priorities(state_done)
    assert result_done.state == PRIORITY_ALL_COMPLETED
    assert result_done.completed_count == 1

    state_empty = tmp_path / "empty"
    _goal_text_json(state_empty, "just a description, no priorities listed.")
    result_empty = resolve_operator_priorities(state_empty)
    assert result_empty.state == PRIORITY_EMPTY

    state_missing = tmp_path / "missing"
    result_missing = resolve_operator_priorities(state_missing)
    assert result_missing.state == PRIORITY_UNAVAILABLE

    # All four are pairwise distinct — none collapses into another.
    assert {
        result_present.state,
        result_done.state,
        result_empty.state,
        result_missing.state,
    } == {PRIORITY_PRESENT, PRIORITY_ALL_COMPLETED, PRIORITY_EMPTY, PRIORITY_UNAVAILABLE}


def test_boundary_documents_leak_no_text(tmp_path: Path):
    sensitive = "the operator secretly wants a mars colony by friday"

    # Empty: the document has real content but no priority section at all —
    # still resolves without the content reaching the reason.
    state_empty = tmp_path / "empty"
    _goal_text_json(state_empty, sensitive)
    result_empty = resolve_operator_priorities(state_empty)
    assert result_empty.state == PRIORITY_EMPTY
    assert sensitive not in result_empty.reason

    # Corrupt: malformed JSON carrying the sensitive text.
    state_corrupt = tmp_path / "corrupt"
    goals_dir = state_corrupt / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "goal_text.json").write_text(
        '{"text": "' + sensitive + '" not valid json }}}', encoding="utf-8"
    )
    result_corrupt = resolve_operator_priorities(state_corrupt)
    assert result_corrupt.state == PRIORITY_UNAVAILABLE
    assert result_corrupt.reason == "malformed_json"
    assert sensitive not in result_corrupt.reason

    # Oversize: valid JSON, but over the cap — must not even be read.
    state_oversize = tmp_path / "oversize"
    goals_dir = state_oversize / "goals"
    goals_dir.mkdir(parents=True)
    oversized_text = sensitive + ("x" * (DOCUMENT_SIZE_CAP_BYTES + 1))
    (goals_dir / "goal_text.json").write_text(
        json.dumps({"text": oversized_text}), encoding="utf-8"
    )
    result_oversize = resolve_operator_priorities(state_oversize)
    assert result_oversize.state == PRIORITY_UNAVAILABLE
    assert result_oversize.reason == "oversize"
    assert sensitive not in result_oversize.reason

    # No boundary result exposes a raw-text field at all — the priority
    # resolver never carries the document's text, only its state.
    for result in (result_empty, result_corrupt, result_oversize):
        assert not hasattr(result, "text")
