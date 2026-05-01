"""Minimal durable promotion candidate review/apply workflow."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VALID_DECISIONS = {"accept", "reject", "defer", "needs_more_evidence"}
_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PROMOTION_RECORD_VERSION = 'promotion-record-v1'
PATCH_BUNDLE_VERSION = 'promotion-patch-v1'
READINESS_PACKET_VERSION = 'promotion-readiness-packet-v1'
READINESS_INPUTS_BLOCKER_VERSION = 'promotion-readiness-inputs-blocker-v1'


def _utc_iso(now: datetime | None = None) -> str:
    current = now.astimezone(timezone.utc) if now and now.tzinfo else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc))
    return current.isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def complete_promotion_readiness_packet(
    workspace: Path,
    candidate_id: str,
    now: datetime | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Write a durable not-ready readiness packet without accepting a promotion."""
    if not candidate_id or not _CANDIDATE_ID_RE.fullmatch(candidate_id) or candidate_id.startswith("."):
        raise ValueError("candidate_id is invalid")
    promotions_dir = (state_root if state_root is not None else workspace / "state") / "promotions"
    candidate_path = promotions_dir / f"{candidate_id}.json"
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)
    candidate = _read_json(candidate_path)
    governance = candidate.get("governance_packet") if isinstance(candidate.get("governance_packet"), dict) else {}
    review_status = candidate.get("review_status")
    decision = candidate.get("decision")
    review_packet_status = governance.get("review_packet_status") or candidate.get("review_packet_status")
    if review_status != "not_ready_for_policy_review" and decision != "not_ready_for_policy_review" and review_packet_status != "not_ready":
        raise ValueError("promotion candidate is not in not-ready policy-review state")
    completed_at = _utc_iso(now)
    readiness_checks = candidate.get("readiness_checks") or governance.get("readiness_checks")
    readiness_reasons = candidate.get("readiness_reasons") or governance.get("readiness_reasons") or []
    missing_records = [
        name
        for name, value in {
            "decision_record": candidate.get("decision_record"),
            "accepted_record": candidate.get("accepted_record"),
        }.items()
        if value in {None, "", "missing"}
    ]
    packet = {
        "schema_version": READINESS_PACKET_VERSION,
        "promotion_candidate_id": candidate_id,
        "origin_cycle_id": candidate.get("origin_cycle_id"),
        "state": "blocked",
        "reason": "promotion_candidate_not_ready_for_policy_review",
        "review_status": "not_ready_for_policy_review",
        "decision": "not_ready_for_policy_review",
        "decision_record": "blocked_not_ready",
        "accepted_record": "not_created_not_ready",
        "missing_records": missing_records,
        "readiness_checks": readiness_checks,
        "readiness_reasons": readiness_reasons,
        "candidate_path": str(candidate_path),
        "artifact_path": candidate.get("artifact_path"),
        "completed_at_utc": completed_at,
        "recommended_next_action": "supply_missing_promotion_readiness_inputs",
    }
    packet_path = promotions_dir / "readiness_packets" / f"{candidate_id}.json"
    _write_json(packet_path, packet)
    updated_governance = {
        **governance,
        "review_packet_status": "blocked_not_ready",
        "readiness_packet_path": str(packet_path),
        "readiness_packet_schema_version": READINESS_PACKET_VERSION,
    }
    updated = {
        **candidate,
        "review_status": "not_ready_for_policy_review",
        "decision": "not_ready_for_policy_review",
        "decision_record": "blocked_not_ready",
        "accepted_record": "not_created_not_ready",
        "readiness_packet_path": str(packet_path),
        "readiness_packet_completed_at_utc": completed_at,
        "recommended_next_action": "supply_missing_promotion_readiness_inputs",
        "governance_packet": updated_governance,
    }
    _write_json(candidate_path, updated)
    _write_json(promotions_dir / "latest.json", {**updated, "candidate_path": str(candidate_path)})
    return {**packet, "readiness_packet_path": str(packet_path)}


def _missing_provenance_inputs(candidate: dict[str, Any]) -> list[str]:
    provenance = candidate.get("promotion_provenance") if isinstance(candidate.get("promotion_provenance"), dict) else {}
    deployment_fingerprint = provenance.get("deployment_fingerprint") if isinstance(provenance.get("deployment_fingerprint"), dict) else {}
    values = {
        "source_commit": provenance.get("source_commit") or candidate.get("source_commit"),
        "build_recipe_hash": provenance.get("build_recipe_hash") or candidate.get("build_recipe_hash"),
        "artifact_id": provenance.get("artifact_id") or candidate.get("promotion_candidate_id"),
        "artifact_version": provenance.get("artifact_version") or candidate.get("origin_cycle_id"),
        "release_channel": provenance.get("release_channel"),
        "target_host_profile": provenance.get("target_host_profile"),
        "target_authority": provenance.get("target_authority"),
        "deployment_fingerprint_id": deployment_fingerprint.get("deployment_fingerprint_id") or provenance.get("deployment_fingerprint_id"),
        "rollback_evidence": provenance.get("rollback_evidence") or candidate.get("rollback_plan"),
    }
    missing: list[str] = []
    for key, value in values.items():
        if value is None:
            missing.append(key)
        elif isinstance(value, str) and not value.strip():
            missing.append(key)
        elif isinstance(value, (list, dict, tuple, set)) and not value:
            missing.append(key)
    return missing


def supply_missing_promotion_readiness_inputs(
    workspace: Path,
    candidate_id: str,
    now: datetime | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    """Derive objective readiness inputs or write a stronger blocker without accepting."""
    if not candidate_id or not _CANDIDATE_ID_RE.fullmatch(candidate_id) or candidate_id.startswith("."):
        raise ValueError("candidate_id is invalid")
    promotions_dir = (state_root if state_root is not None else workspace / "state") / "promotions"
    candidate_path = promotions_dir / f"{candidate_id}.json"
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)
    candidate = _read_json(candidate_path)
    if candidate.get("decision_record") != "blocked_not_ready" or candidate.get("accepted_record") != "not_created_not_ready":
        raise ValueError("promotion candidate is not a blocked not-ready readiness packet")

    completed_at = _utc_iso(now)
    artifact_path = candidate.get("artifact_path")
    artifact_present = bool(artifact_path and Path(str(artifact_path)).exists())
    evidence_refs = candidate.get("evidence_refs") if isinstance(candidate.get("evidence_refs"), list) else []
    missing_inputs = _missing_provenance_inputs(candidate)
    readiness_checks = {
        "schema_version": "promotion-readiness-inputs-v1",
        "artifact_present": artifact_present,
        "evidence_refs_present": bool(evidence_refs),
        "provenance_complete": not missing_inputs,
        "missing_inputs": missing_inputs,
    }
    readiness_reasons = [f"{field}_missing" for field in missing_inputs]
    if not artifact_present:
        readiness_reasons.append("artifact_missing")
    if not evidence_refs:
        readiness_reasons.append("evidence_refs_missing")

    recommended_next_action = "ready_for_policy_review" if not readiness_reasons else f"supply_{readiness_reasons[0].removesuffix('_missing')}_or_policy_override"
    blocker = {
        "schema_version": READINESS_INPUTS_BLOCKER_VERSION,
        "state": "blocked" if readiness_reasons else "ready_for_policy_review",
        "reason": "promotion_readiness_inputs_missing" if readiness_reasons else "promotion_readiness_inputs_supplied",
        "promotion_candidate_id": candidate_id,
        "missing_inputs": missing_inputs + (["artifact"] if not artifact_present else []) + (["evidence_refs"] if not evidence_refs else []),
        "readiness_checks": readiness_checks,
        "readiness_reasons": readiness_reasons,
        "recommended_next_action": recommended_next_action,
        "completed_at_utc": completed_at,
    }

    packet_path = promotions_dir / "readiness_packets" / f"{candidate_id}.json"
    packet = _read_json(packet_path) if packet_path.exists() else {}
    packet_updated = {
        **packet,
        "schema_version": READINESS_PACKET_VERSION,
        "promotion_candidate_id": candidate_id,
        "state": blocker["state"],
        "reason": blocker["reason"],
        "readiness_checks": readiness_checks,
        "readiness_reasons": readiness_reasons,
        "readiness_blocker": blocker,
        "recommended_next_action": recommended_next_action,
        "readiness_inputs_completed_at_utc": completed_at,
    }
    governance = candidate.get("governance_packet") if isinstance(candidate.get("governance_packet"), dict) else {}
    updated = {
        **candidate,
        "review_status": "not_ready_for_policy_review" if readiness_reasons else "ready_for_policy_review",
        "decision": "not_ready_for_policy_review" if readiness_reasons else "ready_for_policy_review",
        "decision_record": "blocked_not_ready",
        "accepted_record": "not_created_not_ready",
        "readiness_packet_path": str(packet_path),
        "readiness_checks": readiness_checks,
        "readiness_reasons": readiness_reasons,
        "readiness_blocker": blocker,
        "recommended_next_action": recommended_next_action,
        "governance_packet": {
            **governance,
            "review_packet_status": "blocked_not_ready" if readiness_reasons else "pending_operator_review",
            "readiness_packet_path": str(packet_path),
            "readiness_blocker": blocker,
        },
    }
    _write_json(packet_path, packet_updated)
    _write_json(candidate_path, updated)
    _write_json(promotions_dir / "latest.json", {**updated, "candidate_path": str(candidate_path)})
    return blocker


def review_promotion_candidate(
    workspace: Path,
    candidate_id: str,
    decision: str,
    decision_reason: str,
    now: datetime | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"unsupported decision '{decision}'")
    if not decision_reason.strip():
        raise ValueError("decision_reason is required")
    if not candidate_id or not _CANDIDATE_ID_RE.fullmatch(candidate_id) or candidate_id.startswith("."):
        raise ValueError("candidate_id is invalid")

    promotions_dir = (state_root if state_root is not None else workspace / "state") / "promotions"
    candidate_path = promotions_dir / f"{candidate_id}.json"
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)

    candidate = _read_json(candidate_path)
    reviewed_at = _utc_iso(now)
    updated = {
        **candidate,
        "review_status": "reviewed",
        "decision": decision,
        "decision_reason": decision_reason,
        "reviewed_at_utc": reviewed_at,
    }
    _write_json(candidate_path, updated)

    decision_record = {
        "schema_version": PROMOTION_RECORD_VERSION,
        "promotion_candidate_id": candidate_id,
        "origin_cycle_id": updated.get("origin_cycle_id"),
        "decision": decision,
        "decision_reason": decision_reason,
        "review_status": "reviewed",
        "reviewed_at_utc": reviewed_at,
        "candidate_path": str(candidate_path),
    }
    _write_json(promotions_dir / "decisions" / f"{candidate_id}.json", decision_record)

    latest_payload = {
        **updated,
        "candidate_path": str(candidate_path),
    }
    _write_json(promotions_dir / "latest.json", latest_payload)

    if decision == "accept":
        patch_bundle = {
            "schema_version": PATCH_BUNDLE_VERSION,
            "promotion_candidate_id": candidate_id,
            "origin_cycle_id": updated.get("origin_cycle_id"),
            "target_repo": updated.get("target_repo"),
            "target_branch": updated.get("target_branch") or "promote/self-evolving",
            "source_paths": updated.get("source_paths") or [],
            "evidence_refs": updated.get("evidence_refs") or [],
            "review_status": "reviewed",
            "decision": "accept",
            "decision_reason": decision_reason,
            "generated_at_utc": reviewed_at,
        }
        patch_bundle_path = promotions_dir / "patches" / f"{candidate_id}.json"
        _write_json(patch_bundle_path, patch_bundle)
        accepted_record = {
            **updated,
            "schema_version": PROMOTION_RECORD_VERSION,
            "accepted_at_utc": reviewed_at,
            "accepted_branch": updated.get("target_branch") or "promote/self-evolving",
            "patch_bundle_path": str(patch_bundle_path),
        }
        _write_json(promotions_dir / "accepted" / f"{candidate_id}.json", accepted_record)

    return latest_payload
