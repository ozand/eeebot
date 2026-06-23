"""Task: the EXECUTED synthesize/materialize lane must be insight-derived.

#8/#9 enriched the generated_candidates feed, but the lane the subagent actually
runs comes from feedback_decision.selected_task (built in _derive_feedback_decision).
On the live host that lane stayed generic, so materialization was a no-op
(changed_files=None). This verifies the selected lane now carries the insight
(its title flows through selected_task_label -> _derive_bounded_tasks_from_plan).
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.coordinator import (
    MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID,
    SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
    _derive_feedback_decision,
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


def _ambition_history(goals_dir: Path, n: int = 5) -> None:
    history_dir = goals_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(n):
        # same streak key ("synthesized-reward-loop"), change-free, no subagents
        (history_dir / f"cycle-{idx}.json").write_text(
            json.dumps(
                {
                    "result_status": "PASS",
                    "goal_id": "goal-bootstrap",
                    "current_task_id": "record-reward",
                    "reward_signal": {"value": 0.8},
                }
            ),
            encoding="utf-8",
        )


def _task_plan() -> dict:
    return {
        "schema_version": "task-plan-v1",
        "current_task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID,
        "goal_id": "goal-bootstrap",
        "reward_signal": {"value": 0.8},
        "tasks": [
            {"task_id": SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID, "title": "Synthesize", "status": "active", "kind": "review"},
        ],
    }


def test_executed_materialize_lane_is_insight_derived_with_workspace(tmp_path: Path):
    workspace = tmp_path
    goals_dir = tmp_path / "state" / "goals"
    _ambition_history(goals_dir)
    _seed_lesson(workspace)

    decision = _derive_feedback_decision(_task_plan(), goals_dir, state_root=None, workspace=workspace)
    assert decision is not None
    assert decision["selected_task_id"] == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    # the insight content reaches the executed lane (title + label the subagent runs)
    assert "scripts/eeebot_dashboard.py" in decision["selected_task_title"]
    assert "scripts/eeebot_dashboard.py" in decision["selected_task_label"]


def test_executed_lane_stays_generic_without_workspace(tmp_path: Path):
    goals_dir = tmp_path / "state" / "goals"
    _ambition_history(goals_dir)
    # no workspace passed → no insight enrichment (backward-compatible)
    decision = _derive_feedback_decision(_task_plan(), goals_dir, state_root=None, workspace=None)
    assert decision is not None
    assert decision["selected_task_id"] == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    assert "insight:" not in (decision["selected_task_title"] or "").lower()
