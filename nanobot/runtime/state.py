"""Canonical runtime state helpers for operator-facing summaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _safe_read_json(path: Path | None) -> Any:
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_json_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return matches[0] if matches else None


def load_runtime_state_from_root(state_root: Path, source_kind: str = "workspace_state") -> dict[str, Any]:
    """Load canonical runtime state from an explicit state root if present."""
    reports_dir = state_root / "reports"
    outbox_dir = state_root / "outbox"
    goals_dir = state_root / "goals"
    promotions_dir = state_root / "promotions"

    latest_report = _latest_json_file(reports_dir, "evolution-*.json") or _latest_json_file(reports_dir, "*.json")
    latest_goal = _latest_json_file(goals_dir, "*.json")
    latest_outbox = _latest_json_file(outbox_dir, "latest.json") or _latest_json_file(outbox_dir, "*.json")
    latest_promotion = _latest_json_file(promotions_dir, "latest.json") or _latest_json_file(promotions_dir, "*.json")

    report_data = _safe_read_json(latest_report)
    goal_data = _safe_read_json(latest_goal)
    outbox_data = _safe_read_json(latest_outbox)
    promotion_data = _safe_read_json(latest_promotion)

    approval_gate = None
    explicit_next_hint = None
    if isinstance(outbox_data, dict):
        approval_gate = outbox_data.get("approval_gate") or outbox_data.get("approvalGate")
        explicit_next_hint = outbox_data.get("next_hint") or outbox_data.get("nextHint")

    approval_gate_state = None
    approval_gate_ttl_minutes = None
    next_hint = explicit_next_hint
    if isinstance(approval_gate, dict):
        approval_gate_state = approval_gate.get("state") or approval_gate.get("status")
        approval_gate_ttl_minutes = approval_gate.get("ttl_minutes") or approval_gate.get("ttlMinutes")
        if next_hint is None:
            if approval_gate_state in {"fresh", "active", "valid", "ok"}:
                next_hint = "none"
            else:
                next_hint = "refresh approval gate manually"
    elif approval_gate:
        approval_gate_state = str(approval_gate)
        if next_hint is None:
            next_hint = "refresh approval gate manually"
    elif next_hint is None:
        next_hint = "approval gate missing; refresh manually"

    active_goal = None
    if isinstance(goal_data, dict):
        active_goal = goal_data.get("active_goal") or goal_data.get("activeGoal") or goal_data.get("goal_id") or goal_data.get("goalId")
    if not active_goal and isinstance(report_data, dict):
        active_goal = (
            report_data.get("goal_id")
            or report_data.get("goalId")
            or ((report_data.get("goal") or {}).get("goal_id") if isinstance(report_data.get("goal"), dict) else None)
            or ((report_data.get("goal") or {}).get("goalId") if isinstance(report_data.get("goal"), dict) else None)
        )

    cycle_id = None
    cycle_started = None
    cycle_ended = None
    evidence_ref = None
    promotion_candidate_id = None
    review_status = None
    decision = None
    decision_reason = None
    runtime_status = None
    artifact_paths = None
    promotion_path = str(latest_promotion) if latest_promotion else None
    if isinstance(report_data, dict):
        cycle_id = report_data.get("cycle_id") or report_data.get("cycleId")
        cycle_started = report_data.get("cycle_started_utc") or report_data.get("cycleStartedUtc")
        cycle_ended = report_data.get("cycle_ended_utc") or report_data.get("cycleEndedUtc")
        evidence_ref = report_data.get("evidence_ref_id") or report_data.get("evidenceRefId")
        promotion_candidate_id = report_data.get("promotion_candidate_id") or report_data.get("promotionCandidateId")
        review_status = report_data.get("review_status") or report_data.get("reviewStatus")
        decision = report_data.get("decision")
        runtime_status = (
            report_data.get("result_status")
            or report_data.get("resultStatus")
            or ((report_data.get("process_reflection") or {}).get("status") if isinstance(report_data.get("process_reflection"), dict) else None)
        )
        follow_through = report_data.get("follow_through") if isinstance(report_data.get("follow_through"), dict) else None
        if isinstance(follow_through, dict):
            artifact_paths = follow_through.get("artifact_paths") or follow_through.get("artifactPaths")
        capability_gate = report_data.get("capability_gate") if isinstance(report_data.get("capability_gate"), dict) else None
        if approval_gate is None and isinstance(capability_gate, dict):
            approval_gate = capability_gate.get("approval") if isinstance(capability_gate.get("approval"), dict) else None
            if isinstance(approval_gate, dict):
                approval_gate_state = approval_gate.get("reason") or ("ok" if approval_gate.get("ok") else "blocked")

    if isinstance(promotion_data, dict):
        promotion_candidate_id = (
            promotion_data.get("promotion_candidate_id")
            or promotion_data.get("promotionCandidateId")
            or promotion_candidate_id
        )
        review_status = promotion_data.get("review_status") or promotion_data.get("reviewStatus") or review_status
        decision = promotion_data.get("decision") or decision
        decision_reason = promotion_data.get("decision_reason") or promotion_data.get("decisionReason") or decision_reason

    return {
        "runtime_state_source": source_kind,
        "runtime_state_root": str(state_root),
        "active_goal": active_goal,
        "cycle_id": cycle_id,
        "cycle_started_utc": cycle_started,
        "cycle_ended_utc": cycle_ended,
        "evidence_ref": evidence_ref,
        "promotion_candidate_id": promotion_candidate_id,
        "review_status": review_status,
        "decision": decision,
        "decision_reason": decision_reason,
        "runtime_status": runtime_status,
        "artifact_paths": artifact_paths,
        "promotion_path": promotion_path,
        "approval_gate": approval_gate,
        "approval_gate_state": approval_gate_state,
        "approval_gate_ttl_minutes": approval_gate_ttl_minutes,
        "next_hint": next_hint,
        "report_path": str(latest_report) if latest_report else None,
        "goal_path": str(latest_goal) if latest_goal else None,
        "outbox_path": str(latest_outbox) if latest_outbox else None,
    }



def load_runtime_state(workspace: Path) -> dict[str, Any]:
    """Load canonical runtime state from the workspace if present."""
    return load_runtime_state_from_root(workspace / "state", source_kind="workspace_state")


def format_runtime_state(runtime: dict[str, Any]) -> list[str]:
    """Format the canonical runtime state into stable user-facing lines."""
    lines = ["Runtime:"]

    def _render(label: str, value: Any) -> None:
        if value in (None, ""):
            lines.append(f"  {label}: unknown")
        elif isinstance(value, dict):
            compact = ", ".join(f"{k}={v}" for k, v in value.items())
            lines.append(f"  {label}: {compact or 'unknown'}")
        else:
            lines.append(f"  {label}: {value}")

    _render("Active goal", runtime.get("active_goal"))
    _render("Cycle", runtime.get("cycle_id"))
    _render("Cycle started", runtime.get("cycle_started_utc"))
    _render("Cycle ended", runtime.get("cycle_ended_utc"))
    _render("Evidence", runtime.get("evidence_ref"))
    _render("Promotion candidate", runtime.get("promotion_candidate_id"))
    _render("Promotion review", runtime.get("review_status"))
    _render("Promotion decision", runtime.get("decision"))
    _render("Promotion reason", runtime.get("decision_reason"))
    _render("Promotion source", runtime.get("promotion_path"))
    _render("Approval gate", runtime.get("approval_gate"))
    _render("Gate state", runtime.get("approval_gate_state"))
    if runtime.get("approval_gate_ttl_minutes") is not None:
        _render("Gate TTL (min)", runtime.get("approval_gate_ttl_minutes"))
    _render("Next", runtime.get("next_hint"))
    _render("Report source", runtime.get("report_path"))
    _render("Goal source", runtime.get("goal_path"))
    _render("Outbox source", runtime.get("outbox_path"))
    return lines