"""Regression tests for issue #580's third (final) fix.

After the orphan-task fixes (f491fa1, dd6efb8), the coordinator on host eeepc
was observed live to bounce forever between
`subagent-verify-materialized-improvement` and `refresh-approval-gate` (a CORE
bookkeeping task) inside `_derive_feedback_decision`'s
`retire_goal_artifact_pair` branch (nanobot/runtime/coordinator.py).

The branch has five cascading tiers once a goal/artifact PASS streak reaches
the retirement threshold:

1. materialize-pass-streak-improvement selectable -> promote_review_followup
2. synthesize-next-improvement-candidate selectable -> feedback_pass_streak_switch
3. any other selectable task EXCLUDING CORE_TASK_IDS + current
4. (removed) any other selectable task INCLUDING core tasks + current
5. arc-completion fallback: record-reward or a fresh synthesize candidate

Tier 4 used to fire whenever any CORE bookkeeping task (which runs every
cycle anyway, so is essentially always "selectable") was available, which
pre-empted tier 5's designed arc-completion flow and produced the observed
bounce. Tier 4 has been deleted; tier 5 now handles every "nothing bounded
left to select" case.
"""
import json
from pathlib import Path

from nanobot.runtime.coordinator import _derive_feedback_decision


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_strong_pass_history(history_dir: Path, current_task_id: str, count: int = 3) -> None:
    for idx in range(count):
        _write_json(
            history_dir / f"cycle-{idx}.json",
            {
                "schema_version": "task-history-v1",
                "result_status": "PASS",
                "goal_id": "goal-bootstrap",
                "current_task_id": current_task_id,
                "artifact_paths": ["state/improvements/materialized-pass-streak.json"],
            },
        )


def _live_bounce_task_plan() -> dict:
    return {
        "current_task_id": "subagent-verify-materialized-improvement",
        "reward_signal": {"value": 1.2},
        "tasks": [
            {"task_id": "synthesize-next-improvement-candidate", "title": "Synthesize", "status": "done"},
            {"task_id": "materialize-synthesized-improvement", "title": "Materialize synthesized", "status": "done"},
            {"task_id": "inspect-pass-streak", "title": "Inspect repeated PASS streak", "status": "done"},
            {"task_id": "materialize-pass-streak-improvement", "title": "Materialize improvement", "status": "done"},
            {
                "task_id": "subagent-verify-materialized-improvement",
                "title": "Verify materialized improvement",
                "status": "active",
            },
            {"task_id": "exploit-successful-improvement-path", "title": "Orphan", "status": "pending"},
            {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "pending"},
            {"task_id": "verify-approval-gate", "title": "Verify approval gate", "status": "pending"},
            {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "pending"},
            {"task_id": "record-reward", "title": "Record cycle reward", "status": "pending"},
        ],
    }


def test_arc_complete_bounce_restarts_via_record_reward_not_a_core_task(tmp_path: Path) -> None:
    """Live bounce replay: from subagent-verify with everything else in the

    synthesize->materialize->verify arc already done, the coordinator must
    stop bouncing onto a CORE bookkeeping task (e.g. refresh-approval-gate)
    and instead run the designed arc-completion flow: record the reward so
    the next cycle can synthesize a fresh candidate.
    """
    goals_dir = tmp_path / "state" / "goals"
    history_dir = goals_dir / "history"
    experiments_dir = tmp_path / "state" / "experiments"
    history_dir.mkdir(parents=True)
    experiments_dir.mkdir(parents=True)

    _write_strong_pass_history(history_dir, "subagent-verify-materialized-improvement")
    _write_json(
        experiments_dir / "latest.json",
        {
            "outcome": "keep",
            "current_task_id": "subagent-verify-materialized-improvement",
            "reward_signal": {"value": 1.2},
        },
    )

    task_plan = _live_bounce_task_plan()

    decision = _derive_feedback_decision(task_plan, goals_dir)

    assert decision is not None
    assert decision["mode"] == "record_reward_after_synthesized_materialization"
    assert decision["selected_task_id"] == "record-reward"
    assert decision["selected_task_id"] not in {
        "refresh-approval-gate",
        "verify-approval-gate",
        "run-bounded-turn",
    }
    assert decision["selected_task_id"] != "exploit-successful-improvement-path"
    assert decision["selected_task_id"] != "subagent-verify-materialized-improvement"


def test_non_arc_complete_lane_still_selects_pending_synthesize_candidate(tmp_path: Path) -> None:
    """Safety check: tiers 1-3 are untouched by the tier-4 removal. When the

    synthesize-next-improvement-candidate task is still pending (the arc is
    NOT yet complete), it must remain selectable and win via tier 2
    (`feedback_pass_streak_switch`), never falling through to the
    arc-completion tier.
    """
    goals_dir = tmp_path / "state" / "goals"
    history_dir = goals_dir / "history"
    experiments_dir = tmp_path / "state" / "experiments"
    history_dir.mkdir(parents=True)
    experiments_dir.mkdir(parents=True)

    _write_strong_pass_history(history_dir, "subagent-verify-materialized-improvement")
    _write_json(
        experiments_dir / "latest.json",
        {
            "outcome": "keep",
            "current_task_id": "subagent-verify-materialized-improvement",
            "reward_signal": {"value": 1.2},
        },
    )

    task_plan = _live_bounce_task_plan()
    for task in task_plan["tasks"]:
        if task["task_id"] == "synthesize-next-improvement-candidate":
            task["status"] = "pending"

    decision = _derive_feedback_decision(task_plan, goals_dir)

    assert decision is not None
    assert decision["mode"] == "retire_goal_artifact_pair"
    assert decision["selected_task_id"] == "synthesize-next-improvement-candidate"
    assert decision["selection_source"] == "feedback_pass_streak_switch"
