"""Minimal durable self-evolving runtime coordinator."""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


DEFAULT_ACTIVE_GOAL = "goal-bootstrap"
GOAL_ROTATION_STREAK_LIMIT = 3
TASK_PLAN_VERSION = "task-plan-v1"
EXPERIMENT_VERSION = "experiment-v1"
DEFAULT_EXPERIMENT_BUDGET = {
    "max_requests": 1,
    "max_tool_calls": 8,
    "max_subagents": 2,
    "max_timeout_seconds": 900,
}


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return _utc_now(value).isoformat().replace("+00:00", "Z")


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_artifact_paths(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if item not in (None, ""))
    return (str(value),)


def _extract_history_signature(history_entry: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(history_entry, dict):
        return None
    result_status = history_entry.get("result_status") or history_entry.get("status")
    if result_status != "PASS":
        return None

    goal_id = history_entry.get("goal_id") or history_entry.get("active_goal") or history_entry.get("goalId")
    if not goal_id and isinstance(history_entry.get("goal"), dict):
        goal = history_entry.get("goal") or {}
        goal_id = goal.get("goal_id") or goal.get("goalId")

    artifact_paths = history_entry.get("artifact_paths") or history_entry.get("artifactPaths")
    if artifact_paths is None and isinstance(history_entry.get("follow_through"), dict):
        artifact_paths = history_entry["follow_through"].get("artifact_paths") or history_entry["follow_through"].get("artifactPaths")
    if artifact_paths is None and isinstance(history_entry.get("goal"), dict):
        follow_through = history_entry["goal"].get("follow_through")
        if isinstance(follow_through, dict):
            artifact_paths = follow_through.get("artifact_paths") or follow_through.get("artifactPaths")

    normalized_artifacts = _normalize_artifact_paths(artifact_paths)
    if not goal_id or not normalized_artifacts:
        return None
    return str(goal_id), normalized_artifacts


def _latest_goal_rotation_streak(goals_dir: Path, active_goal: str) -> tuple[int, tuple[str, tuple[str, ...]] | None]:
    if active_goal == DEFAULT_ACTIVE_GOAL:
        return 0, None

    history_dir = goals_dir / "history"
    if not history_dir.exists():
        return 0, None

    history_files = sorted(
        history_dir.glob("cycle-*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    if not history_files:
        return 0, None

    streak = 0
    signature: tuple[str, tuple[str, ...]] | None = None
    for path in history_files:
        payload = _safe_read_json(path)
        current_signature = _extract_history_signature(payload or {}) if isinstance(payload, dict) else None
        if current_signature is None:
            break
        if streak == 0:
            signature = current_signature
            if current_signature[0] != active_goal:
                break
            streak = 1
            continue
        if current_signature != signature:
            break
        streak += 1
    return streak, signature


def _write_active_goal(goals_dir: Path, active_goal: str, metadata: dict[str, Any] | None = None) -> None:
    goals_dir.mkdir(parents=True, exist_ok=True)
    active_path = goals_dir / "active.json"
    payload: dict[str, Any] = {"active_goal": active_goal}
    if metadata:
        payload.update(metadata)
    active_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _ensure_active_goal(goals_dir: Path, now: datetime | None = None) -> str:
    goals_dir.mkdir(parents=True, exist_ok=True)
    active_path = goals_dir / "active.json"
    active_goal = DEFAULT_ACTIVE_GOAL
    if active_path.exists():
        payload = _safe_read_json(active_path) or {}
        active_goal = (
            payload.get("active_goal")
            or payload.get("activeGoal")
            or payload.get("active_goal_id")
            or payload.get("activeGoalId")
            or payload.get("goal_id")
            or payload.get("goalId")
            or DEFAULT_ACTIVE_GOAL
        )

    streak, signature = _latest_goal_rotation_streak(goals_dir, active_goal)
    if streak >= GOAL_ROTATION_STREAK_LIMIT and signature is not None:
        rotated_from, artifact_paths = signature
        active_goal = DEFAULT_ACTIVE_GOAL
        _write_active_goal(
            goals_dir,
            active_goal,
            metadata={
                "rotation_reason": "goal/artifact PASS streak exceeded loop-breaker limit",
                "rotation_streak": streak,
                "rotation_trigger_goal": rotated_from,
                "rotation_trigger_artifact_paths": list(artifact_paths),
                "rotation_triggered_at_utc": _utc_iso(_utc_now(now)),
            },
        )
        return active_goal

    _write_active_goal(goals_dir, active_goal)
    return active_goal


def _load_approval_gate(state_root: Path, now: datetime) -> tuple[dict[str, Any], str]:
    approvals_dir = state_root / "approvals"
    gate_path = approvals_dir / "apply.ok"
    if not gate_path.exists():
        return (
            {"state": "missing", "ttl_minutes": None, "source": str(gate_path)},
            "approval gate missing; refresh manually",
        )

    raw_payload = _safe_read_json(gate_path)
    if not isinstance(raw_payload, dict):
        return (
            {"state": "invalid", "ttl_minutes": None, "source": str(gate_path)},
            "refresh approval gate manually",
        )

    payload = raw_payload
    expires_at = _parse_datetime(
        payload.get("expires_at_utc")
        or payload.get("expiresAtUtc")
        or payload.get("expires_at")
        or payload.get("expiresAt")
    )
    ttl_minutes = payload.get("ttl_minutes") or payload.get("ttlMinutes")
    if expires_at is not None:
        remaining_seconds = (expires_at - now).total_seconds()
        if remaining_seconds <= 0:
            return (
                {
                    "state": "expired",
                    "ttl_minutes": 0,
                    "expires_at_utc": _utc_iso(expires_at),
                    "source": str(gate_path),
                },
                "refresh approval gate manually",
            )
        computed_ttl = max(1, math.ceil(remaining_seconds / 60))
        return (
            {
                "state": "fresh",
                "ttl_minutes": int(ttl_minutes or computed_ttl),
                "expires_at_utc": _utc_iso(expires_at),
                "source": str(gate_path),
            },
            "none",
        )

    if ttl_minutes is not None:
        return (
            {
                "state": "fresh",
                "ttl_minutes": int(ttl_minutes),
                "source": str(gate_path),
            },
            "none",
        )

    return (
        {"state": "invalid", "ttl_minutes": None, "source": str(gate_path)},
        "refresh approval gate manually",
    )


def _derive_reward_signal(result_status: str, improvement_score: Any) -> dict[str, Any]:
    reward_value: float
    reward_source: str
    if improvement_score is not None:
        try:
            reward_value = float(improvement_score)
            reward_source = "improvement_score"
        except (TypeError, ValueError):
            reward_value = 0.0
            reward_source = "improvement_score_unusable"
    else:
        reward_value = {"PASS": 1.0, "BLOCK": 0.0, "ERROR": -1.0}.get(result_status, 0.0)
        reward_source = "result_status"

    return {
        "value": reward_value,
        "source": reward_source,
        "result_status": result_status,
        "improvement_score": improvement_score,
    }


def _derive_budget_usage(
    *,
    result_status: str,
    cycle_started_utc: str,
    cycle_ended_utc: str,
) -> dict[str, Any]:
    started = _parse_datetime(cycle_started_utc)
    ended = _parse_datetime(cycle_ended_utc)
    elapsed_seconds = 0
    if started is not None and ended is not None:
        elapsed_seconds = max(0, int((ended - started).total_seconds()))

    request_count = 1 if result_status in {"PASS", "ERROR"} else 0
    return {
        "requests": request_count,
        "tool_calls": 0,
        "subagents": 0,
        "elapsed_seconds": elapsed_seconds,
    }


def _build_experiment_snapshot(
    *,
    experiment_id: str,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
    selected_tasks: str,
    task_selection_source: str,
    cycle_started_utc: str,
    cycle_ended_utc: str,
    report_path: Path,
    history_path: Path,
    outbox_path: Path,
    promotion_candidate_id: str | None,
    review_status: str | None,
    decision: str | None,
    reward_signal: dict[str, Any],
) -> dict[str, Any]:
    budget = dict(DEFAULT_EXPERIMENT_BUDGET)
    budget_used = _derive_budget_usage(
        result_status=result_status,
        cycle_started_utc=cycle_started_utc,
        cycle_ended_utc=cycle_ended_utc,
    )
    if result_status == "BLOCK":
        budget_used["requests"] = 0
    return {
        "schema_version": EXPERIMENT_VERSION,
        "experiment_id": experiment_id,
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "result_status": result_status,
        "approval_gate_state": approval_gate_state,
        "next_hint": next_hint,
        "selected_tasks": selected_tasks,
        "task_selection_source": task_selection_source,
        "cycle_started_utc": cycle_started_utc,
        "cycle_ended_utc": cycle_ended_utc,
        "budget": budget,
        "budget_used": budget_used,
        "reward_signal": reward_signal,
        "promotion_candidate_id": promotion_candidate_id,
        "review_status": review_status,
        "decision": decision,
        "report_path": str(report_path),
        "history_path": str(history_path),
        "outbox_path": str(outbox_path),
    }


def _build_task_plan_snapshot(
    *,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
    experiment: dict[str, Any],
    report_path: Path,
    history_path: Path,
    improvement_score: Any,
) -> dict[str, Any]:
    blocked_next_step = next_hint if result_status == "BLOCK" else ""
    if result_status == "BLOCK":
        file_action = {
            "kind": "file_write",
            "path": "state/approvals/apply.ok",
            "summary": "Write a fresh approval gate with a valid TTL",
        }
        verification_command = "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"
        tasks = [
            {"task_id": "refresh-approval-gate", "title": file_action["summary"], "status": "active", **file_action},
            {"task_id": "verify-approval-gate", "title": f"Verify the gate with `{verification_command}`", "status": "pending", "command": verification_command},
        ]
    elif result_status == "ERROR":
        tasks = [
            {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
            {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "active"},
            {"task_id": "record-reward", "title": "Record cycle reward", "status": "pending"},
        ]
        file_action = None
        verification_command = None
    else:
        tasks = [
            {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
            {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "done"},
            {"task_id": "record-reward", "title": "Record cycle reward", "status": "active"},
        ]
        file_action = None
        verification_command = None

    current_task_id = next(task["task_id"] for task in tasks if task["status"] == "active")
    task_counts = {
        "total": len(tasks),
        "done": sum(1 for task in tasks if task["status"] == "done"),
        "active": sum(1 for task in tasks if task["status"] == "active"),
        "pending": sum(1 for task in tasks if task["status"] == "pending"),
    }
    reward_signal = _derive_reward_signal(result_status, improvement_score)
    payload = {
        "schema_version": TASK_PLAN_VERSION,
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "active_goal": goal_id,
        "result_status": result_status,
        "approval_gate_state": approval_gate_state,
        "next_hint": next_hint,
        "blocked_next_step": blocked_next_step,
        "current_task_id": current_task_id,
        "task_counts": task_counts,
        "tasks": tasks,
        "reward_signal": reward_signal,
        "budget": experiment["budget"],
        "budget_used": experiment["budget_used"],
        "experiment": experiment,
        "report_path": str(report_path),
        "history_path": str(history_path),
    }
    if file_action is not None:
        payload["file_action"] = file_action
    if verification_command is not None:
        payload["verification_command"] = verification_command
    return payload


def _derive_bounded_tasks_from_plan(tasks: str, task_plan: dict[str, Any] | None) -> tuple[str, str]:
    """Prefer the recorded current task from the prior plan when available."""
    if not isinstance(task_plan, dict):
        return tasks, "requested_tasks"

    current_task_id = task_plan.get("current_task_id") or task_plan.get("currentTaskId")
    if not current_task_id:
        return tasks, "requested_tasks"

    selected_task: dict[str, Any] | None = None
    recorded_tasks = task_plan.get("tasks")
    if isinstance(recorded_tasks, list):
        for task in recorded_tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("task_id") or task.get("taskId")
            if task_id == current_task_id:
                selected_task = task
                break

    if isinstance(selected_task, dict):
        task_title = selected_task.get("title") or selected_task.get("summary") or selected_task.get("task_id") or current_task_id
        return f"{task_title} [task_id={current_task_id}]", "recorded_current_task"

    return str(current_task_id), "recorded_current_task_id"


_DEFAULT_HOST_CONTROL_PLANE_STATE_ROOT = Path("/var/lib/eeepc-agent/self-evolving-agent/state")


def _workspace_looks_like_eeepc_live_runtime(workspace: Path) -> bool:
    """Detect the live eeepc runtime workspace layout.

    The live systemd unit runs the gateway from /home/opencode/.nanobot-eeepc/workspace.
    When that layout is present and no explicit runtime-state source is set, we should
    promote the canonical host-control-plane state root instead of the workspace-local
    fallback so the live activation actually emits goals/current/active/history files.
    """
    return workspace.parent.name == ".nanobot-eeepc" and workspace.name == "workspace"


def _resolve_runtime_state_root(workspace: Path) -> Path:
    source_kind = os.getenv("NANOBOT_RUNTIME_STATE_SOURCE")
    if source_kind is None:
        source_kind = "host_control_plane" if _workspace_looks_like_eeepc_live_runtime(workspace) else "workspace_state"
    if source_kind == "host_control_plane":
        override = os.getenv("NANOBOT_RUNTIME_STATE_ROOT")
        return Path(override).expanduser() if override else _DEFAULT_HOST_CONTROL_PLANE_STATE_ROOT
    return workspace / "state"


async def run_self_evolving_cycle(
    workspace: Path,
    tasks: str,
    execute_turn: Callable[[str], Awaitable[str]],
    now: datetime | None = None,
) -> str:
    """Run one bounded self-evolving cycle and persist canonical artifacts."""
    current = _utc_now(now)
    state_root = _resolve_runtime_state_root(workspace)
    reports_dir = state_root / "reports"
    goals_dir = state_root / "goals"
    outbox_dir = state_root / "outbox"
    promotions_dir = state_root / "promotions"
    experiments_dir = state_root / "experiments"
    for directory in (reports_dir, goals_dir, outbox_dir, experiments_dir):
        directory.mkdir(parents=True, exist_ok=True)

    recorded_task_plan = _safe_read_json(goals_dir / "current.json")
    selected_tasks, task_selection_source = _derive_bounded_tasks_from_plan(tasks, recorded_task_plan)

    active_goal = _ensure_active_goal(goals_dir, current)
    approval_gate, next_hint = _load_approval_gate(state_root, current)

    cycle_id = f"cycle-{uuid.uuid4().hex[:12]}"
    evidence_ref_id = f"evidence-{uuid.uuid4().hex[:12]}"
    cycle_started = _utc_iso(current)

    execution_response: str | None = None
    execution_error: str | None = None
    promotion_candidate_id: str | None = None
    review_status: str | None = None
    decision: str | None = None
    if approval_gate["state"] == "fresh":
        try:
            execution_response = await execute_turn(selected_tasks)
            promotion_candidate_id = f"promotion-{uuid.uuid4().hex[:12]}"
            review_status = "pending"
            decision = "pending"
            result_status = "PASS"
            bounded_apply = "on"
            promotion_execute = "on"
            summary = f"Self-evolving cycle PASS — goal={active_goal} — evidence={evidence_ref_id}"
        except Exception as exc:
            execution_error = str(exc)
            result_status = "ERROR"
            bounded_apply = "off"
            promotion_execute = "off"
            summary = f"Self-evolving cycle ERROR — goal={active_goal} — {execution_error}"
    else:
        result_status = "BLOCK"
        bounded_apply = "off"
        promotion_execute = "off"
        summary = f"Self-evolving cycle BLOCK — goal={active_goal} — {next_hint}"

    cycle_ended = _utc_iso(datetime.now(timezone.utc))
    history_dir = goals_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"cycle-{cycle_id}.json"
    report_path = reports_dir / f"evolution-{current.strftime('%Y%m%dT%H%M%SZ')}-{cycle_id}.json"
    experiment_id = f"experiment-{cycle_id}"
    experiment_path = experiments_dir / f"{experiment_id}.json"
    outbox_path = outbox_dir / "latest.json"
    reward_signal = _derive_reward_signal(result_status, None)
    experiment = _build_experiment_snapshot(
        experiment_id=experiment_id,
        cycle_id=cycle_id,
        goal_id=active_goal,
        result_status=result_status,
        approval_gate_state=approval_gate["state"],
        next_hint=next_hint,
        selected_tasks=selected_tasks,
        task_selection_source=task_selection_source,
        cycle_started_utc=cycle_started,
        cycle_ended_utc=cycle_ended,
        report_path=report_path,
        history_path=history_path,
        outbox_path=outbox_path,
        promotion_candidate_id=promotion_candidate_id,
        review_status=review_status,
        decision=decision,
        reward_signal=reward_signal,
    )
    report = {
        "cycle_id": cycle_id,
        "cycle_started_utc": cycle_started,
        "cycle_ended_utc": cycle_ended,
        "goal_id": active_goal,
        "tasks": tasks,
        "selected_tasks": selected_tasks,
        "task_selection_source": task_selection_source,
        "result_status": result_status,
        "evidence_ref_id": evidence_ref_id,
        "promotion_candidate_id": promotion_candidate_id,
        "review_status": review_status,
        "decision": decision,
        "approval_gate": approval_gate,
        "next_hint": next_hint,
        "bounded_apply": bounded_apply,
        "promotion_execute": promotion_execute,
        "budget": experiment["budget"],
        "budget_used": experiment["budget_used"],
        "experiment": experiment,
        "experiment_path": str(experiment_path),
        "summary": summary,
        "execution_response": execution_response,
        "execution_error": execution_error,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    promotion_path = None
    if promotion_candidate_id:
        promotions_dir.mkdir(parents=True, exist_ok=True)
        promotion_record = {
            "promotion_candidate_id": promotion_candidate_id,
            "candidate_created_utc": cycle_ended,
            "origin_cycle_id": cycle_id,
            "origin_host": "local-workspace",
            "source_paths": [str(report_path)],
            "target_repo": "ozand/nanobot",
            "target_branch": "promote/self-evolving",
            "base_commit": None,
            "candidate_patch_hash": None,
            "evidence_refs": [evidence_ref_id],
            "validation_summary": result_status,
            "resource_impact_summary": None,
            "rollback_plan": "Revert the candidate and keep host-local only.",
            "review_status": review_status,
            "decision": decision,
            "experiment_id": experiment_id,
            "budget": experiment["budget"],
            "budget_used": experiment["budget_used"],
        }
        promotion_path = promotions_dir / f"{promotion_candidate_id}.json"
        promotion_path.write_text(json.dumps(promotion_record, indent=2, ensure_ascii=False), encoding="utf-8")
        (promotions_dir / "latest.json").write_text(
            json.dumps({**promotion_record, "candidate_path": str(promotion_path)}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    outbox = {
        "approval_gate": approval_gate,
        "next_hint": next_hint,
        "summary": summary,
        "selected_tasks": selected_tasks,
        "task_selection_source": task_selection_source,
        "budget": experiment["budget"],
        "budget_used": experiment["budget_used"],
        "experiment": experiment,
        "goal": {
            "goal_id": active_goal,
            "text": active_goal,
            "follow_through": {
                "status": "artifact" if execution_response and result_status == "PASS" else "blocked_next_action",
                "blocked_next_step": "" if result_status == "PASS" else next_hint,
                "artifact_paths": [],
                "action_summary": summary,
            },
        },
        "latest_report": {
            "cycle_id": cycle_id,
            "goal_id": active_goal,
            "result_status": result_status,
            "evidence_ref_id": evidence_ref_id,
            "promotion_candidate_id": promotion_candidate_id,
            "review_status": review_status,
            "decision": decision,
            "candidate_path": str(promotion_path) if promotion_path else None,
            "summary": summary,
            "report_path": str(report_path),
            "experiment_id": experiment_id,
        },
    }
    if result_status == "BLOCK":
        outbox["goal"]["follow_through"]["file_action"] = {
            "kind": "file_write",
            "path": "state/approvals/apply.ok",
            "summary": "Write a fresh approval gate with a valid TTL",
        }
        outbox["goal"]["follow_through"]["verification_command"] = "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"

    outbox_path.write_text(
        json.dumps(outbox, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    report_index = {
        "ok": result_status != "ERROR",
        "source": str(report_path),
        "status": result_status,
        "improvement_score": reward_signal["value"],
        "budget": experiment["budget"],
        "budget_used": experiment["budget_used"],
        "experiment": experiment,
        "goal": {
            "goal_id": active_goal,
            "text": active_goal,
            "follow_through": {
                "status": "artifact" if execution_response and result_status == "PASS" else "blocked_next_action",
                "blocked_next_step": "" if result_status == "PASS" else next_hint,
                "artifact_paths": [],
                "action_summary": summary,
            },
        },
        "goal_context": {
            "subagent_rollup": {
                "enabled": False,
                "count_total": 0,
                "count_done": 0,
            }
        },
        "capability_gate": {
            "approval": approval_gate,
        },
        "promotion": {
            "promotion_candidate_id": promotion_candidate_id,
            "candidate_path": str(promotion_path) if promotion_path else None,
            "review_status": review_status,
            "decision": decision,
        },
    }
    if result_status == "BLOCK":
        report_index["goal"]["follow_through"]["file_action"] = {
            "kind": "file_write",
            "path": "state/approvals/apply.ok",
            "summary": "Write a fresh approval gate with a valid TTL",
        }
        report_index["goal"]["follow_through"]["verification_command"] = "PYTHONPATH=. pytest -q tests/test_runtime_coordinator.py"
    report_index_path = outbox_dir / "report.index.json"
    report_index_path.write_text(
        json.dumps(report_index, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    history_dir = goals_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"cycle-{cycle_id}.json"
    current_plan = _build_task_plan_snapshot(
        cycle_id=cycle_id,
        goal_id=active_goal,
        result_status=result_status,
        approval_gate_state=approval_gate["state"],
        next_hint=next_hint,
        experiment=experiment,
        report_path=report_path,
        history_path=history_path,
        improvement_score=report_index["improvement_score"],
    )
    history_entry = {
        **current_plan,
        "schema_version": "task-history-v1",
        "recorded_at_utc": cycle_ended,
        "report_index_path": str(report_index_path),
        "cycle_started_utc": cycle_started,
        "cycle_ended_utc": cycle_ended,
        "evidence_ref_id": evidence_ref_id,
        "approval_gate": approval_gate,
        "summary": summary,
    }
    (goals_dir / "current.json").write_text(
        json.dumps(current_plan, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    experiment_record = {
        **experiment,
        "report_path": str(report_path),
        "history_path": str(history_path),
        "outbox_path": str(outbox_path),
        "report_index_path": str(report_index_path),
    }
    experiment_path.write_text(
        json.dumps(experiment_record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (experiments_dir / "latest.json").write_text(
        json.dumps({**experiment_record, "experiment_path": str(experiment_path)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    history_path.write_text(
        json.dumps(history_entry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return summary
