"""Minimal durable self-evolving runtime coordinator."""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


DEFAULT_ACTIVE_GOAL = "goal-bootstrap"
TASK_PLAN_VERSION = "task-plan-v1"


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


def _ensure_active_goal(goals_dir: Path) -> str:
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
    active_path.write_text(
        json.dumps({"active_goal": active_goal}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return active_goal


def _load_approval_gate(workspace: Path, now: datetime) -> tuple[dict[str, Any], str]:
    approvals_dir = workspace / "state" / "approvals"
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


def _build_task_plan_snapshot(
    *,
    cycle_id: str,
    goal_id: str,
    result_status: str,
    approval_gate_state: str,
    next_hint: str,
    report_path: Path,
    history_path: Path,
    improvement_score: Any,
) -> dict[str, Any]:
    if result_status == "BLOCK":
        tasks = [
            {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "active"},
            {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "pending"},
            {"task_id": "record-reward", "title": "Record cycle reward", "status": "pending"},
        ]
    elif result_status == "ERROR":
        tasks = [
            {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
            {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "active"},
            {"task_id": "record-reward", "title": "Record cycle reward", "status": "pending"},
        ]
    else:
        tasks = [
            {"task_id": "refresh-approval-gate", "title": "Refresh approval gate", "status": "done"},
            {"task_id": "run-bounded-turn", "title": "Run bounded turn", "status": "done"},
            {"task_id": "record-reward", "title": "Record cycle reward", "status": "active"},
        ]

    current_task_id = next(task["task_id"] for task in tasks if task["status"] == "active")
    task_counts = {
        "total": len(tasks),
        "done": sum(1 for task in tasks if task["status"] == "done"),
        "active": sum(1 for task in tasks if task["status"] == "active"),
        "pending": sum(1 for task in tasks if task["status"] == "pending"),
    }
    reward_signal = _derive_reward_signal(result_status, improvement_score)
    return {
        "schema_version": TASK_PLAN_VERSION,
        "cycle_id": cycle_id,
        "goal_id": goal_id,
        "active_goal": goal_id,
        "result_status": result_status,
        "approval_gate_state": approval_gate_state,
        "next_hint": next_hint,
        "current_task_id": current_task_id,
        "task_counts": task_counts,
        "tasks": tasks,
        "reward_signal": reward_signal,
        "report_path": str(report_path),
        "history_path": str(history_path),
    }


async def run_self_evolving_cycle(
    workspace: Path,
    tasks: str,
    execute_turn: Callable[[str], Awaitable[str]],
    now: datetime | None = None,
) -> str:
    """Run one bounded self-evolving cycle and persist canonical artifacts."""
    current = _utc_now(now)
    state_root = workspace / "state"
    reports_dir = state_root / "reports"
    goals_dir = state_root / "goals"
    outbox_dir = state_root / "outbox"
    promotions_dir = state_root / "promotions"
    for directory in (reports_dir, goals_dir, outbox_dir):
        directory.mkdir(parents=True, exist_ok=True)

    active_goal = _ensure_active_goal(goals_dir)
    approval_gate, next_hint = _load_approval_gate(workspace, current)

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
            execution_response = await execute_turn(tasks)
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
    report = {
        "cycle_id": cycle_id,
        "cycle_started_utc": cycle_started,
        "cycle_ended_utc": cycle_ended,
        "goal_id": active_goal,
        "tasks": tasks,
        "result_status": result_status,
        "evidence_ref_id": evidence_ref_id,
        "promotion_candidate_id": promotion_candidate_id,
        "review_status": review_status,
        "decision": decision,
        "approval_gate": approval_gate,
        "next_hint": next_hint,
        "bounded_apply": bounded_apply,
        "promotion_execute": promotion_execute,
        "summary": summary,
        "execution_response": execution_response,
        "execution_error": execution_error,
    }
    report_path = reports_dir / f"evolution-{current.strftime('%Y%m%dT%H%M%SZ')}-{cycle_id}.json"
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
        },
    }
    (outbox_dir / "latest.json").write_text(
        json.dumps(outbox, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    reward_signal = _derive_reward_signal(result_status, None)
    report_index = {
        "ok": result_status != "ERROR",
        "source": str(report_path),
        "status": result_status,
        "improvement_score": reward_signal["value"],
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
    history_path.write_text(
        json.dumps(history_entry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return summary
