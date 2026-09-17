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

from nanobot.runtime.knowledge_curator import LESSONS_REL, _reflector_card, apply_staged_lesson_cards

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


def test_reflector_card_with_zero_cycles_is_declined_not_padded(tmp_path: Path) -> None:
    """_reflector_card must not fabricate a retrieval_count when cycles is
    empty (every real caller passes a non-empty cycles list, #1171's K=2
    recurrence or a single origin cycle -- this exercises the degenerate
    case directly). A genuine zero must reach validate_trainer_citation as
    a zero and be declined; padding it to 1 would be a citation the mint
    cannot back."""
    workspace = _workspace(tmp_path)
    state = tmp_path / "state"
    card = _reflector_card(
        card_id="LESS-REF-zero-0001",
        detail="Configure a fallback model group for the local model in LiteLLM so server crashes fail over automatically.",
        problem="LiteLLM server crashed without a fallback route",
        cycles=[], days=[], first_seen="2026-09-01", last_seen="2026-09-01",
        kind="approach_hint",
    )
    assert card is not None
    assert card["citation"]["retrieval_count"] == 0
    assert card["citation"]["offered_or_shown"] is False

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == []
    decisions = _decisions(state)
    assert any(
        d["lesson_id"] == "LESS-REF-zero-0001" and d["decision"] == "mint_declined"
        and "citation_invalid" in d["reason"]
        for d in decisions
    )


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


# --- ADR-021 phase 1's owed follow-up: cross-check the citation's claimed
# retrieval_count against state/lesson_usage/scans.jsonl -------------------

def _scans_row(lesson_ids: list[str], *, ts: str | None = None) -> dict:
    # Must be inside the 14-day retention window relative to the REAL
    # clock (read_citation_scans has no `now=` override in the gate's
    # call path) -- a fixed past date would eventually (and did, once)
    # fall outside retention and silently make the store read as
    # unavailable instead of present.
    if ts is None:
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"ts": ts, "cycle_id": "cycle-scan", "lesson_ids": lesson_ids}


def _write_scans(state: Path, rows: list[dict]) -> None:
    import json as _json

    lesson_usage = state / "lesson_usage"
    lesson_usage.mkdir(parents=True, exist_ok=True)
    (lesson_usage / "scans.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_citation_overstated_when_store_present_is_declined(tmp_path: Path) -> None:
    """scans.jsonl shows this lesson was actually cited once; the card
    claims 5 -- the gate must catch the overstatement rather than trust
    the claimed number at face value."""
    workspace = _workspace(tmp_path)
    state = tmp_path / "state"
    _write_scans(state, [_scans_row(["LESS-REF-cite-0001"])])
    citation = {**_VALID_CITATION, "retrieval_count": 5}
    card = _card("LESS-REF-cite-0001", citation=citation)

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == []
    decisions = _decisions(state)
    assert any(
        d["lesson_id"] == "LESS-REF-cite-0001" and d["decision"] == "mint_declined"
        and "citation_overstated" in d["reason"]
        for d in decisions
    )


def test_citation_within_store_count_is_accepted(tmp_path: Path) -> None:
    """The claimed count (1) is within (equal to) what scans.jsonl shows --
    a truthful claim is not penalized."""
    workspace = _workspace(tmp_path)
    state = tmp_path / "state"
    _write_scans(state, [_scans_row(["LESS-REF-cite-0001"])])
    citation = {**_VALID_CITATION, "retrieval_count": 1}
    card = _card("LESS-REF-cite-0001", citation=citation)

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == ["LESS-REF-cite-0001"]
    decisions = _decisions(state)
    assert not any("citation_overstated" in d.get("reason", "") for d in decisions)


def test_missing_scan_store_accepts_on_schema_alone_and_records_the_skip(tmp_path: Path) -> None:
    """No state/lesson_usage/ at all -- the cross-check cannot run, so it
    must never fail the citation closed. The card is accepted on citation
    schema alone, and the skip is recorded so the gap stays visible."""
    workspace = _workspace(tmp_path)
    state = tmp_path / "state"  # deliberately: no lesson_usage/ created
    citation = {**_VALID_CITATION, "retrieval_count": 999}
    card = _card("LESS-REF-cite-0001", citation=citation)

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == ["LESS-REF-cite-0001"]
    decisions = _decisions(state)
    assert any(
        d["lesson_id"] == "LESS-REF-cite-0001" and d["decision"] == "citation_crosscheck_skipped"
        for d in decisions
    )


def test_unreadable_scan_store_also_accepts_on_schema_alone(tmp_path: Path, monkeypatch) -> None:
    """A present-but-broken store (read_citation_scans raising or
    returning 'unavailable') must degrade the same way as an absent one --
    never a fail-closed decline on a store the gate cannot trust."""
    from nanobot.runtime import knowledge_curator as kc

    workspace = _workspace(tmp_path)
    state = tmp_path / "state"
    (state / "lesson_usage").mkdir(parents=True)  # dir exists, no readable content

    def _boom(*_a, **_k):
        raise RuntimeError("corrupt store")

    monkeypatch.setattr(kc, "read_citation_scans", _boom)
    citation = {**_VALID_CITATION, "retrieval_count": 999}
    card = _card("LESS-REF-cite-0001", citation=citation)

    applied = apply_staged_lesson_cards(workspace, {"cards": [card]}, state_dir=state)

    assert applied == ["LESS-REF-cite-0001"]
    decisions = _decisions(state)
    assert any(d["decision"] == "citation_crosscheck_skipped" for d in decisions)
