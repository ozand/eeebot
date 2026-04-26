import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.runtime.state import _material_progress_snapshot, _subagent_rollup_snapshot, format_runtime_state, load_runtime_state, load_runtime_state_from_root, resolve_runtime_state_location
from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path):
    from pathlib import Path

    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_cycle_writes_block_report_when_gate_missing(tmp_path):
    execute = AsyncMock(return_value="should not run")
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)

    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=now,
        )
    )

    execute.assert_not_awaited()
    assert "BLOCK" in summary

    runtime = load_runtime_state(tmp_path)
    assert runtime["active_goal"] == "goal-bootstrap"
    assert runtime["approval_gate_state"] == "missing"
    assert runtime["next_hint"] == "approval gate missing; refresh manually"
    assert runtime["cycle_id"].startswith("cycle-")
    assert runtime["evidence_ref"].startswith("evidence-")
    assert runtime["current_task_id"] == "refresh-approval-gate"
    assert runtime["task_reward_value"] == 0.0
    assert runtime["experiment"]["schema_version"] == "experiment-v1"
    assert runtime["experiment"]["outcome"] == "blocked"
    assert runtime["experiment"]["metric_name"] == "reward_signal.value"
    assert runtime["experiment"]["metric_current"] == 0.0
    assert runtime["experiment"]["metric_baseline"] is None
    assert runtime["experiment"]["metric_frontier"] == 0.0
    assert runtime["experiment"]["complexity_delta"] == 0
    assert runtime["experiment"]["simplicity_judgment"] == "simple"
    assert runtime["experiment"]["contract_path"].endswith('.json')
    assert runtime["experiment_budget"]["max_requests"] == 2
    assert runtime["experiment_budget_used"]["requests"] == 0
    assert runtime["experiment_reward_signal"]["value"] == 0.0
    assert runtime["task_plan"]["schema_version"] == "task-plan-v1"
    assert runtime["task_history"]["schema_version"] == "task-history-v1"
    assert runtime["hypothesis_backlog_schema_version"] == "hypothesis-backlog-v1"
    assert runtime["hypothesis_backlog_selected_id"] == "refresh-approval-gate"
    assert runtime["hypothesis_backlog_entry_count"] == 2
    assert runtime["credits_balance"] == 0.0
    assert runtime["credits_delta"] == 0.0

    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "BLOCK"
    assert report["goal_id"] == "goal-bootstrap"
    assert report["current_task_id"] == "refresh-approval-gate"
    assert report["reward_signal"]["value"] == 0.0
    assert report["approval_gate"]["state"] == "missing"
    assert report["summary"] == summary
    assert report["experiment"]["schema_version"] == "experiment-v1"
    assert report["experiment"]["outcome"] == "blocked"
    assert report["experiment"]["metric_frontier"] == 0.0
    assert report["budget_used"]["requests"] == 0
    assert (tmp_path / "state" / "experiments" / f"{report['experiment']['experiment_id']}.json").exists()
    contract = _read_json(tmp_path / "state" / "experiments" / "contracts" / f"{report['experiment']['experiment_id']}.json")
    assert contract["contract_type"] == "bounded-hourly-self-improvement"
    assert contract["success_metric"] == "reward_signal.value"
    assert contract["hypothesis"].startswith("If task `refresh-approval-gate`")
    assert contract["success_checks"]
    assert contract["run_budget"]["max_requests"] == 2
    assert contract["run_budget"]["max_tool_calls"] == 12

    outbox = _read_json(tmp_path / "state" / "outbox" / "latest.json")
    assert outbox["approval_gate"]["state"] == "missing"
    assert outbox["next_hint"] == "approval gate missing; refresh manually"
    assert outbox["latest_report"]["result_status"] == "BLOCK"

    report_index = _read_json(tmp_path / "state" / "outbox" / "report.index.json")
    assert report_index["status"] == "BLOCK"
    assert report_index["source"] == runtime["report_path"]
    assert report_index["goal"]["goal_id"] == "goal-bootstrap"
    assert report_index["goal"]["text"] == "goal-bootstrap"
    follow_through = report_index["goal"]["follow_through"]
    assert follow_through["status"] == "blocked_next_action"
    assert follow_through["blocked_next_step"] == "approval gate missing; refresh manually"
    assert follow_through["artifact_paths"] == []
    assert follow_through["file_action"] == {
        "kind": "file_write",
        "path": "state/approvals/apply.ok",
        "summary": "Write a fresh approval gate with a valid TTL",
    }
    assert follow_through["verification_command"] == "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"
    assert report_index["goal_context"]["subagent_rollup"]["enabled"] is False
    assert report_index["improvement_score"] == 0.0
    assert report_index["capability_gate"]["approval"]["state"] == "missing"

    goal = _read_json(tmp_path / "state" / "goals" / "active.json")
    assert goal["active_goal"] == "goal-bootstrap"

    current = _read_json(tmp_path / "state" / "goals" / "current.json")
    assert current["schema_version"] == "task-plan-v1"
    assert current["current_task_id"] == "refresh-approval-gate"
    assert current["task_counts"] == {"total": 2, "done": 0, "active": 1, "pending": 1}
    assert current["blocked_next_step"] == "approval gate missing; refresh manually"
    assert current["file_action"] == {
        "kind": "file_write",
        "path": "state/approvals/apply.ok",
        "summary": "Write a fresh approval gate with a valid TTL",
    }
    assert current["verification_command"] == "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"
    assert current["reward_signal"]["value"] == 0.0
    backlog = _read_json(tmp_path / "state" / "hypotheses" / "backlog.json")
    assert not any(item.get("task_id") == "inspect-pass-streak" for item in backlog["entries"])
    research_feed = _read_json(tmp_path / "state" / "research" / "feed.json")
    assert research_feed["entry_count"] == 0
    assert backlog["schema_version"] == "hypothesis-backlog-v1"
    assert backlog["goal_id"] == "goal-bootstrap"
    assert backlog["selected_hypothesis_id"] == "refresh-approval-gate"
    assert backlog["entry_count"] == 2
    assert backlog["entries"][0]["selected"] is True
    assert backlog["entries"][0]["selection_status"] == "selected"
    assert backlog["entries"][0]["bounded_priority_score"] >= backlog["entries"][1]["bounded_priority_score"]
    assert backlog["entries"][0]["wsjf"]["score"] >= backlog["entries"][1]["wsjf"]["score"]
    assert backlog["entries"][0]["wsjf"]["job_size"] >= 1
    assert backlog["entries"][0]["execution_spec"]["goal"] == "goal-bootstrap"
    assert backlog["entries"][0]["execution_spec"]["task_title"] == "Write a fresh approval gate with a valid TTL"
    assert backlog["entries"][0]["execution_spec"]["budget"]["max_requests"] == 2
    assert backlog["entries"][0]["execution_spec"]["acceptance"] == "Write a fresh approval gate with a valid TTL at state/approvals/apply.ok"
    assert backlog["entries"][0]["hadi"]["hypothesis"] == "Write a fresh approval gate with a valid TTL"
    assert backlog["entries"][0]["hadi"]["action"] == "Write a fresh approval gate with a valid TTL at state/approvals/apply.ok"
    assert backlog["entries"][0]["hadi"]["data"]["result_status"] == "BLOCK"
    assert backlog["entries"][0]["hadi"]["insights"]
    assert backlog["entries"][1]["selected"] is False
    assert backlog["entries"][1]["selection_status"] == "backlog"
    assert backlog["entries"][1]["execution_spec"]["budget"]["max_tool_calls"] == 12
    credits = _read_json(tmp_path / "state" / "credits" / "latest.json")
    assert credits["schema_version"] == "credits-ledger-v1"
    assert credits["balance"] == 0.0
    assert credits["delta"] == 0.0
    assert credits["cycle_id"] == runtime["cycle_id"]
    history_line = (tmp_path / "state" / "credits" / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    assert json.loads(history_line)["cycle_id"] == runtime["cycle_id"]
    history = _read_json(tmp_path / "state" / "goals" / "history" / f"cycle-{runtime['cycle_id']}.json")
    assert history["schema_version"] == "task-history-v1"
    assert history["report_index_path"].endswith("report.index.json")


def test_cycle_writes_pass_report_when_gate_is_fresh(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "active.json").write_text(
        json.dumps({"active_goal": "goal-123"}),
        encoding="utf-8",
    )

    execute = AsyncMock(return_value="agent completed bounded work")
    now = expires_at - timedelta(minutes=30)

    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=now,
        )
    )

    execute.assert_awaited_once_with("check open tasks")
    assert "PASS" in summary

    runtime = load_runtime_state(tmp_path)
    assert runtime["active_goal"] == "goal-123"
    assert runtime["approval_gate_state"] == "fresh"
    assert runtime["approval_gate_ttl_minutes"] == 60
    assert runtime["next_hint"] == "none"
    assert runtime["current_task_id"] == "record-reward"
    assert runtime["task_reward_value"] == 1.0
    assert runtime["experiment"]["schema_version"] == "experiment-v1"
    assert runtime["experiment"]["outcome"] == "keep"
    assert runtime["experiment"]["metric_name"] == "reward_signal.value"
    assert runtime["experiment"]["metric_current"] == 1.0
    assert runtime["experiment"]["metric_frontier"] == 1.0
    assert runtime["experiment"]["complexity_delta"] == 1
    assert runtime["experiment"]["simplicity_judgment"] == "moderate"
    assert runtime["experiment_budget_used"]["requests"] == 1
    assert runtime["task_plan"]["schema_version"] == "task-plan-v1"
    assert runtime["task_history"]["schema_version"] == "task-history-v1"

    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "PASS"
    assert report["goal_id"] == "goal-123"
    assert report["current_task_id"] == "record-reward"
    assert report["reward_signal"]["value"] == 1.0
    assert report["approval_gate"]["state"] == "fresh"
    assert report["execution_response"] == "agent completed bounded work"
    assert report["bounded_apply"] == "on"
    assert report["promotion_execute"] == "on"
    assert report["promotion_candidate_id"].startswith("promotion-")
    assert report["review_status"] == "pending_policy_review"
    assert report["decision"] == "pending_policy_review"
    assert report["experiment"]["schema_version"] == "experiment-v1"
    assert report["experiment"]["outcome"] == "keep"
    assert report["experiment"]["metric_frontier"] == 1.0
    assert report["budget_used"]["requests"] == 1
    assert report["budget_used"]["tool_calls"] == 1
    assert report["budget"]["max_tool_calls"] == 12

    outbox = _read_json(tmp_path / "state" / "outbox" / "latest.json")
    assert outbox["approval_gate"]["state"] == "fresh"
    assert outbox["next_hint"] == "none"
    assert outbox["latest_report"]["result_status"] == "PASS"
    assert outbox["latest_report"]["goal_id"] == "goal-123"
    assert outbox["latest_report"]["promotion_candidate_id"] == report["promotion_candidate_id"]

    report_index = _read_json(tmp_path / "state" / "outbox" / "report.index.json")
    assert report_index["status"] == "PASS"
    assert report_index["source"] == runtime["report_path"]
    assert report_index["goal"]["goal_id"] == "goal-123"
    assert report_index["goal"]["text"] == "goal-123"
    assert report_index["goal"]["follow_through"]["status"] == "artifact"
    assert report_index["goal"]["follow_through"]["artifact_paths"] == [runtime["report_path"]]
    assert report_index["goal_context"]["subagent_rollup"]["enabled"] is False
    assert report_index["improvement_score"] == 1.0
    assert report_index["capability_gate"]["approval"]["state"] == "fresh"
    assert report_index["promotion"]["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert report_index["promotion"]["candidate_path"].endswith(f"{report['promotion_candidate_id']}.json")
    assert report_index["promotion"]["review_status"] == "pending_policy_review"
    assert report_index["promotion"]["decision"] == "pending_policy_review"

    promotions_latest = _read_json(tmp_path / "state" / "promotions" / "latest.json")
    assert promotions_latest["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert promotions_latest["origin_cycle_id"] == report["cycle_id"]
    candidate_path = tmp_path / "state" / "promotions" / f"{report['promotion_candidate_id']}.json"
    candidate = _read_json(candidate_path)
    assert candidate["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert candidate["origin_cycle_id"] == report["cycle_id"]
    assert candidate["target_branch"] == "promote/self-evolving"
    assert candidate["promotion_provenance"]["source_commit"]
    assert candidate["promotion_provenance"]["deployment_fingerprint"]["deployment_fingerprint_id"].startswith(report["promotion_candidate_id"])
    assert candidate["evidence_refs"] == [report["evidence_ref_id"]]

    current = _read_json(tmp_path / "state" / "goals" / "current.json")
    assert current["schema_version"] == "task-plan-v1"
    assert current["current_task_id"] == "record-reward"
    generated = current.get("generated_candidates") or []
    assert generated == []
    assert current["task_counts"] == {"total": 3, "done": 2, "active": 1, "pending": 0}
    assert current["reward_signal"]["value"] == 1.0
    assert current["budget_used"]["requests"] == 1
    assert current["experiment"]["experiment_id"] == report["experiment"]["experiment_id"]
    history = _read_json(tmp_path / "state" / "goals" / "history" / f"cycle-{runtime['cycle_id']}.json")
    assert history["schema_version"] == "task-history-v1"
    assert history["recorded_at_utc"] == report["cycle_ended_utc"]
    assert history["current_task_id"] == "record-reward"


def test_cycle_writes_discard_revert_record_when_metric_regresses(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )
    experiments_dir = tmp_path / "state" / "experiments"
    experiments_dir.mkdir(parents=True)
    (experiments_dir / "latest.json").write_text(
        json.dumps({"metric_current": 2.0, "metric_frontier": 2.0, "experiment_id": "experiment-old"}),
        encoding="utf-8",
    )

    execute = AsyncMock(return_value="agent completed bounded work")
    now = expires_at - timedelta(minutes=30)
    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=now,
        )
    )
    assert "PASS" in summary
    runtime = load_runtime_state(tmp_path)
    assert runtime["experiment"]["outcome"] == "discard"
    assert runtime["experiment"]["metric_baseline"] == 2.0
    assert runtime["experiment"]["metric_current"] == 1.0
    assert runtime["experiment"]["metric_frontier"] == 2.0
    assert runtime["experiment"]["revert_required"] is True
    assert runtime["experiment"]["revert_status"] == "skipped_no_material_change"
    assert runtime["experiment"]["revert"]["revert_path"] == runtime["experiment"]["revert_path"]
    assert runtime["experiment"]["revert"]["reason"] == "discarded telemetry did not produce a material file change to revert"
    revert = _read_json(Path(runtime["experiment"]["revert_path"]))
    assert revert["experiment_id"] == runtime["experiment"]["experiment_id"]
    assert revert["outcome"] == "discard"
    assert revert["revert_status"] == "skipped_no_material_change"
    assert revert["terminal"] is True


def test_cycle_prefers_recorded_current_task_from_existing_plan(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "task-plan-v1",
                "current_task_id": "record-reward",
                "tasks": [
                    {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
                    {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "done"},
                    {"task_id": "record-reward", "title": "Record cycle reward", "status": "active"},
                ],
            }
        ),
        encoding="utf-8",
    )

    execute = AsyncMock(return_value="agent completed bounded work")

    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=expires_at - timedelta(minutes=30),
        )
    )

    execute.assert_awaited_once_with("Record cycle reward [task_id=record-reward]")
    assert "PASS" in summary

    runtime = load_runtime_state(tmp_path)
    assert runtime["current_task_id"] == "record-reward"
    report = _read_json(runtime["report_path"])
    assert report["selected_tasks"] == "Record cycle reward [task_id=record-reward]"
    assert report["task_selection_source"] == "recorded_current_task"
    outbox = _read_json(tmp_path / "state" / "outbox" / "latest.json")
    assert outbox["selected_tasks"] == "Record cycle reward [task_id=record-reward]"
    assert outbox["task_selection_source"] == "recorded_current_task"


def test_cycle_persists_recorded_feedback_decision_into_latest_authority_artifacts(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    recorded_feedback_decision = {
        "mode": "retire_terminal_selfevo_lane",
        "reason": "latest self-evolution issue reached a terminal merged/closed or terminal no-op state; do not recreate analyze-last-failed-candidate",
        "reward_value": 1.0,
        "current_task_id": "analyze-last-failed-candidate",
        "current_task_class": "other",
        "selected_task_id": "record-reward",
        "selected_task_class": "execution",
        "selection_source": "feedback_terminal_selfevo_retire",
        "selected_task_title": "Record cycle reward",
        "selected_task_label": "Record cycle reward [task_id=record-reward]",
        "terminal_selfevo_issue": {"terminal_status": "merged"},
    }
    (goals_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "task-plan-v1",
                "current_task_id": "analyze-last-failed-candidate",
                "tasks": [
                    {"task_id": "analyze-last-failed-candidate", "title": "Analyze the last failed self-evolution candidate", "status": "active"},
                    {"task_id": "record-reward", "title": "Record cycle reward", "status": "pending"},
                ],
                "feedback_decision": recorded_feedback_decision,
            }
        ),
        encoding="utf-8",
    )

    execute = AsyncMock(return_value="agent completed bounded work")

    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=expires_at - timedelta(minutes=30),
        )
    )

    execute.assert_awaited_once()
    assert "PASS" in summary

    runtime = load_runtime_state(tmp_path)
    current = _read_json(goals_dir / "current.json")
    outbox = _read_json(tmp_path / "state" / "outbox" / "latest.json")
    experiment = _read_json(tmp_path / "state" / "experiments" / "latest.json")
    report = _read_json(runtime["report_path"])
    report_index = _read_json(tmp_path / "state" / "outbox" / "report.index.json")
    goal_registry = _read_json(goals_dir / "registry.json")
    control_summary = _read_json(tmp_path / "state" / "control_plane" / "current_summary.json")

    # Regression guard for #177: before the fix, the resolved decision from
    # goals/current.json stayed there while the authoritative latest artifacts
    # were republished with feedback_decision=null.
    assert current["feedback_decision"]["mode"] == "retire_terminal_selfevo_lane"
    assert outbox["feedback_decision"]["mode"] == "retire_terminal_selfevo_lane"
    assert experiment["feedback_decision"]["mode"] == "retire_terminal_selfevo_lane"
    assert report["feedback_decision"]["mode"] == "retire_terminal_selfevo_lane"
    assert control_summary["task_plan"]["feedback_decision"]["mode"] == "retire_terminal_selfevo_lane"

    # Regression guard for #178: after a terminal self-evolution lane is retired,
    # every current-task surface must point to the selected follow-up lane. The
    # retired/pre-plan task is kept only as diagnostic feedback_decision context.
    assert current["current_task_id"] == "record-reward"
    assert outbox["current_task_id"] == "record-reward"
    assert experiment["current_task_id"] == "record-reward"
    assert report["current_task_id"] == "record-reward"
    assert report_index["current_task_id"] == "record-reward"
    assert control_summary["task_plan"]["current_task_id"] == "record-reward"
    assert control_summary["task_boundary"]["task_id"] == "record-reward"
    assert control_summary["experiment"]["current_task_id"] == "record-reward"
    assert report["feedback_decision"]["current_task_id"] == "analyze-last-failed-candidate"
    assert report["feedback_decision"]["selected_task_id"] == "record-reward"
    assert outbox["selected_tasks"] == "Record cycle reward [task_id=record-reward]"
    assert report["selected_tasks"] == "Record cycle reward [task_id=record-reward]"
    assert report_index["selected_tasks"] == "Record cycle reward [task_id=record-reward]"
    assert goal_registry["schema_version"] == "goal-registry-v1"
    assert goal_registry["active_goal_id"] == "goal-bootstrap"
    assert goal_registry["current_task_id"] == "record-reward"
    assert goal_registry["current_task"] == "Record cycle reward"
    assert goal_registry["latest_report_path"] == str(runtime["report_path"])
    assert goal_registry["latest_outbox_path"] == str(tmp_path / "state" / "outbox" / "report.index.json")


def test_cycle_rotates_goal_after_repeated_same_goal_artifact_passes(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    target_goal = "goal-44e50921129bf475"
    (goals_dir / "active.json").write_text(
        json.dumps({"active_goal": target_goal}),
        encoding="utf-8",
    )
    history_dir = goals_dir / "history"
    history_dir.mkdir(parents=True)
    for index in range(3):
        cycle_id = f"cycle-repeat-{index}"
        (history_dir / f"cycle-{cycle_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": "task-history-v1",
                    "cycle_id": cycle_id,
                    "goal_id": target_goal,
                    "result_status": "PASS",
                    "artifact_paths": ["prompts/diagnostics.md"],
                    "recorded_at_utc": f"2026-04-15T12:0{index}:00Z",
                }
            ),
            encoding="utf-8",
        )

    (goals_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "task-plan-v1",
                "cycle_id": "cycle-prior-pass",
                "goal_id": target_goal,
                "active_goal": target_goal,
                "current_task_id": "record-reward",
                "task_counts": {"total": 3, "done": 2, "active": 1, "pending": 0},
                "tasks": [
                    {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
                    {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "done"},
                    {"task_id": "record-reward", "title": "Record cycle reward", "status": "active"},
                ],
                "reward_signal": {
                    "value": 1.0,
                    "source": "result_status",
                    "result_status": "PASS",
                    "improvement_score": None,
                },
            }
        ),
        encoding="utf-8",
    )

    execute = AsyncMock(return_value="agent completed bounded work")
    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=expires_at - timedelta(minutes=30),
        )
    )

    execute.assert_awaited_once_with("Record cycle reward [task_id=record-reward]")
    assert "PASS" in summary

    runtime = load_runtime_state(tmp_path)
    assert runtime["active_goal"] == "goal-bootstrap"
    assert runtime["goal_rotation_reason"] == "goal/artifact PASS streak exceeded loop-breaker limit"
    assert runtime["goal_rotation_streak"] == 3
    assert runtime["goal_rotation_trigger_goal"] == target_goal
    assert runtime["goal_rotation_trigger_artifact_paths"] == ["prompts/diagnostics.md"]
    assert runtime["task_feedback_decision"]["mode"] == "retire_goal_artifact_pair"
    assert runtime["task_feedback_decision"]["retire_goal_artifact_pair"] is True

    report = _read_json(runtime["report_path"])
    assert report["goal_id"] == "goal-bootstrap"
    assert report["result_status"] == "PASS"
    assert report["feedback_decision"]["mode"] == "retire_goal_artifact_pair"

    active = _read_json(goals_dir / "active.json")
    assert active["active_goal"] == "goal-bootstrap"
    assert active["rotation_trigger_goal"] == target_goal
    assert active["rotation_trigger_artifact_paths"] == ["prompts/diagnostics.md"]
    assert active["rotation_streak"] == 3

    current = _read_json(goals_dir / "current.json")
    assert current["feedback_decision"]["mode"] == "retire_goal_artifact_pair"
    history = _read_json(history_dir / f"cycle-{runtime['cycle_id']}.json")
    assert history["feedback_decision"]["mode"] == "retire_goal_artifact_pair"
    experiment = _read_json(runtime["experiment_path"])
    assert experiment["feedback_decision"]["mode"] == "retire_goal_artifact_pair"


def test_cycle_writes_runtime_surfaces_into_host_control_plane_root_when_lane_active(tmp_path, monkeypatch):
    host_state = tmp_path / "host-state"
    approvals_dir = host_state / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    goals_dir = host_state / "goals"
    goals_dir.mkdir(parents=True)
    target_goal = "goal-host-control-plane"
    (goals_dir / "active.json").write_text(
        json.dumps({"active_goal": target_goal}),
        encoding="utf-8",
    )
    history_dir = goals_dir / "history"
    history_dir.mkdir(parents=True)
    for index in range(3):
        cycle_id = f"cycle-host-{index}"
        (history_dir / f"cycle-{cycle_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": "task-history-v1",
                    "cycle_id": cycle_id,
                    "goal_id": target_goal,
                    "result_status": "PASS",
                    "artifact_paths": ["prompts/diagnostics.md"],
                    "recorded_at_utc": f"2026-04-15T12:0{index}:00Z",
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_SOURCE", "host_control_plane")
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(host_state))

    execute = AsyncMock(return_value="agent completed bounded work")
    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=expires_at - timedelta(minutes=30),
        )
    )

    execute.assert_awaited_once_with("check open tasks")
    assert "PASS" in summary
    assert not (tmp_path / "state").exists()

    runtime = load_runtime_state_from_root(host_state, source_kind="host_control_plane")
    assert runtime["runtime_state_root"] == str(host_state)
    assert runtime["runtime_state_source"] == "host_control_plane"
    assert runtime["active_goal"] == "goal-bootstrap"
    assert runtime["goal_rotation_reason"] == "goal/artifact PASS streak exceeded loop-breaker limit"
    assert runtime["goal_rotation_streak"] == 3
    assert runtime["goal_rotation_trigger_goal"] == target_goal
    assert runtime["goal_rotation_trigger_artifact_paths"] == ["prompts/diagnostics.md"]
    assert runtime["current_task_id"] == "record-reward"
    assert runtime["task_reward_value"] == 1.0
    assert runtime["experiment_budget_used"]["requests"] == 1

    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "PASS"
    assert report["goal_id"] == "goal-bootstrap"
    assert report["approval_gate"]["state"] == "fresh"

    active = _read_json(host_state / "goals" / "active.json")
    assert active["active_goal"] == "goal-bootstrap"
    assert active["rotation_trigger_goal"] == target_goal
    assert active["rotation_trigger_artifact_paths"] == ["prompts/diagnostics.md"]
    assert active["rotation_streak"] == 3

    current = _read_json(host_state / "goals" / "current.json")
    assert current["schema_version"] == "task-plan-v1"
    assert current["current_task_id"] == "record-reward"
    assert current["reward_signal"]["value"] == 1.0
    assert current["history_path"] == runtime["task_history_path"]

    history = _read_json(host_state / "goals" / "history" / f"cycle-{runtime['cycle_id']}.json")
    assert history["schema_version"] == "task-history-v1"
    assert history["recorded_at_utc"] == report["cycle_ended_utc"]
    assert history["current_task_id"] == "record-reward"
    assert history["reward_signal"]["value"] == 1.0


def test_load_runtime_state_from_root_includes_subagent_telemetry_lane(tmp_path):
    state_root = tmp_path / "host-state"
    subagents_dir = state_root / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "sub-1.json").write_text(
        json.dumps(
            {
                "subagent_id": "sub-1",
                "status": "ok",
                "summary": "done",
                "result": "done",
            }
        ),
        encoding="utf-8",
    )

    runtime = load_runtime_state_from_root(
        state_root,
        source_kind="host_control_plane",
    )

    assert runtime["subagent_telemetry_root"] == str(subagents_dir)
    assert runtime["subagent_telemetry_count"] == 1
    assert runtime["subagent_telemetry_path"].endswith("sub-1.json")
    assert runtime["subagent_telemetry_latest_id"] == "sub-1"
    assert runtime["subagent_telemetry_latest_status"] == "ok"
    assert runtime["subagent_telemetry_latest_summary"] == "done"

    formatted = format_runtime_state(runtime)
    assert any("Subagent telemetry root" in line for line in formatted)
    assert any("Subagent telemetry latest" in line and "id=sub-1" in line for line in formatted)


def test_resolve_runtime_state_location_prefers_bridge_state_dir_when_canonical(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    bridge_state_root = tmp_path / "eeepc-agent" / "self-evolving-agent" / "state"

    monkeypatch.delenv("NANOBOT_RUNTIME_STATE_SOURCE", raising=False)
    monkeypatch.delenv("NANOBOT_RUNTIME_STATE_ROOT", raising=False)
    monkeypatch.setenv("STATE_DIR", str(bridge_state_root))

    state_root, source_kind = resolve_runtime_state_location(workspace)

    assert state_root == bridge_state_root
    assert source_kind == "host_control_plane"



def test_cycle_defaults_to_host_control_plane_root_for_eeepc_workspace_layout(tmp_path, monkeypatch):
    workspace = tmp_path / ".nanobot-eeepc" / "workspace"
    workspace.mkdir(parents=True)
    host_state = tmp_path / "eeepc-state"
    approvals_dir = host_state / "approvals"
    approvals_dir.mkdir(parents=True)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": "2026-04-15T13:00:00Z", "ttl_minutes": 60}),
        encoding="utf-8",
    )

    monkeypatch.delenv("NANOBOT_RUNTIME_STATE_SOURCE", raising=False)
    monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(host_state))

    execute = AsyncMock(return_value="agent completed bounded work")
    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=workspace,
            tasks="check open tasks",
            execute_turn=execute,
            now=datetime(2026, 4, 15, 12, 30, tzinfo=timezone.utc),
        )
    )

    execute.assert_awaited_once_with("check open tasks")
    assert "PASS" in summary
    assert not (workspace / "state").exists()
    assert (host_state / "goals" / "current.json").exists()
    assert (host_state / "goals" / "active.json").exists()
    assert list((host_state / "goals" / "history").glob("cycle-*.json"))


def test_cycle_persists_error_artifacts_when_execution_raises(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    async def _boom(_tasks: str) -> str:
        raise RuntimeError("bounded apply failed")

    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=_boom,
            now=expires_at - timedelta(minutes=30),
        )
    )

    assert "ERROR" in summary
    runtime = load_runtime_state(tmp_path)
    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "ERROR"
    assert report["execution_error"] == "bounded apply failed"
    outbox = _read_json(tmp_path / "state" / "outbox" / "latest.json")
    assert outbox["latest_report"]["result_status"] == "ERROR"
    current = _read_json(tmp_path / "state" / "goals" / "current.json")
    assert current["current_task_id"] == "run-bounded-turn"
    assert current["reward_signal"]["value"] == -1.0
    assert runtime["current_task_id"] == "run-bounded-turn"
    assert runtime["task_reward_value"] == -1.0


def test_cycle_switches_task_class_after_low_reward_when_possible(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "task-plan-v1",
                "cycle_id": "cycle-low-reward",
                "goal_id": "goal-low-reward",
                "active_goal": "goal-low-reward",
                "current_task_id": "record-reward",
                "task_counts": {"total": 3, "done": 2, "active": 1, "pending": 0},
                "tasks": [
                    {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
                    {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "done"},
                    {"task_id": "record-reward", "title": "Record cycle reward", "status": "active"},
                ],
                "reward_signal": {
                    "value": 0.0,
                    "source": "result_status",
                    "result_status": "BLOCK",
                    "improvement_score": None,
                },
            }
        ),
        encoding="utf-8",
    )

    execute = AsyncMock(return_value="agent completed bounded work")
    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=expires_at - timedelta(minutes=30),
        )
    )

    execute.assert_awaited_once_with("Run bounded turn [task_id=run-bounded-turn]")
    assert "PASS" in summary

    runtime = load_runtime_state(tmp_path)
    assert runtime["task_feedback_decision"]["mode"] == "switch_task_class"
    assert runtime["task_feedback_decision"]["selected_task_id"] == "run-bounded-turn"
    assert runtime["task_feedback_decision"]["selection_source"] == "feedback_low_reward_switch"
    assert runtime["current_task_id"] == "run-bounded-turn"

    report = _read_json(runtime["report_path"])
    assert report["feedback_decision"]["mode"] == "switch_task_class"
    assert report["feedback_decision"]["selected_task_id"] == "run-bounded-turn"

    current = _read_json(goals_dir / "current.json")
    assert current["current_task_id"] == "run-bounded-turn"
    assert current["feedback_decision"]["mode"] == "switch_task_class"
    assert current["next_cycle_task_id"] == "run-bounded-turn"

    experiment = _read_json(runtime["experiment_path"])
    assert experiment["feedback_decision"]["mode"] == "switch_task_class"

    formatted = format_runtime_state(runtime)
    assert any("Feedback" in line and "switch_task_class" in line for line in formatted)


def test_cycle_forces_remediation_after_repeated_block_failure_class(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    (approvals_dir / "apply.ok").write_text(json.dumps(["bad"]), encoding="utf-8")

    goals_dir = tmp_path / "state" / "goals"
    goals_dir.mkdir(parents=True)
    (goals_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "task-plan-v1",
                "cycle_id": "cycle-repeat-block",
                "goal_id": "goal-repeat-block",
                "active_goal": "goal-repeat-block",
                "current_task_id": "refresh-approval-gate",
                "task_counts": {"total": 2, "done": 0, "active": 1, "pending": 1},
                "tasks": [
                    {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "active"},
                    {"task_id": "verify-approval-gate", "title": "Verify the gate", "status": "pending"},
                ],
                "reward_signal": {
                    "value": 0.0,
                    "source": "result_status",
                    "result_status": "BLOCK",
                    "improvement_score": None,
                },
            }
        ),
        encoding="utf-8",
    )
    history_dir = goals_dir / "history"
    history_dir.mkdir(parents=True)
    for index in range(2):
        cycle_id = f"cycle-block-{index}"
        (history_dir / f"cycle-{cycle_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": "task-history-v1",
                    "cycle_id": cycle_id,
                    "goal_id": "goal-repeat-block",
                    "result_status": "BLOCK",
                    "approval_gate": {"state": "missing", "ttl_minutes": None},
                    "next_hint": "approval gate missing; refresh manually",
                    "recorded_at_utc": f"2026-04-15T11:0{index}:00Z",
                }
            ),
            encoding="utf-8",
        )

    execute = AsyncMock(return_value="should not run")
    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        )
    )

    execute.assert_not_awaited()
    assert "BLOCK" in summary

    runtime = load_runtime_state(tmp_path)
    assert runtime["task_feedback_decision"]["mode"] == "force_remediation"
    assert runtime["task_feedback_decision"]["repeat_block_count"] == 2
    assert runtime["task_feedback_decision"]["selected_task_id"] == "verify-approval-gate"
    assert runtime["current_task_id"] == "verify-approval-gate"

    report = _read_json(runtime["report_path"])
    assert report["feedback_decision"]["mode"] == "force_remediation"
    assert report["feedback_decision"]["repeat_block_count"] == 2

    current = _read_json(goals_dir / "current.json")
    assert current["current_task_id"] == "verify-approval-gate"
    assert current["feedback_decision"]["mode"] == "force_remediation"
    assert current["next_cycle_task_id"] == "verify-approval-gate"

    history = _read_json(history_dir / f"cycle-{runtime['cycle_id']}.json")
    assert history["feedback_decision"]["mode"] == "force_remediation"


def test_malformed_gate_payload_blocks_instead_of_crashing(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    (approvals_dir / "apply.ok").write_text(json.dumps(["bad"]), encoding="utf-8")

    execute = AsyncMock(return_value="should not run")
    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        )
    )

    execute.assert_not_awaited()
    assert "BLOCK" in summary
    runtime = load_runtime_state(tmp_path)
    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "BLOCK"
    assert report["approval_gate"]["state"] == "invalid"
    assert runtime["promotion_candidate_id"] is None
    assert not (tmp_path / "state" / "promotions").exists()


def test_cycle_accepts_epoch_based_approval_gate(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_epoch": int(expires_at.timestamp()), "managed_by": "keeper"}),
        encoding="utf-8",
    )

    execute = AsyncMock(return_value="agent completed bounded work")
    summary = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="check open tasks",
            execute_turn=execute,
            now=datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc),
        )
    )

    execute.assert_awaited_once()
    assert "PASS" in summary
    runtime = load_runtime_state(tmp_path)
    report = _read_json(runtime["report_path"])
    assert report["approval_gate"]["state"] == "fresh"
    assert report["result_status"] == "PASS"


@pytest.mark.asyncio
async def test_cycle_records_real_end_time_after_execution(tmp_path):
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    start = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": (start + timedelta(hours=1)).isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    async def _execute(_tasks: str) -> str:
        await asyncio.sleep(0.01)
        return "done"

    await run_self_evolving_cycle(
        workspace=tmp_path,
        tasks="check open tasks",
        execute_turn=_execute,
        now=start,
    )

    runtime = load_runtime_state(tmp_path)
    report = _read_json(runtime["report_path"])
    assert report["cycle_started_utc"] == "2026-04-15T12:00:00Z"
    assert report["cycle_ended_utc"] != report["cycle_started_utc"]


def test_subagent_rollup_materializes_terminal_telemetry_for_matching_request(tmp_path):
    state_root = tmp_path / "state"
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True)
    request_path = request_dir / "request-old.json"
    request_path.write_text(json.dumps({"task_id": "inspect-pass-streak", "request_status": "queued"}), encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    import os
    os.utime(request_path, (old.timestamp(), old.timestamp()))
    telemetry_path = state_root / "subagents" / "terminal-result.json"
    telemetry_path.write_text(json.dumps({"task_id": "inspect-pass-streak", "status": "done", "summary": "bounded review completed"}), encoding="utf-8")

    rollup = _subagent_rollup_snapshot(state_root=state_root, current_task_id="inspect-pass-streak")

    assert rollup["state"] == "completed"
    assert rollup["completed_result_count"] == 1
    assert rollup["stale_request_count"] == 0
    assert rollup["active_task_linkage"]["result_status"] == "done"
    assert rollup["active_task_linkage"]["source"] == "task_plan"
    assert rollup["latest_request"]["materialized_result_path"].endswith("terminal-result.json")


def test_subagent_materializer_executes_research_only_request_with_local_executor(tmp_path):
    from nanobot.runtime.subagent_materializer import materialize_subagent_requests

    state_root = tmp_path / "state"
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True)
    request_path = request_dir / "request-cycle-pi.json"
    request_path.write_text(json.dumps({
        "schema_version": "subagent-request-v1",
        "request_status": "queued",
        "task_id": "subagent-verify-materialized-improvement",
        "cycle_id": "cycle-pi",
        "profile": "research_only",
        "task_title": "Verify materialized proof",
        "source_artifact": "workspace/state/improvements/materialized-cycle-pi.json",
    }), encoding="utf-8")

    summary = materialize_subagent_requests(
        state_root=state_root,
        now=datetime(2026, 4, 25, 12, 10, tzinfo=timezone.utc),
        executor_command="python3 -c 'import sys; print(\"APPROVED:\" + sys.stdin.read()[:20])'",
    )

    assert summary["executed_count"] == 1
    assert summary["blocked_result_count"] == 0
    result = _read_json(Path(summary["results"][0]["path"]))
    assert result["status"] == "completed"
    assert result["result_status"] == "completed"
    assert result["terminal_reason"] is None
    assert result["executor"]["provider"] == "hermes_pi_qwen"
    assert result["executor"]["model"] == "gpt-5.3-codex"
    assert result["executor"]["base_url"] == "https://litellm.ayga.tech:9443/v1"
    assert "sk-" not in json.dumps(result)
    assert result["summary"].startswith("APPROVED:")

    rollup = _subagent_rollup_snapshot(state_root=state_root, current_task_id="subagent-verify-materialized-improvement")
    assert rollup["result_count"] == 1
    assert rollup["blocked_result_count"] == 0
    assert rollup["latest_result"]["status"] == "completed"


def test_subagent_materializer_records_executor_failure_without_leaking_command_secrets(tmp_path):
    from nanobot.runtime.subagent_materializer import materialize_subagent_requests

    state_root = tmp_path / "state"
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True)
    request_path = request_dir / "request-cycle-fail.json"
    request_path.write_text(json.dumps({
        "schema_version": "subagent-request-v1",
        "request_status": "queued",
        "task_id": "subagent-verify-materialized-improvement",
        "cycle_id": "cycle-fail",
        "profile": "research_only",
    }), encoding="utf-8")

    summary = materialize_subagent_requests(
        state_root=state_root,
        now=datetime(2026, 4, 25, 12, 10, tzinfo=timezone.utc),
        executor_command="python3 -c 'import sys; sys.stderr.write(\"bad sk-secret\"); raise SystemExit(7)'",
    )

    assert summary["executed_count"] == 0
    assert summary["blocked_result_count"] == 1
    result = _read_json(Path(summary["results"][0]["path"]))
    assert result["status"] == "blocked"
    assert result["terminal_reason"] == "local_executor_failed"
    assert result["executor"]["base_url"] == "https://litellm.ayga.tech:9443/v1"
    serialized = json.dumps(result)
    assert "sk-secret" not in serialized
    assert "python3 -c" not in serialized


def test_subagent_materializer_runs_executor_without_shell(tmp_path, monkeypatch):
    import subprocess
    from nanobot.runtime import subagent_materializer

    state_root = tmp_path / "state"
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True)
    (request_dir / "request-safe-argv.json").write_text(json.dumps({
        "schema_version": "subagent-request-v1",
        "request_status": "queued",
        "task_id": "safe-argv",
        "cycle_id": "cycle-safe-argv",
        "profile": "research_only",
    }), encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="APPROVED safe argv", stderr="")

    monkeypatch.setattr(subagent_materializer.subprocess, "run", fake_run)

    summary = subagent_materializer.materialize_subagent_requests(
        state_root=state_root,
        now=datetime(2026, 4, 25, 12, 10, tzinfo=timezone.utc),
        executor_command=["python3", "-c", "print('safe'); touch /tmp/should-not-run"],
    )

    assert summary["executed_count"] == 1
    assert calls
    assert calls[0]["shell"] is False
    assert isinstance(calls[0]["argv"], list)
    assert calls[0]["argv"][2] == "print('safe'); touch /tmp/should-not-run"


def test_subagent_materializer_pi_dev_executor_uses_public_json_argv(tmp_path, monkeypatch):
    import subprocess
    from nanobot.runtime import subagent_materializer

    state_root = tmp_path / "state"
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True)
    (request_dir / "request-pi-dev.json").write_text(json.dumps({
        "schema_version": "subagent-request-v1",
        "request_status": "queued",
        "task_id": "pi-dev-public-route",
        "cycle_id": "cycle-pi-dev",
        "profile": "research_only",
    }), encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="{\"response\":\"APPROVED pi route\"}", stderr="")

    monkeypatch.setattr(subagent_materializer.subprocess, "run", fake_run)
    monkeypatch.setenv("NANOBOT_SUBAGENT_EXECUTOR", "pi_dev")
    monkeypatch.delenv("NANOBOT_SUBAGENT_EXECUTOR_COMMAND", raising=False)

    summary = subagent_materializer.materialize_subagent_requests(
        state_root=state_root,
        now=datetime(2026, 4, 25, 12, 10, tzinfo=timezone.utc),
    )

    assert summary["executed_count"] == 1
    argv = calls[0]["argv"]
    assert calls[0]["shell"] is False
    assert argv[0].endswith("/pi") or argv[0] == "pi"
    assert "--mode" in argv
    assert "json" in argv
    assert "-p" in argv
    assert "--no-session" in argv
    assert "--no-tools" in argv
    assert argv[argv.index("--provider") + 1] == "hermes_pi_qwen"
    assert argv[argv.index("--model") + 1] == "gpt-5.3-codex"
    result = _read_json(Path(summary["results"][0]["path"]))
    assert result["executor"]["base_url"] == "https://litellm.ayga.tech:9443/v1"
    assert "coder-model" not in json.dumps(result)


def test_subagent_materializer_terminalizes_queued_request_and_rollup_correlates_result(tmp_path):
    from nanobot.runtime.subagent_materializer import materialize_subagent_requests

    state_root = tmp_path / "state"
    request_dir = state_root / "subagents" / "requests"
    request_dir.mkdir(parents=True)
    request_path = request_dir / "request-cycle-old.json"
    request_path.write_text(json.dumps({
        "schema_version": "subagent-request-v1",
        "request_status": "queued",
        "task_id": "subagent-verify-materialized-improvement",
        "cycle_id": "cycle-old",
        "profile": "research_only",
        "source_artifact": "workspace/state/improvements/materialized-cycle-old.json",
    }), encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    import os
    os.utime(request_path, (old.timestamp(), old.timestamp()))

    summary = materialize_subagent_requests(state_root=state_root, now=datetime(2026, 4, 25, 12, 10, tzinfo=timezone.utc))

    assert summary["terminalized_count"] == 1
    assert summary["blocked_result_count"] == 1
    result_path = Path(summary["results"][0]["path"])
    result = _read_json(result_path)
    assert result["schema_version"] == "subagent-result-v1"
    assert result["request_path"] == str(request_path)
    assert result["task_id"] == "subagent-verify-materialized-improvement"
    assert result["status"] == "blocked"
    assert result["terminal_reason"] == "local_executor_unavailable"

    rollup = _subagent_rollup_snapshot(state_root=state_root, current_task_id="subagent-verify-materialized-improvement")
    assert rollup["result_count"] == 1
    assert rollup["blocked_result_count"] == 1
    assert rollup["stale_request_count"] == 0
    assert rollup["latest_request"]["materialized_result_path"] == str(result_path)


def test_material_progress_rejects_stale_historic_proofs_for_discarded_current_cycle():
    runtime = {
        "current_task_id": "inspect-pass-streak",
        "experiment": {
            "outcome": "discard",
            "decision": "pending_policy_review",
            "review_status": "pending_policy_review",
            "revert_status": "skipped_no_material_change",
        },
        "selfevo_current_state": {
            "last_merge": {"pr_number": 28, "merged": True},
            "last_issue_lifecycle": {"issue_number": 27, "pr_number": 28, "status": "terminal_merged"},
        },
        "promotion_artifact_path": "/workspace/state/improvements/materialized-cycle-old.json",
        "subagent_rollup": {"state": "stale", "completed_result_count": 0, "latest_result": None, "active_task_id": "inspect-pass-streak"},
    }

    progress = _material_progress_snapshot(runtime)

    assert progress["state"] == "blocked"
    assert progress["healthy_autonomy_allowed"] is False
    assert progress["blocking_reason"] == "missing_current_material_progress"
    assert "historic_or_unlinked_selfevo_pr" in progress["non_qualifying_proofs"]
    assert "historic_or_unaccepted_promotion_artifact" in progress["non_qualifying_proofs"]


def test_subagent_rollup_counts_distinct_result_artifacts_for_same_task(tmp_path):
    state_root = tmp_path / "state"
    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    request_dir.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    for idx in range(2):
        request_path = request_dir / f"request-cycle-{idx}.json"
        request_path.write_text(json.dumps({"task_id": "same-task", "cycle_id": f"cycle-{idx}", "request_status": "queued"}), encoding="utf-8")
        result_path = result_dir / f"result-cycle-{idx}.json"
        result_path.write_text(json.dumps({"schema_version": "subagent-result-v1", "status": "blocked", "task_id": "same-task", "cycle_id": f"cycle-{idx}", "request_path": str(request_path)}), encoding="utf-8")

    rollup = _subagent_rollup_snapshot(state_root=state_root, current_task_id="same-task")

    assert rollup["result_count"] == 2
    assert rollup["blocked_result_count"] == 2
    assert rollup["stale_request_count"] == 0
    assert all(request["materialized_result_path"] for request in [rollup["latest_request"]])


def test_load_runtime_state_prefers_materialized_subagent_results_over_stale_outbox_rollup(tmp_path):
    state_root = tmp_path / "state"
    reports_dir = state_root / "reports"
    outbox_dir = state_root / "outbox"
    goals_dir = state_root / "goals"
    request_dir = state_root / "subagents" / "requests"
    result_dir = state_root / "subagents" / "results"
    for directory in (reports_dir, outbox_dir, goals_dir, request_dir, result_dir):
        directory.mkdir(parents=True)
    (goals_dir / "current.json").write_text(json.dumps({"current_task_id": "same-task", "current_task": "same-task"}), encoding="utf-8")
    (reports_dir / "evolution-latest.json").write_text(json.dumps({"cycle_id": "cycle-1", "current_task_id": "same-task", "result_status": "PASS"}), encoding="utf-8")
    (outbox_dir / "latest.json").write_text(json.dumps({"goal_context": {"subagent_rollup": {"state": "stale", "completed_result_count": 0, "stale_request_count": 2}}}), encoding="utf-8")
    request_path = request_dir / "request-cycle-1.json"
    request_path.write_text(json.dumps({"task_id": "same-task", "cycle_id": "cycle-1", "request_status": "queued"}), encoding="utf-8")
    (result_dir / "result-cycle-1.json").write_text(json.dumps({"schema_version": "subagent-result-v1", "status": "blocked", "task_id": "same-task", "cycle_id": "cycle-1", "request_path": str(request_path)}), encoding="utf-8")

    runtime = load_runtime_state_from_root(state_root)

    assert runtime["subagent_rollup"]["state"] == "completed"
    assert runtime["subagent_rollup"]["result_count"] == 1
    assert runtime["subagent_rollup"]["stale_request_count"] == 0
    assert runtime["subagent_rollup"]["latest_request"]["status"] == "blocked"
    assert runtime["subagent_rollup"]["latest_request"]["materialized_result_status"] == "blocked"


def test_material_progress_does_not_treat_blocked_subagent_terminalization_as_healthy():
    runtime = {
        "current_task_id": "inspect-pass-streak",
        "experiment": {
            "outcome": "discard",
            "decision": "pending_policy_review",
            "review_status": "pending_policy_review",
            "revert_status": "skipped_no_material_change",
        },
        "subagent_rollup": {
            "state": "completed",
            "completed_result_count": 1,
            "blocked_result_count": 1,
            "latest_result": {
                "path": "/workspace/state/subagents/results/result-cycle-1.json",
                "status": "blocked",
                "summary": "Subagent request terminalized as blocked because no local executor is available",
            },
            "active_task_id": "inspect-pass-streak",
        },
    }

    progress = _material_progress_snapshot(runtime)

    consumed = next(proof for proof in progress["proofs"] if proof["kind"] == "consumed_subagent_result")
    assert consumed["present"] is False
    assert consumed["reason"] == "subagent_result_blocked"
    assert progress["state"] == "blocked"
    assert progress["healthy_autonomy_allowed"] is False
    assert progress["blocking_reason"] == "missing_current_material_progress"
