"""Tests for the ADR-021 rule 3 bounded retirement-candidate evidence.

This module combines skill_fitness.census and
lesson_v2.lesson_zero_citation_census, which each carry their own test
coverage — these tests exercise only the combiner's own behavior (bound,
bound_hit reporting, partial-availability handling, ordering), so the
underlying censuses are monkeypatched rather than reconstructed here.
"""
from __future__ import annotations

from pathlib import Path

from nanobot.runtime import retirement_candidates as rc


def _patch_censuses(monkeypatch, *, skill_result: dict, lesson_result: dict) -> None:
    monkeypatch.setattr(
        rc.skill_fitness, "census", lambda state_dir, repo, **kw: skill_result
    )
    monkeypatch.setattr(
        rc.lesson_v2, "lesson_zero_citation_census", lambda state_dir, **kw: lesson_result
    )


def test_combines_both_sources_under_the_bound(tmp_path: Path, monkeypatch) -> None:
    _patch_censuses(
        monkeypatch,
        skill_result={
            "ok": True,
            "skills_total": 2,
            "zero_read": [{"skill": "idle-skill", "reads_in_window": 0, "last_read": "2026-08-01T00:00:00Z"}],
        },
        lesson_result={
            "ok": True,
            "lessons_offered": 1,
            "zero_citation": [
                {"lesson_id": "LESS-A", "offered_in_window": 3, "last_offered": "2026-09-01T00:00:00Z", "last_cited": None}
            ],
        },
    )
    result = rc.retirement_candidates(tmp_path / "state", tmp_path / "repo", max_per_run=5)
    assert result["total_candidates"] == 2
    assert result["bound_hit"] is False
    assert result["skill_census_ok"] is True
    assert result["lesson_census_ok"] is True
    kinds = {c["kind"] for c in result["candidates"]}
    assert kinds == {"skill", "lesson"}


def test_bound_hit_reports_truncation_not_silence(tmp_path: Path, monkeypatch) -> None:
    zero_read = [
        {"skill": f"idle-{i}", "reads_in_window": 0, "last_read": f"2026-08-0{i}T00:00:00Z"}
        for i in range(1, 8)
    ]
    _patch_censuses(
        monkeypatch,
        skill_result={"ok": True, "skills_total": 7, "zero_read": zero_read},
        lesson_result={"ok": True, "lessons_offered": 0, "zero_citation": []},
    )
    result = rc.retirement_candidates(tmp_path / "state", tmp_path / "repo", max_per_run=3)
    assert result["total_candidates"] == 7
    assert result["bound_hit"] is True
    assert len(result["candidates"]) == 3


def test_never_active_candidate_sorts_before_a_dated_one(tmp_path: Path, monkeypatch) -> None:
    _patch_censuses(
        monkeypatch,
        skill_result={
            "ok": True,
            "skills_total": 2,
            "zero_read": [
                {"skill": "used-once-long-ago", "reads_in_window": 0, "last_read": "2026-01-01T00:00:00Z"},
                {"skill": "never-read", "reads_in_window": 0, "last_read": None},
            ],
        },
        lesson_result={"ok": True, "lessons_offered": 0, "zero_citation": []},
    )
    result = rc.retirement_candidates(tmp_path / "state", tmp_path / "repo", max_per_run=5)
    assert [c["id"] for c in result["candidates"]] == ["never-read", "used-once-long-ago"]


def test_one_side_unavailable_does_not_silence_the_other(tmp_path: Path, monkeypatch) -> None:
    _patch_censuses(
        monkeypatch,
        skill_result={"ok": False, "reason": "reads_unavailable", "skills_total": 0, "zero_read": []},
        lesson_result={
            "ok": True,
            "lessons_offered": 1,
            "zero_citation": [
                {"lesson_id": "LESS-A", "offered_in_window": 1, "last_offered": "2026-09-01T00:00:00Z", "last_cited": None}
            ],
        },
    )
    result = rc.retirement_candidates(tmp_path / "state", tmp_path / "repo")
    assert result["skill_census_ok"] is False
    assert result["skill_census_reason"] == "reads_unavailable"
    assert result["lesson_census_ok"] is True
    assert result["total_candidates"] == 1
    assert result["candidates"][0]["kind"] == "lesson"


def test_default_bound_matches_module_constant(tmp_path: Path, monkeypatch) -> None:
    _patch_censuses(
        monkeypatch,
        skill_result={"ok": True, "skills_total": 0, "zero_read": []},
        lesson_result={"ok": True, "lessons_offered": 0, "zero_citation": []},
    )
    result = rc.retirement_candidates(tmp_path / "state", tmp_path / "repo")
    assert result["max_per_run"] == rc.MAX_RETIREMENT_CANDIDATES_PER_RUN


def test_candidate_rows_carry_the_raw_numbers_a_citation_needs(tmp_path: Path, monkeypatch) -> None:
    """#1666 phase 3: retrieval_count/exposure must be real numbers on the
    row, not something a caller has to parse out of the free-text
    ``evidence`` string."""
    _patch_censuses(
        monkeypatch,
        skill_result={
            "ok": True, "skills_total": 1,
            "zero_read": [{"skill": "idle-skill", "reads_in_window": 0, "last_read": None}],
        },
        lesson_result={
            "ok": True, "lessons_offered": 1,
            "zero_citation": [
                {"lesson_id": "LESS-A", "offered_in_window": 12, "last_offered": "2026-09-01T00:00:00Z", "last_cited": None}
            ],
        },
    )
    result = rc.retirement_candidates(tmp_path / "state", tmp_path / "repo")
    by_kind = {c["kind"]: c for c in result["candidates"]}
    assert by_kind["skill"]["retrieval_count"] == 0
    assert by_kind["skill"]["exposure"] is None  # no per-skill offered/exposure count exists yet
    assert by_kind["lesson"]["retrieval_count"] == 0
    assert by_kind["lesson"]["exposure"] == 12


# ─── propose_retirement (#1666 phase 3, first checkbox) ─────────────────────


class TestProposeRetirement:
    def test_lesson_candidate_cites_offered_exposure(self) -> None:
        candidate = {
            "kind": "lesson", "id": "LESS-A", "evidence": "offered 12x, cited 0x in the observed window",
            "last_activity": None, "retrieval_count": 0, "exposure": 12,
        }
        proposal = rc.propose_retirement(candidate)
        assert proposal["operation"] == "retire"
        assert proposal["kind"] == "lesson"
        assert proposal["target_id"] == "LESS-A"
        citation = proposal["citation"]
        assert citation["kind"] == "lesson"
        assert citation["target_id"] == "LESS-A"
        assert citation["source"] == "retirement_candidates.lesson_census"
        assert citation["retrieval_count"] == 0
        assert citation["exposure"] == 12
        assert citation["offered_or_shown"] is True

    def test_skill_candidate_has_no_exposure_and_is_not_offered_or_shown(self) -> None:
        """#1666 phase 3: skills have no per-skill offered/exposure count
        instrumented yet -- the citation must say so honestly (exposure is
        None) rather than fabricate a passing denominator."""
        candidate = {
            "kind": "skill", "id": "idle-skill", "evidence": "zero confirmed reads in the observed window",
            "last_activity": None, "retrieval_count": 0, "exposure": None,
        }
        proposal = rc.propose_retirement(candidate)
        citation = proposal["citation"]
        assert citation["source"] == "retirement_candidates.skill_census"
        assert citation["retrieval_count"] == 0
        assert citation["exposure"] is None
        assert citation["offered_or_shown"] is False

    def test_retrieval_count_is_never_padded(self) -> None:
        """A retirement candidate's retrieval_count is 0 by construction
        (that's what makes it a candidate) -- propose_retirement must pass
        it through as the real 0, never floor it to 1."""
        candidate = {
            "kind": "lesson", "id": "LESS-Z", "evidence": "x", "last_activity": None,
            "retrieval_count": 0, "exposure": 15,
        }
        assert rc.propose_retirement(candidate)["citation"]["retrieval_count"] == 0
