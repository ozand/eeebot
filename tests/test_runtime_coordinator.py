import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from nanobot.runtime.state import load_runtime_state
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

    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "BLOCK"
    assert report["goal_id"] == "goal-bootstrap"
    assert report["approval_gate"]["state"] == "missing"
    assert report["summary"] == summary

    outbox = _read_json(tmp_path / "state" / "outbox" / "latest.json")
    assert outbox["approval_gate"]["state"] == "missing"
    assert outbox["next_hint"] == "approval gate missing; refresh manually"
    assert outbox["latest_report"]["result_status"] == "BLOCK"

    report_index = _read_json(tmp_path / "state" / "outbox" / "report.index.json")
    assert report_index["status"] == "BLOCK"
    assert report_index["source"] == runtime["report_path"]
    assert report_index["goal"]["goal_id"] == "goal-bootstrap"
    assert report_index["goal"]["follow_through"]["artifact_paths"] == []
    assert report_index["capability_gate"]["approval"]["state"] == "missing"

    goal = _read_json(tmp_path / "state" / "goals" / "active.json")
    assert goal["active_goal"] == "goal-bootstrap"


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

    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "PASS"
    assert report["goal_id"] == "goal-123"
    assert report["approval_gate"]["state"] == "fresh"
    assert report["execution_response"] == "agent completed bounded work"
    assert report["bounded_apply"] == "on"
    assert report["promotion_execute"] == "on"
    assert report["promotion_candidate_id"].startswith("promotion-")
    assert report["review_status"] == "pending"
    assert report["decision"] == "pending"

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
    assert report_index["goal"]["follow_through"]["artifact_paths"] == []
    assert report_index["capability_gate"]["approval"]["state"] == "fresh"
    assert report_index["promotion"]["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert report_index["promotion"]["candidate_path"].endswith(f"{report['promotion_candidate_id']}.json")
    assert report_index["promotion"]["review_status"] == "pending"
    assert report_index["promotion"]["decision"] == "pending"

    promotions_latest = _read_json(tmp_path / "state" / "promotions" / "latest.json")
    assert promotions_latest["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert promotions_latest["origin_cycle_id"] == report["cycle_id"]
    candidate_path = tmp_path / "state" / "promotions" / f"{report['promotion_candidate_id']}.json"
    candidate = _read_json(candidate_path)
    assert candidate["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert candidate["origin_cycle_id"] == report["cycle_id"]
    assert candidate["target_branch"] == "promote/self-evolving"
    assert candidate["evidence_refs"] == [report["evidence_ref_id"]]


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
