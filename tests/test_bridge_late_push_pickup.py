"""Tests for #1709 increment 2: bridge._finish_pending_pushes.

At the same safe cycle-start boundary as _pickup_staged_promotions (bridge
lock held, HEAD on clean main), a 'push_pending' terminal cycle (increment 1:
a gate-passed cycle whose final push exhausted its transient-error retries)
gets resolved: 'pushed_late' if origin/main is unchanged and the branch still
exists, 'superseded' if origin/main moved, 'abandoned' if the branch is gone.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import bridge
from nanobot.runtime import demand
from nanobot.runtime import cycle_ledger
from tests.test_bridge_cycle_branch import (
    _commit_file,
    _init_repo,
    _origin_main_sha,
    _run,
)


def _read_ledger_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stage_push_pending(
    tmp_path: Path, work: Path, cycle_id: str, files_changed: list[str] | None = None,
) -> dict:
    """Set up a cycle branch and a push_pending row matching the real recorder."""
    state_dir = tmp_path / "state"
    setup = bridge._setup_cycle_branch(work, cycle_id)
    assert setup["ok"]
    changed_paths = ["feature.py"] if files_changed is None else files_changed
    for index, path in enumerate(changed_paths):
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        _commit_file(work, path, f"# test content {index}\n", f"feat: change {path}")
    _run(work, "checkout", "main")  # boundary precondition: HEAD on clean main
    cycle_ledger.record_cycle_outcome(
        state_dir, cycle_id, "push_pending", "push_pending", changed_paths, setup["branch"],
        main_sha_before=setup["main_sha"],
    )
    return {"state_dir": state_dir, "branch": setup["branch"], "main_sha": setup["main_sha"]}


class TestFinishPendingPushes:
    def test_late_push_carries_service_only_paths_and_does_not_complete_demand(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        service = ["diary/2026-09-25.md", "memory/MEMORY.md"]
        staged = _stage_push_pending(tmp_path, work, "cid-service-late", service)

        assert bridge._finish_pending_pushes(work, staged["state_dir"]) == 1
        rows = _read_ledger_rows(staged["state_dir"])
        pushed = [r for r in rows if r.get("phase") == "outcome" and r.get("cycle_id") == "cid-service-late"][-1]
        assert pushed["files_changed"] == service
        assert pushed["delivered"] is False
        assert pushed["delivery_state"] == "known"
        assert pushed["outcome"] == "partial"
        assert pushed["reason"] == "service_only"
        assert (pushed["verdict"], pushed["verdict_reason"]) == ("inconclusive", "service_only")
        from nanobot.runtime import goal_gap_futility
        from datetime import datetime, timezone
        rows_for_futility = []
        for index in range(5):
            cycle = f"failed-{index}"
            ts = f"2026-01-01T00:00:0{index}Z"
            rows_for_futility.extend([
                {"phase": "proposed", "cycle_id": cycle, "demand_id": "goal-service-late", "ts": ts},
                {"phase": "outcome", "cycle_id": cycle, "outcome": "validation_failed", "ts": ts},
            ])
        rows_for_futility.extend([
            {"phase": "proposed", "cycle_id": "cid-service-late", "demand_id": "goal-service-late", "ts": "2026-01-01T00:00:06Z"},
            {**pushed, "ts": "2026-01-01T00:00:06Z"},
            {"phase": "proposed", "cycle_id": "failed-after", "demand_id": "goal-service-late", "ts": "2026-01-01T00:00:07Z"},
            {"phase": "outcome", "cycle_id": "failed-after", "outcome": "validation_failed", "ts": "2026-01-01T00:00:07Z"},
        ])
        assert goal_gap_futility._demand_attempt_count(
            rows_for_futility, "goal-service-late", datetime(2025, 1, 1, tzinfo=timezone.utc),
        ) == 7
        from nanobot.runtime import llm_proposer
        proposed_rows = [
            {"phase": "proposed", "cycle_id": "cid-service-late", "task_title": "service-only regression title"},
        ]
        pushed_for_recent_failures = {**pushed, "outcome": "partial"}
        assert llm_proposer._recent_failed_titles([*proposed_rows, pushed_for_recent_failures]) == [
            "service-only regression title",
        ]

        (staged["state_dir"] / "demand").mkdir(parents=True, exist_ok=True)
        cycle_ledger.append_event(staged["state_dir"], {
            "phase": "proposed", "cycle_id": "cid-service-late", "demand_id": "priority-service-late",
        })
        assert demand._fold_completed(staged["state_dir"]) == set()

        # A second recovery boundary must recognize partial/service_only as
        # this cycle's resolution, not append a contradictory abandoned row.
        assert bridge._finish_pending_pushes(work, staged["state_dir"]) == 0
        repeated_rows = _read_ledger_rows(staged["state_dir"])
        resolutions = [
            row for row in repeated_rows
            if row.get("phase") == "outcome" and row.get("cycle_id") == "cid-service-late"
            and row.get("outcome") != "push_pending"
        ]
        assert len(resolutions) == 1
        assert resolutions[0]["outcome"] == "partial"

    def test_late_push_with_old_empty_file_list_is_unknown_and_not_folded(self, tmp_path):
        from nanobot.runtime import demand
        _, work = _init_repo(tmp_path)
        staged = _stage_push_pending(tmp_path, work, "cid-legacy-empty", [])
        assert bridge._finish_pending_pushes(work, staged["state_dir"]) == 1
        rows = _read_ledger_rows(staged["state_dir"])
        pushed = [r for r in rows if r.get("phase") == "outcome" and r.get("cycle_id") == "cid-legacy-empty"][-1]
        assert pushed["files_changed"] == []
        assert pushed["outcome"] == "pushed_late"
        assert pushed["reason"] == "delivery_unknown"
        assert pushed["delivered"] is False
        assert pushed["delivery_state"] == "unknown"
        (staged["state_dir"] / "demand").mkdir(parents=True, exist_ok=True)
        cycle_ledger.append_event(staged["state_dir"], {
            "phase": "proposed", "cycle_id": "cid-legacy-empty", "demand_id": "priority-legacy-empty",
        })
        assert demand._fold_completed(staged["state_dir"]) == set()
        completed_path = staged["state_dir"] / "demand" / "completed.json"
        if completed_path.exists():
            completed = json.loads(completed_path.read_text(encoding="utf-8"))
            assert "priority-legacy-empty" not in completed.get("entries", {})

    def test_unchanged_main_pushes_late(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        staged = _stage_push_pending(tmp_path, work, "cid-late")

        resolved = bridge._finish_pending_pushes(work, staged["state_dir"])

        assert resolved == 1
        rows = _read_ledger_rows(staged["state_dir"])
        outcome_rows = [r for r in rows if r.get("phase") == "outcome" and r.get("cycle_id") == "cid-late"]
        assert [r["outcome"] for r in outcome_rows] == ["push_pending", "pushed_late"]
        assert _origin_main_sha(origin) != staged["main_sha"]
        # branch cleaned up after a successful late push, same as a normal integration.
        branches = _run(work, "branch", "--list", staged["branch"]).stdout
        assert staged["branch"] not in branches

    def test_moved_main_records_superseded_and_keeps_branch(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        staged = _stage_push_pending(tmp_path, work, "cid-superseded")

        # Simulate another cycle integrating meanwhile: origin/main advances
        # past the recorded main_sha_before.
        _commit_file(work, "other.py", "x = 1\n", "feat: unrelated change")
        _run(work, "push", "origin", "main")

        resolved = bridge._finish_pending_pushes(work, staged["state_dir"])

        assert resolved == 1
        rows = _read_ledger_rows(staged["state_dir"])
        outcome_rows = [r for r in rows if r.get("phase") == "outcome" and r.get("cycle_id") == "cid-superseded"]
        assert [r["outcome"] for r in outcome_rows] == ["push_pending", "superseded"]
        # branch kept for forensics — never merged/rebased/force-pushed.
        branches = _run(work, "branch", "--list", staged["branch"]).stdout
        assert staged["branch"] in branches

    def test_missing_branch_records_abandoned(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        staged = _stage_push_pending(tmp_path, work, "cid-abandoned")
        _run(work, "branch", "-D", staged["branch"])

        resolved = bridge._finish_pending_pushes(work, staged["state_dir"])

        assert resolved == 1
        rows = _read_ledger_rows(staged["state_dir"])
        outcome_rows = [r for r in rows if r.get("phase") == "outcome" and r.get("cycle_id") == "cid-abandoned"]
        assert [r["outcome"] for r in outcome_rows] == ["push_pending", "abandoned"]

    def test_second_call_does_not_duplicate_resolution(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        staged = _stage_push_pending(tmp_path, work, "cid-idempotent")

        first = bridge._finish_pending_pushes(work, staged["state_dir"])
        second = bridge._finish_pending_pushes(work, staged["state_dir"])

        assert first == 1
        assert second == 0
        rows = _read_ledger_rows(staged["state_dir"])
        outcome_rows = [r for r in rows if r.get("phase") == "outcome" and r.get("cycle_id") == "cid-idempotent"]
        assert [r["outcome"] for r in outcome_rows] == ["push_pending", "pushed_late"]

    def test_no_pending_rows_is_a_cheap_noop(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"
        assert bridge._finish_pending_pushes(work, state_dir) == 0
