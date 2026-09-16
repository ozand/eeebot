"""ADR-021 phase 2 (#1666): the trainer proposes, the gate holds.

Two rules land in ``apply_staged_lesson_cards`` — the sole write path from a
staged (trainer-proposed) lesson card into ``lessons/lessons.yaml`` (see
``nanobot.runtime.bridge._pickup_staged_promotions``, its only caller):

1. Rule 4 — a proposal without a valid citation is declined, never applied.
2. Rule 2's two-author conflict rule — an id collision with the checkout's
   existing (first-writer) content is declined and the decline is recorded,
   not silently absorbed as an ordinary no-op.

See ``tests/test_trainer_no_direct_mutation.py`` for the call-graph
assertion that this is the *only* path a trainer proposal can reach
``lessons/`` or ``skills/`` through.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from nanobot.runtime.knowledge_curator import LESSONS_REL, apply_staged_lesson_cards

_VALID_CITATION = {
    "kind": "lesson",
    "target_id": "LESS-REF-cite-0001",
    "source": "reflector.recurrence_v1",
    "retrieval_count": 2,
    "offered_or_shown": True,
}


def _workspace(tmp_path: Path, existing: list[dict] | None = None) -> Path:
    workspace = tmp_path / "workspace"
    target = workspace / LESSONS_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump({"lessons": existing or []}, sort_keys=False), encoding="utf-8")
    return workspace


def _decisions(state_dir: Path) -> list[dict]:
    path = state_dir / "curator" / "decisions.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _card(card_id: str, *, citation: dict | None) -> dict:
    card = {
        "schema_version": 2, "id": card_id, "title": "t", "problem": "p", "solution": "s",
        "tags": ["runtime"], "source": "reflector", "severity": "medium", "seen_count": 2,
        "first_seen": "2026-09-01", "last_seen": "2026-09-02", "evidence": ["cycle-a", "cycle-b"],
    }
    if citation is not None:
        card["citation"] = citation
    return card


# --- rule 4: citation required -------------------------------------------

def test_card_without_citation_is_declined(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    state = tmp_path / "state"
    card = _card("LESS-REF-cite-0001", citation=None)

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == []
    lessons = yaml.safe_load((workspace / LESSONS_REL).read_text(encoding="utf-8"))["lessons"]
    assert lessons == []
    decisions = _decisions(state)
    assert any(
        d["lesson_id"] == "LESS-REF-cite-0001" and d["decision"] == "mint_declined"
        and "citation_invalid" in d["reason"]
        for d in decisions
    )


def test_card_with_malformed_citation_is_declined(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    state = tmp_path / "state"
    card = _card("LESS-REF-cite-0002", citation={"kind": "lesson"})  # missing required fields

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == []
    decisions = _decisions(state)
    assert any(d["decision"] == "mint_declined" and "citation_invalid" in d["reason"] for d in decisions)


def test_card_with_valid_citation_is_applied(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    state = tmp_path / "state"
    card = _card("LESS-REF-cite-0001", citation=_VALID_CITATION)

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == ["LESS-REF-cite-0001"]
    lessons = yaml.safe_load((workspace / LESSONS_REL).read_text(encoding="utf-8"))["lessons"]
    assert [entry["id"] for entry in lessons] == ["LESS-REF-cite-0001"]


# --- rule 2: two-author conflict on an id collision ------------------------

def test_id_collision_with_differing_content_is_declined_and_recorded(tmp_path: Path) -> None:
    """The checkout already carries this id with different content (e.g. the
    executor authored it in its own cycle). The trainer's proposal for the
    same id is declined -- the existing (first) writer keeps authorship --
    and, unlike an ordinary idempotent-retry no-op, the decline is recorded."""
    existing_entry = {
        "id": "LESS-REF-cite-0001", "problem": "executor's own problem text",
        "solution": "executor's own solution text", "seen_count": 1,
        "last_seen": "2026-08-01",
    }
    workspace = _workspace(tmp_path, existing=[existing_entry])
    state = tmp_path / "state"
    card = _card("LESS-REF-cite-0001", citation=_VALID_CITATION)  # same id, different problem/solution

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == []
    lessons = yaml.safe_load((workspace / LESSONS_REL).read_text(encoding="utf-8"))["lessons"]
    assert lessons == [existing_entry]  # untouched
    decisions = _decisions(state)
    assert any(
        d["lesson_id"] == "LESS-REF-cite-0001" and d["decision"] == "mint_declined"
        and "id_conflict" in d["reason"]
        for d in decisions
    )


def test_id_collision_with_identical_content_is_a_quiet_retry(tmp_path: Path) -> None:
    """A retried pickup re-proposing a card already applied verbatim is the
    ordinary idempotent no-op (#1209) -- not a conflict -- and is not
    recorded as one."""
    existing_entry = {
        "id": "LESS-REF-cite-0001", "problem": "p", "solution": "s",
        "seen_count": 2, "last_seen": "2026-09-02",
    }
    workspace = _workspace(tmp_path, existing=[existing_entry])
    state = tmp_path / "state"
    card = _card("LESS-REF-cite-0001", citation=_VALID_CITATION)  # same id, same problem/solution

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == []
    decisions = _decisions(state)
    assert not any("id_conflict" in d.get("reason", "") for d in decisions)
