"""The EXECUTED synthesize/materialize lane must be insight-derived (HADI I->H).

#8/#9 enriched the generated_candidates feed, but the lane the subagent actually
runs comes from feedback_decision.selected_task, which _derive_feedback_decision
builds from a generic template on many return paths. _enrich_decision_lane_with_insight
post-processes the returned decision in one place so the executed lane carries a
concrete insight (its title flows through selected_task_label ->
_derive_bounded_tasks_from_plan -> the subagent prompt).
"""
from __future__ import annotations

from pathlib import Path

from nanobot.runtime.coordinator import (
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
    _enrich_decision_lane_with_insight,
    _render_task_selection,
)
from nanobot.runtime.lessons import LessonsDB

INSIGHT = "Targeting scripts/eeebot_dashboard.py metrics reliably raises reward."


def _seed_lesson(workspace: Path) -> None:
    LessonsDB(workspace).record_lesson(
        task_id="dashboard-metrics",
        title="Dashboard metrics",
        description="d",
        impact="Positive reward signal: 1.4",
        approach="a",
        reusable_insight=INSIGHT,
        files_changed=["scripts/eeebot_dashboard.py"],
        cycle_id="cycle-x",
    )


def _generic_decision(task_id: str) -> dict:
    # mirrors an early-return decision dict from _derive_feedback_decision
    return {
        "mode": "synthesize_next_candidate",
        "current_task_id": "record-reward",
        "strong_pass_count": 3,
        "goal_artifact_signature": None,
        "selected_task_id": task_id,
        "selected_task_class": "review",
        "selected_task_title": "Synthesize one new bounded improvement candidate from retired lanes",
        "selected_task_label": "Synthesize one new bounded improvement candidate from retired lanes [task_id=%s]" % task_id,
    }


def test_synthesize_lane_becomes_insight_derived(tmp_path: Path):
    _seed_lesson(tmp_path)
    out = _enrich_decision_lane_with_insight(
        _generic_decision(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID), tmp_path, "goal-bootstrap"
    )
    assert "scripts/eeebot_dashboard.py" in out["selected_task_title"]
    assert "scripts/eeebot_dashboard.py" in out["selected_task_label"]
    assert out["selected_task_id"] == SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID


def test_materialize_lane_becomes_insight_derived(tmp_path: Path):
    _seed_lesson(tmp_path)
    dec = _generic_decision(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID)
    dec["selected_task_title"] = "Materialize one bounded improvement from the synthesized candidate"
    out = _enrich_decision_lane_with_insight(dec, tmp_path, "goal-bootstrap")
    assert "scripts/eeebot_dashboard.py" in out["selected_task_title"]
    assert out["selected_task_id"] == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID


def test_no_insight_when_no_lessons_is_noop(tmp_path: Path):
    dec = _generic_decision(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID)
    out = _enrich_decision_lane_with_insight(dec, tmp_path, "goal-bootstrap")
    assert out == dec  # unchanged — backward compatible


def test_non_synth_lane_untouched(tmp_path: Path):
    _seed_lesson(tmp_path)
    dec = _generic_decision("record-reward")
    out = _enrich_decision_lane_with_insight(dec, tmp_path, "goal-bootstrap")
    assert out == dec  # only synthesize/materialize lanes are enriched


def test_already_insight_derived_not_double_wrapped(tmp_path: Path):
    _seed_lesson(tmp_path)
    dec = _generic_decision(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID)
    dec["selected_task_title"] = "Synthesize a bounded improvement candidate from insight: foo"
    out = _enrich_decision_lane_with_insight(dec, tmp_path, "goal-bootstrap")
    assert out == dec  # already insight-derived → left as-is


def test_none_workspace_is_noop():
    dec = _generic_decision(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID)
    assert _enrich_decision_lane_with_insight(dec, None, "goal-bootstrap") == dec
    assert _render_task_selection  # symbol used above is importable


# ── goal-source fallback (autoresearch concrete target from todo.md) ─────────

TODO = """\
# todo
- [x] 0. done item
- [ ] 1. Approval truth normalization
  - Problem: approval file can be expired while dashboard still implies fresh.
  - Product changes:
    - recompute approval freshness from apply.ok
"""


def test_goal_source_used_when_insight_not_actionable(tmp_path: Path):
    # vague lesson (no file target) + a todo.md with a concrete open goal
    LessonsDB(tmp_path).record_lesson(
        task_id="meta", title="meta", description="d", impact="Positive reward signal: 1.2",
        approach="a", reusable_insight="Consolidate this optimization pattern in subsequent cycles.",
        files_changed=[], cycle_id="c1",
    )
    (tmp_path / "todo.md").write_text(TODO, encoding="utf-8")
    out = _enrich_decision_lane_with_insight(
        _generic_decision(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID), tmp_path, "goal-bootstrap"
    )
    assert "Approval truth normalization" in out["selected_task_title"]
    assert "insight:" in out["selected_task_title"].lower()


def test_actionable_insight_preferred_over_goal(tmp_path: Path):
    _seed_lesson(tmp_path)  # actionable: names scripts/eeebot_dashboard.py
    (tmp_path / "todo.md").write_text(TODO, encoding="utf-8")
    out = _enrich_decision_lane_with_insight(
        _generic_decision(MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID), tmp_path, "goal-bootstrap"
    )
    assert "scripts/eeebot_dashboard.py" in out["selected_task_title"]
    assert "Approval truth normalization" not in out["selected_task_title"]


def test_no_lessons_no_todo_is_noop(tmp_path: Path):
    out = _enrich_decision_lane_with_insight(
        _generic_decision(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID), tmp_path, "goal-bootstrap"
    )
    assert out["selected_task_title"] == _generic_decision(SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID)["selected_task_title"]
