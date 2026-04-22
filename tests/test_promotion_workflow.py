import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.runtime.coordinator import run_self_evolving_cycle
from nanobot.runtime.state import load_runtime_state
from nanobot.runtime.promotion import review_promotion_candidate


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
