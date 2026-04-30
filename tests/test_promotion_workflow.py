import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.runtime.coordinator import run_self_evolving_cycle
from nanobot.runtime.state import load_runtime_state
from nanobot.runtime.promotion import complete_promotion_readiness_packet, review_promotion_candidate


def _read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _create_pass_candidate(tmp_path: Path) -> tuple[dict, str]:
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 60}),
        encoding="utf-8",
    )

    result = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks="prepare candidate",
            execute_turn=AsyncMock(return_value="bounded work complete"),
            now=expires_at - timedelta(minutes=15),
        )
    )
    assert "PASS" in result
    runtime = load_runtime_state(tmp_path)
    candidate_id = runtime["promotion_candidate_id"]
    assert candidate_id
    candidate = _read_json(tmp_path / "state" / "promotions" / f"{candidate_id}.json")
    return candidate, candidate_id


def _write_promotion_candidate(
    tmp_path: Path,
    candidate_id: str,
    provenance: dict,
) -> Path:
    promotions_dir = tmp_path / "state" / "promotions"
    promotions_dir.mkdir(parents=True)
    candidate_path = promotions_dir / f"{candidate_id}.json"
    payload = {
        "schema_version": "promotion-record-v1",
        "promotion_candidate_id": candidate_id,
        "origin_cycle_id": f"cycle-{candidate_id}",
        "candidate_created_utc": "2026-04-18T11:45:00Z",
        "origin_host": "local-workspace",
        "source_paths": ["state/reports/evolution-cycle.json"],
        "target_repo": "ozand/nanobot",
        "target_branch": "promote/self-evolving",
        "review_status": "ready_for_policy_review",
        "decision": "accept",
        "decision_reason": "ready to validate provenance gate",
        "artifact_path": str(promotions_dir / f"{candidate_id}.artifact.json"),
        "decision_record": "pending_operator_review_packet",
        "accepted_record": None,
        "promotion_provenance": provenance,
        "governance_packet": {
            "review_packet_status": "pending_operator_review",
            "review_status": "ready_for_policy_review",
            "decision": "accept",
            "source_artifact": str(promotions_dir / f"{candidate_id}.artifact.json"),
            "promotion_provenance": provenance,
        },
    }
    candidate_path.write_text(json.dumps(payload), encoding="utf-8")
    return candidate_path


def test_accept_review_writes_decision_trail_and_accepted_record(tmp_path):
    candidate, candidate_id = _create_pass_candidate(tmp_path)

    result = review_promotion_candidate(
        workspace=tmp_path,
        candidate_id=candidate_id,
        decision="accept",
        decision_reason="validated and ready for reviewable branch",
    )

    assert result["decision"] == "accept"
    assert result["review_status"] == "reviewed"

    updated = _read_json(tmp_path / "state" / "promotions" / f"{candidate_id}.json")
    assert updated["decision"] == "accept"
    assert updated["decision_reason"] == "validated and ready for reviewable branch"
    assert updated["review_status"] == "reviewed"
    assert updated["reviewed_at_utc"].endswith("Z")

    latest = _read_json(tmp_path / "state" / "promotions" / "latest.json")
    assert latest["promotion_candidate_id"] == candidate_id
    assert latest["decision"] == "accept"
    runtime = load_runtime_state(tmp_path)
    assert runtime["promotion_replay_readiness"]["state"] == "ready"
    assert runtime["promotion_provenance"]["status"] == "ready"
    assert runtime["promotion_provenance"]["source_commit"]
    assert runtime["governance_coverage"]["state"] == "healthy"
    assert latest["schema_version"] == "promotion-record-v1"

    decision_record = _read_json(tmp_path / "state" / "promotions" / "decisions" / f"{candidate_id}.json")
    assert decision_record["schema_version"] == "promotion-record-v1"
    assert decision_record["promotion_candidate_id"] == candidate_id
    assert decision_record["decision"] == "accept"
    assert decision_record["decision_reason"] == "validated and ready for reviewable branch"

    accepted_record = _read_json(tmp_path / "state" / "promotions" / "accepted" / f"{candidate_id}.json")
    assert accepted_record["schema_version"] == "promotion-record-v1"
    assert accepted_record["promotion_candidate_id"] == candidate_id
    assert accepted_record["decision"] == "accept"
    assert accepted_record["target_branch"] == candidate["target_branch"]
    assert accepted_record["patch_bundle_path"].endswith(f"{candidate_id}.json")
    patch_bundle = _read_json(accepted_record["patch_bundle_path"])
    assert patch_bundle["promotion_candidate_id"] == candidate_id
    assert patch_bundle["target_branch"] == candidate["target_branch"]
    assert patch_bundle["evidence_refs"] == candidate["evidence_refs"]


def test_accept_review_supports_custom_state_root(tmp_path):
    state_root = tmp_path / "custom-state"
    candidate_id = "custom-root-candidate"
    provenance = {"status": "ready", "source_commit": "abc123"}
    promotions_dir = state_root / "promotions"
    promotions_dir.mkdir(parents=True)
    (promotions_dir / f"{candidate_id}.json").write_text(json.dumps({
        "schema_version": "promotion-record-v1",
        "promotion_candidate_id": candidate_id,
        "origin_cycle_id": "cycle-custom-root",
        "source_paths": ["state/reports/evolution-cycle.json"],
        "target_repo": "ozand/nanobot",
        "target_branch": "promote/self-evolving",
        "review_status": "ready_for_policy_review",
        "decision": "accept",
        "artifact_path": str(promotions_dir / f"{candidate_id}.artifact.json"),
        "promotion_provenance": provenance,
    }), encoding="utf-8")

    result = review_promotion_candidate(
        workspace=tmp_path,
        state_root=state_root,
        candidate_id=candidate_id,
        decision="accept",
        decision_reason="validated in custom state root",
    )

    assert result["decision"] == "accept"
    assert (state_root / "promotions" / "decisions" / f"{candidate_id}.json").exists()
    assert (state_root / "promotions" / "accepted" / f"{candidate_id}.json").exists()
    assert not (tmp_path / "state" / "promotions" / f"{candidate_id}.json").exists()


def test_placeholder_provenance_blocks_promotion_readiness(tmp_path):
    candidate_id = "promotion-placeholder"
    provenance = {
        "source_commit": "unknown",
        "build_recipe_hash": "local-build",
        "artifact_id": "unknown",
        "artifact_version": "unknown",
        "release_channel": "unknown",
        "target_host_profile": "unknown",
        "target_authority": "unknown",
        "deployment_fingerprint": {"deployment_fingerprint_id": "unknown"},
        "rollback_evidence": [],
    }
    _write_promotion_candidate(tmp_path, candidate_id, provenance)

    review_promotion_candidate(
        workspace=tmp_path,
        candidate_id=candidate_id,
        decision="accept",
        decision_reason="accepted for readiness gating check",
    )

    runtime = load_runtime_state(tmp_path)
    assert runtime["promotion_candidate_id"] == candidate_id
    assert runtime["promotion_provenance"]["status"] == "blocked"
    assert runtime["promotion_replay_readiness"]["state"] == "blocked"
    assert "missing_or_placeholder_provenance" in runtime["promotion_replay_readiness"]["reason"]


def test_valid_provenance_surfaces_in_runtime_and_promotion_record(tmp_path):
    candidate_id = "promotion-provenance"
    provenance = {
        "source_commit": "abc123def456",
        "build_recipe_hash": "recipe-hash-123",
        "artifact_id": "artifact-175",
        "artifact_version": "1.0.0",
        "release_channel": "stable",
        "target_host_profile": "weak-host",
        "target_authority": "runtime-promotion-policy",
        "deployment_fingerprint": {
            "deployment_fingerprint_id": "dfp-175",
            "artifact_id": "artifact-175",
            "artifact_version": "1.0.0",
            "release_channel": "stable",
            "target_host_profile": "weak-host",
            "target_authority": "runtime-promotion-policy",
        },
        "rollback_evidence": {"evidence_refs": ["rollback-evidence-1"]},
    }
    _write_promotion_candidate(tmp_path, candidate_id, provenance)

    review_promotion_candidate(
        workspace=tmp_path,
        candidate_id=candidate_id,
        decision="accept",
        decision_reason="validated provenance fields",
    )

    runtime = load_runtime_state(tmp_path)
    assert runtime["promotion_candidate_id"] == candidate_id
    assert runtime["promotion_provenance"]["status"] == "ready"
    assert runtime["promotion_provenance"]["source_commit"] == "abc123def456"
    assert runtime["promotion_provenance"]["deployment_fingerprint"]["deployment_fingerprint_id"] == "dfp-175"
    assert runtime["promotion_replay_readiness"]["state"] == "ready"
    assert runtime["promotion_summary"] == f"{candidate_id} | reviewed | accept"
    latest = _read_json(tmp_path / "state" / "promotions" / "latest.json")
    assert latest["promotion_provenance"]["artifact_id"] == "artifact-175"


def test_reject_review_writes_decision_trail_without_accepted_record(tmp_path):
    _candidate, candidate_id = _create_pass_candidate(tmp_path)

    result = review_promotion_candidate(
        workspace=tmp_path,
        candidate_id=candidate_id,
        decision="reject",
        decision_reason="weak-host cost too high",
    )

    assert result["decision"] == "reject"
    assert result["review_status"] == "reviewed"

    updated = _read_json(tmp_path / "state" / "promotions" / f"{candidate_id}.json")
    assert updated["decision"] == "reject"
    assert updated["decision_reason"] == "weak-host cost too high"

    decision_record = _read_json(tmp_path / "state" / "promotions" / "decisions" / f"{candidate_id}.json")
    assert decision_record["decision"] == "reject"
    assert decision_record["decision_reason"] == "weak-host cost too high"
    assert not (tmp_path / "state" / "promotions" / "accepted" / f"{candidate_id}.json").exists()


def test_needs_more_evidence_requires_reason_and_updates_runtime_state(tmp_path):
    _candidate, candidate_id = _create_pass_candidate(tmp_path)

    with pytest.raises(ValueError, match="decision_reason"):
        review_promotion_candidate(
            workspace=tmp_path,
            candidate_id=candidate_id,
            decision="needs_more_evidence",
            decision_reason="",
        )

    result = review_promotion_candidate(
        workspace=tmp_path,
        candidate_id=candidate_id,
        decision="needs_more_evidence",
        decision_reason="missing replayable patch bundle",
    )

    assert result["decision"] == "needs_more_evidence"
    runtime = load_runtime_state(tmp_path)
    assert runtime["promotion_candidate_id"] == candidate_id
    assert runtime["review_status"] == "reviewed"
    assert runtime["decision"] == "needs_more_evidence"
    assert runtime["decision_reason"] == "missing replayable patch bundle"
    assert runtime["promotion_replay_readiness"]["state"] == "blocked"
    assert runtime["governance_coverage"]["state"] == "action_required"


def test_review_unknown_candidate_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        review_promotion_candidate(
            workspace=tmp_path,
            candidate_id="promotion-missing",
            decision="reject",
            decision_reason="not found",
        )


def test_review_rejects_unsafe_candidate_id(tmp_path):
    promotions_dir = tmp_path / "state" / "promotions"
    promotions_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match="candidate_id"):
        review_promotion_candidate(
            workspace=tmp_path,
            candidate_id="../goals/active",
            decision="reject",
            decision_reason="unsafe path",
        )


def test_complete_promotion_readiness_packet_writes_review_packet_without_accepting(tmp_path):
    candidate_id = "promotion-not-ready-packet"
    promotions_dir = tmp_path / "state" / "promotions"
    promotions_dir.mkdir(parents=True)
    artifact_path = tmp_path / "state" / "improvements" / "materialized-cycle-packet.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(json.dumps({"cycle_id": "cycle-packet"}), encoding="utf-8")
    candidate_path = promotions_dir / f"{candidate_id}.json"
    candidate_path.write_text(json.dumps({
        "schema_version": "promotion-record-v1",
        "promotion_candidate_id": candidate_id,
        "origin_cycle_id": "cycle-packet",
        "review_status": "not_ready_for_policy_review",
        "decision": "not_ready_for_policy_review",
        "decision_record": None,
        "accepted_record": None,
        "artifact_path": str(artifact_path),
        "readiness_checks": {"definition_of_ready": "missing"},
        "readiness_reasons": ["definition_of_ready_missing"],
        "governance_packet": {"review_packet_status": "not_ready"},
    }), encoding="utf-8")

    result = complete_promotion_readiness_packet(workspace=tmp_path, candidate_id=candidate_id)

    assert result["state"] == "blocked"
    assert result["reason"] == "promotion_candidate_not_ready_for_policy_review"
    assert result["recommended_next_action"] == "supply_missing_promotion_readiness_inputs"
    packet_path = Path(result["readiness_packet_path"])
    assert packet_path.exists()
    packet = _read_json(packet_path)
    assert packet["schema_version"] == "promotion-readiness-packet-v1"
    assert packet["promotion_candidate_id"] == candidate_id
    assert packet["decision"] == "not_ready_for_policy_review"
    assert packet["decision_record"] == "blocked_not_ready"
    assert packet["accepted_record"] == "not_created_not_ready"
    assert packet["missing_records"] == ["decision_record", "accepted_record"]
    assert packet["readiness_reasons"] == ["definition_of_ready_missing"]
    updated = _read_json(candidate_path)
    assert updated["review_status"] == "not_ready_for_policy_review"
    assert updated["decision"] == "not_ready_for_policy_review"
    assert updated["decision_record"] == "blocked_not_ready"
    assert updated["accepted_record"] == "not_created_not_ready"
    assert updated["readiness_packet_path"] == str(packet_path)
    assert not (promotions_dir / "accepted" / f"{candidate_id}.json").exists()
    runtime = load_runtime_state(tmp_path)
    assert runtime["promotion_replay_readiness"]["state"] == "blocked"
    assert runtime["promotion_replay_readiness"]["review_packet_status"] == "blocked_not_ready"
    assert runtime["promotion_replay_readiness"]["reason"] == "promotion_candidate_not_ready_for_policy_review"
    assert runtime["promotion_replay_readiness"]["recommended_next_action"] == "supply_missing_promotion_readiness_inputs"
