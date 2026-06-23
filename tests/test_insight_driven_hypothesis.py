"""Tests for task #8: close the HADI Insight -> next-Hypothesis arc.

The synthesized improvement candidates must be derived from the freshest
accumulated reusable insight (when one exists) instead of a static template,
so an empty backlog is no longer a terminal stall state while insights exist.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from nanobot.runtime.coordinator import (
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
    _freshest_reusable_insight,
    _lesson_reward_value,
    _rank_insights_for_goal,
    _select_insight_for_goal,
    _synthesized_materialize_improvement_candidate,
    _synthesized_next_improvement_candidate,
)
from nanobot.runtime.lessons import LessonsDB

INSIGHT = "Short utility scripts are implementable in a single bounded bridge session."


# ── factory: materialize candidate ──────────────────────────────────────────

def test_materialize_candidate_reflects_insight():
    cand = _synthesized_materialize_improvement_candidate(
        current_task_id=SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
        strong_pass_count=3,
        goal_artifact_signature=None,
        insight=INSIGHT,
    )
    assert cand["task_id"] == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    # title + acceptance + hypothesis all carry the insight content
    assert "Short utility scripts" in cand["title"]
    assert "Short utility scripts" in cand["acceptance"]
    assert "Short utility scripts" in cand["hadi_cycle"]["hypothesis"]
    assert cand["derived_from_insight"].startswith("Short utility scripts")
    # contract fields preserved
    assert cand["kind"] == "execution"
    assert cand["hadi_required"] is True
    assert "task_readiness" in cand


def test_materialize_candidate_without_insight_is_backward_compatible():
    cand = _synthesized_materialize_improvement_candidate(
        current_task_id=SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
        strong_pass_count=3,
        goal_artifact_signature=None,
    )
    assert cand["title"] == "Materialize one bounded improvement from the synthesized candidate"
    assert "derived_from_insight" not in cand
    assert cand["selection_source"] == "feedback_synthesis_materialization"


# ── factory: synthesize-next candidate ──────────────────────────────────────

def test_next_candidate_reflects_insight():
    cand = _synthesized_next_improvement_candidate(
        current_task_id=None,
        strong_pass_count=3,
        goal_artifact_signature=None,
        insight=INSIGHT,
    )
    assert cand["task_id"] == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID
    assert "Short utility scripts" in cand["title"]
    assert "Short utility scripts" in cand["acceptance"]
    assert cand["derived_from_insight"].startswith("Short utility scripts")


def test_next_candidate_without_insight_is_backward_compatible():
    cand = _synthesized_next_improvement_candidate(
        current_task_id=None,
        strong_pass_count=3,
        goal_artifact_signature=None,
    )
    assert cand["title"] == "Synthesize one new bounded improvement candidate from retired lanes"
    assert "derived_from_insight" not in cand


def test_blank_insight_falls_back_to_template():
    cand = _synthesized_materialize_improvement_candidate(
        current_task_id=None,
        strong_pass_count=0,
        goal_artifact_signature=None,
        insight="   ",
    )
    assert cand["title"] == "Materialize one bounded improvement from the synthesized candidate"
    assert "derived_from_insight" not in cand


# ── helper: _freshest_reusable_insight ──────────────────────────────────────

def test_freshest_reusable_insight_returns_newest():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        db = LessonsDB(ws)
        db.record_lesson(
            task_id="task-older",
            title="Older",
            description="d",
            impact="i",
            approach="a",
            reusable_insight="Older insight that should be superseded.",
            files_changed=["scripts/a.py"],
            cycle_id="cycle-1",
        )
        db.record_lesson(
            task_id="task-newer",
            title="Newer",
            description="d",
            impact="i",
            approach="a",
            reusable_insight="Newer insight wins.",
            files_changed=["scripts/b.py"],
            cycle_id="cycle-2",
        )
        # record_lesson inserts newest at index 0 → newest insight is returned
        assert _freshest_reusable_insight(ws) == "Newer insight wins."


def test_freshest_reusable_insight_none_when_empty():
    with tempfile.TemporaryDirectory() as td:
        assert _freshest_reusable_insight(Path(td)) is None


# ── ranking: goal relevance + reward + recency (task #9) ────────────────────

def _record(db: LessonsDB, *, task_id: str, insight: str, impact: str, cycle_id: str):
    db.record_lesson(
        task_id=task_id,
        title=task_id,
        description="d",
        impact=impact,
        approach="a",
        reusable_insight=insight,
        files_changed=["scripts/x.py"],
        cycle_id=cycle_id,
    )


def test_lesson_reward_value_parses_embedded_reward():
    assert _lesson_reward_value({"impact": "Positive reward signal: 1.5"}) == 1.5
    assert _lesson_reward_value({"reusable_insight": "yields reward=0.8 here"}) == 0.8
    assert _lesson_reward_value({"impact": "no number"}) == 0.0


def test_rank_prefers_goal_relevant_over_newer_offgoal():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        db = LessonsDB(ws)
        # older but on-goal for goal "approval-truth-normalization"
        _record(
            db,
            task_id="older-onmark",
            insight="The approval truth normalization path fixes stale freshness.",
            impact="Positive reward signal: 1.0",
            cycle_id="c1",
        )
        # newer but off-goal
        _record(
            db,
            task_id="newer-offgoal",
            insight="Dashboard colour tweak improved nothing measurable.",
            impact="Positive reward signal: 1.0",
            cycle_id="c2",
        )
        top = _rank_insights_for_goal(ws, "approval-truth-normalization", top_n=1)
        assert top and "approval truth normalization" in top[0].lower()


def test_rank_reward_breaks_ties_for_equally_relevant():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        db = LessonsDB(ws)
        # both off-goal (relevance 0) → reward should decide, not recency
        _record(
            db,
            task_id="high-reward-older",
            insight="High reward improvement insight alpha.",
            impact="Positive reward signal: 2.0",
            cycle_id="c1",
        )
        _record(
            db,
            task_id="low-reward-newer",
            insight="Low reward improvement insight beta.",
            impact="Positive reward signal: 0.5",
            cycle_id="c2",
        )
        top = _rank_insights_for_goal(ws, "goal-bootstrap", top_n=1)
        assert top and "alpha" in top[0]


def test_select_insight_for_goal_none_when_empty():
    with tempfile.TemporaryDirectory() as td:
        assert _select_insight_for_goal(Path(td), "goal-bootstrap") is None
