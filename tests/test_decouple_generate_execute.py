"""Issue #700: decouple generate->execute — a fresh materialized-improvement
hypothesis that is NOT already done in git and has NO live verify request
must reliably get a verify request written, independent of
``_derive_feedback_decision``'s mode/lane state.

5+ prior fixes (#656/#664/#690/#695/#697) tried to fix the feedback-decision
tangle itself and each eventually stalled again on the live host. This suite
models the DIRTY LIVE STATE that hid every prior bug: an accumulated pile of
stale queued verify requests that already resolved to a terminal result,
sitting alongside a genuinely fresh materialization with no live request.
Clean fixtures alone are not sufficient coverage for this issue.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime.cycle_feedback import IDLE_BACKSTOP_CYCLE_LIMIT, _derive_feedback_decision
from nanobot.runtime.cycle_planning import (
    _ensure_verify_request_for_fresh_materialization,
    _live_verify_request_for_artifact,
    _write_subagent_request_artifact,
)


def _write_materialized_artifact(improvements_dir: Path, *, name: str, title: str, feedback_decision: dict | None = None) -> Path:
    improvements_dir.mkdir(parents=True, exist_ok=True)
    path = improvements_dir / name
    path.write_text(json.dumps({
        "schema_version": "materialized-improvement-v1",
        "task_id": "materialize-synthesized-improvement",
        "next_bounded_candidate": {"title": title},
        "derived_candidate": {"title": title},
        "feedback_decision": feedback_decision,
    }), encoding="utf-8")
    return path


def _write_stale_request_with_terminal_result(
    state_root: Path, *, idx: int, source_artifact: str = "/nonexistent/stale-artifact.json"
) -> None:
    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    request_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    request_id = f"stale-req-{idx}"
    (request_dir / f"request-cycle-stale-{idx}.json").write_text(json.dumps({
        "schema_version": "subagent-request-v1",
        "task_id": "subagent-verify-materialized-improvement",
        "request_id": request_id,
        "request_status": "queued",
        "source_artifact": source_artifact,
    }), encoding="utf-8")
    (result_dir / f"result-{request_id}.json").write_text(json.dumps({
        "schema_version": "subagent-result-v1",
        "request_id": request_id,
        "result_status": "already_done",
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# The core invariant: dirty accumulated-stale-request state must not block a
# fresh verify request from being written for a genuinely fresh generation.
# ---------------------------------------------------------------------------

def test_fresh_materialization_gets_verify_request_despite_five_stale_terminal_requests(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    improvements_dir = state_root / "improvements"

    # The accumulated host mess: 5 stale requests, all request_status=queued,
    # each already carrying a terminal (already_done) result.
    for i in range(5):
        _write_stale_request_with_terminal_result(state_root, idx=i)

    # A fresh Vector-2-style hypothesis materialization with no live request.
    fresh_artifact = _write_materialized_artifact(
        improvements_dir,
        name="materialized-cycle-fresh700.json",
        title="Vector-2: reduce control-plane summary write latency",
    )

    result_path = _ensure_verify_request_for_fresh_materialization(
        state_root=state_root,
        cycle_id="cycle-fresh700",
        goal_id="goal-bootstrap",
    )

    assert result_path is not None
    written = json.loads(Path(result_path).read_text(encoding="utf-8"))
    assert written["task_id"] == "subagent-verify-materialized-improvement"
    assert written["source_artifact"] == str(fresh_artifact)
    assert written["request_status"] == "queued"

    # The 5 stale requests must remain untouched (ignored, not consumed).
    stale_requests = list((state_root / "subagents" / "requests").glob("request-cycle-stale-*.json"))
    assert len(stale_requests) == 5


def test_fresh_materialization_already_done_in_git_log_gets_no_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = tmp_path / "state"
    improvements_dir = state_root / "improvements"
    _write_materialized_artifact(
        improvements_dir,
        name="materialized-cycle-done700.json",
        title="Refresh the approval gate TTL handling",
    )

    import nanobot.runtime.cycle_planning as cycle_planning_mod

    monkeypatch.setattr(
        cycle_planning_mod,
        "_recent_git_log",
        lambda repo_root, since="14 days ago": "abcd123 fix: refresh the approval gate TTL handling (#701)",
    )
    monkeypatch.setattr(Path, "is_dir", lambda self: True)

    result_path = _ensure_verify_request_for_fresh_materialization(
        state_root=state_root,
        cycle_id="cycle-done700",
        goal_id="goal-bootstrap",
    )

    assert result_path is None
    assert not (state_root / "subagents" / "requests").exists()


def test_existing_live_request_for_the_same_artifact_is_not_duplicated(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    improvements_dir = state_root / "improvements"
    artifact_path = _write_materialized_artifact(
        improvements_dir,
        name="materialized-cycle-live700.json",
        title="Vector-3: dashboard latency instrumentation",
    )

    # A live (non-terminal-result) request already exists for this exact
    # artifact — written by the normal feedback-decision handoff earlier.
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    (request_dir / "request-cycle-already-live.json").write_text(json.dumps({
        "schema_version": "subagent-request-v1",
        "task_id": "subagent-verify-materialized-improvement",
        "request_id": "already-live-req",
        "request_status": "queued",
        "source_artifact": str(artifact_path),
    }), encoding="utf-8")

    result_path = _ensure_verify_request_for_fresh_materialization(
        state_root=state_root,
        cycle_id="cycle-live700-guard",
        goal_id="goal-bootstrap",
    )

    # No duplicate written — the guard's write helper returns the existing
    # live request's path instead of minting a new one.
    assert result_path == str(request_dir / "request-cycle-already-live.json")
    all_requests = list(request_dir.glob("*.json"))
    assert len(all_requests) == 1


def test_write_subagent_request_artifact_itself_dedups_against_a_live_request(tmp_path: Path) -> None:
    # Unit-level check of the shared liveness guard used by both the normal
    # handoff (_write_subagent_request_artifact) and the #700 decouple guard.
    state_root = tmp_path / "state"
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    (request_dir / "request-cycle-a.json").write_text(json.dumps({
        "task_id": "subagent-verify-materialized-improvement",
        "request_id": "req-a",
        "request_status": "queued",
        "source_artifact": "/some/artifact.json",
    }), encoding="utf-8")

    first = _live_verify_request_for_artifact(state_root=state_root, source_artifact_path="/some/artifact.json")
    assert first == request_dir / "request-cycle-a.json"

    path_b = _write_subagent_request_artifact(
        state_root=state_root,
        cycle_id="cycle-b",
        goal_id="goal-bootstrap",
        current_plan={
            "current_task_id": "subagent-verify-materialized-improvement",
            "tasks": [],
            "materialized_improvement_artifact_path": "/some/artifact.json",
        },
    )

    assert path_b == str(request_dir / "request-cycle-a.json")
    assert not (request_dir / "request-cycle-b.json").exists()


def test_stale_terminal_request_does_not_count_as_live(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _write_stale_request_with_terminal_result(state_root, idx=0, source_artifact="/some/artifact.json")

    assert _live_verify_request_for_artifact(state_root=state_root, source_artifact_path="/some/artifact.json") is None


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
