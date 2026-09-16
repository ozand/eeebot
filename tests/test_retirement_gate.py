"""ADR-021 rule 3, first checkbox (#1666 phase 3): a proposal to retire
cites the non-use evidence it rests on.

``knowledge_curator.stage_retirement_proposal`` is the gate: it turns one
``retirement_candidates()`` row into a citation (via
``retirement_candidates.propose_retirement``), validates the citation
shape (the same ``trainer_evidence.validate_trainer_citation`` the add
path uses) and a named exposure floor, and records the outcome in
``decisions.jsonl``. It never deletes anything -- see
``tests/test_trainer_no_direct_mutation.py`` for the call-graph proof that
``propose_retirement`` cannot be reached any other way.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.knowledge_curator import MIN_RETIREMENT_EXPOSURE, stage_retirement_proposal


def _decisions(state_dir: Path) -> list[dict]:
    path = state_dir / "curator" / "decisions.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _lesson_candidate(exposure: int, *, lesson_id: str = "LESS-A") -> dict:
    return {
        "kind": "lesson", "id": lesson_id,
        "evidence": f"offered {exposure}x, cited 0x in the observed window",
        "last_activity": None, "retrieval_count": 0, "exposure": exposure,
    }


def _skill_candidate(*, skill_id: str = "idle-skill") -> dict:
    return {
        "kind": "skill", "id": skill_id, "evidence": "zero confirmed reads in the observed window",
        "last_activity": None, "retrieval_count": 0, "exposure": None,
    }


def test_evidence_row_with_real_exposure_is_accepted_and_recorded(tmp_path: Path) -> None:
    state = tmp_path / "state"
    candidate = _lesson_candidate(MIN_RETIREMENT_EXPOSURE + 2)

    result = stage_retirement_proposal(state, candidate)

    assert result["accepted"] is True
    decisions = _decisions(state)
    assert len(decisions) == 1
    row = decisions[0]
    assert row["lesson_id"] == "LESS-A"
    assert row["decision"] == "retirement_proposed"
    assert str(MIN_RETIREMENT_EXPOSURE + 2) in row["reason"]
    assert "retrieval_count=0" in row["reason"]


def test_exposure_exactly_at_the_floor_is_accepted(tmp_path: Path) -> None:
    result = stage_retirement_proposal(tmp_path / "state", _lesson_candidate(MIN_RETIREMENT_EXPOSURE))
    assert result["accepted"] is True


def test_below_floor_exposure_is_declined_and_recorded(tmp_path: Path) -> None:
    """#1672: only 4 of 59 zero-cited lessons were offered >= 10 times; an
    item offered 2 or 3 times is not evidence of anything about that
    specific lesson."""
    state = tmp_path / "state"
    candidate = _lesson_candidate(MIN_RETIREMENT_EXPOSURE - 1)

    result = stage_retirement_proposal(state, candidate)

    assert result["accepted"] is False
    assert "exposure_below_floor" in result["reason"]
    decisions = _decisions(state)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "retirement_declined"
    assert "exposure_below_floor" in decisions[0]["reason"]


def test_skill_candidate_is_declined_for_missing_exposure_not_a_fabricated_pass(tmp_path: Path) -> None:
    """A skill has no per-skill offered/exposure count instrumented yet;
    the gate must refuse rather than treat unmeasured exposure as
    passing (or as automatically failing the floor for the wrong
    reason) -- this fails citation validation itself, one step before
    the floor check even runs."""
    state = tmp_path / "state"
    result = stage_retirement_proposal(state, _skill_candidate())

    assert result["accepted"] is False
    assert "citation_invalid" in result["reason"]
    decisions = _decisions(state)
    assert decisions[0]["decision"] == "retirement_declined"
    assert decisions[0]["target_file"] == "skill"


def test_never_pads_retrieval_count(tmp_path: Path) -> None:
    """A retirement candidate's whole premise is retrieval_count == 0; the
    gate's recorded citation must show that real zero, not a padded 1."""
    candidate = _lesson_candidate(MIN_RETIREMENT_EXPOSURE + 5)
    state = tmp_path / "state"
    result = stage_retirement_proposal(state, candidate)
    assert result["accepted"] is True
    assert "retrieval_count=0" in result["reason"]


def test_no_deletion_or_repo_write_happens(tmp_path: Path) -> None:
    """Staging a retirement proposal must never touch lessons/ or skills/
    -- only decisions.jsonl under state_dir. No caller applies it yet."""
    state = tmp_path / "state"
    stage_retirement_proposal(state, _lesson_candidate(MIN_RETIREMENT_EXPOSURE + 1))
    # Nothing outside state/curator/ was created.
    created = sorted(p.relative_to(state) for p in state.rglob("*") if p.is_file())
    assert created == [Path("curator") / "decisions.jsonl"]
