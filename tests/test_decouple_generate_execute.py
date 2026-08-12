"""Issue #700 idle backstop: the force-restart counter must fire at the limit
even while the planner is replaying a retire/restart-mode recorded decision,
and must reset to zero on a real spawn.

(The generate->execute verify-request minting guard this suite also once
covered was deleted in #747 along with the deterministic planner's
request-minting lane; the LLM proposer (#707) is now the sole request source.)
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.cycle_feedback import IDLE_BACKSTOP_CYCLE_LIMIT, _derive_feedback_decision


# ---------------------------------------------------------------------------
# Backstop counter: force-restart must fire at the limit even while the
# planner is replaying a retire/restart-mode recorded decision — this is
# the early-return short-circuit that previously starved the backstop.
# ---------------------------------------------------------------------------

def test_backstop_force_restart_wins_over_a_replayed_retire_mode_decision(tmp_path: Path) -> None:
    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "history").mkdir()

    # The planner is stuck replaying the SAME retire-mode decision every
    # cycle (current_task_id never changes) — before the #700 fix this early
    # return happened BEFORE the idle-backstop check ever ran.
    recorded_feedback_decision = {
        "mode": "retire_stale_subagent_lane",
        "current_task_id": "record-reward",
        "selected_task_id": "subagent-verify-materialized-improvement",
    }
    task_plan = {
        "current_task_id": "record-reward",
        "feedback_decision": recorded_feedback_decision,
        "cycles_since_productive_spawn": IDLE_BACKSTOP_CYCLE_LIMIT + 1,
        "tasks": [{"task_id": "record-reward", "status": "active"}],
    }

    decision = _derive_feedback_decision(task_plan, goals_dir, state_root=tmp_path / "state")

    assert decision is not None
    assert decision["mode"] == "start_next_improvement_generation"
    assert decision["mode"] != recorded_feedback_decision["mode"]


def test_backstop_counter_increments_across_six_no_spawn_cycles_and_force_restarts(tmp_path: Path) -> None:
    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "history").mkdir()
    state_root = tmp_path / "state"

    fired_at = None
    for cycle in range(IDLE_BACKSTOP_CYCLE_LIMIT + 2):
        task_plan = {
            "current_task_id": "record-reward",
            "cycles_since_productive_spawn": cycle,
            "tasks": [{"task_id": "record-reward", "status": "active"}],
        }
        decision = _derive_feedback_decision(task_plan, goals_dir, state_root=state_root)
        if decision is not None and decision.get("mode") == "start_next_improvement_generation":
            fired_at = cycle
            break

    assert fired_at is not None
    assert fired_at == IDLE_BACKSTOP_CYCLE_LIMIT + 1


def test_backstop_counter_resets_to_zero_on_a_real_spawn(tmp_path: Path) -> None:
    from nanobot.runtime.cycle_planning import _build_task_plan_snapshot

    workspace = tmp_path / "workspace"
    state_root = workspace / "state"
    goals = state_root / "goals"
    goals.mkdir(parents=True)
    (goals / "current.json").write_text(json.dumps({"cycles_since_productive_spawn": 5}), encoding="utf-8")

    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    request_dir.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    (request_dir / "request-real-spawn.json").write_text(json.dumps({
        "task_id": "subagent-verify-materialized-improvement",
        "request_id": "req-real-spawn",
        "request_status": "queued",
    }), encoding="utf-8")
    (result_dir / "result-req-real-spawn.json").write_text(json.dumps({
        "request_id": "req-real-spawn",
        "result_status": "completed",
    }), encoding="utf-8")

    plan = _build_task_plan_snapshot(
        workspace=workspace,
        cycle_id="cycle-real-spawn",
        goal_id="goal-bootstrap",
        result_status="PASS",
        approval_gate_state="fresh",
        next_hint="continue",
        experiment={"reward_signal": {"value": 1.0}, "budget": {}, "budget_used": {}, "outcome": "keep"},
        report_path=tmp_path / "report.json",
        history_path=tmp_path / "history.json",
        improvement_score=1.0,
        feedback_decision=None,
        goals_dir=goals,
    )

    assert plan["cycles_since_productive_spawn"] == 0
