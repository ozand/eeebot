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


def _stage_push_pending(tmp_path: Path, work: Path, cycle_id: str) -> dict:
    """Set up a cycle branch with one commit, return HEAD to clean main (the
    boundary precondition), and write the 'push_pending' ledger row that
    _integrate_cycle_to_main would have written on exhausted retries."""
    state_dir = tmp_path / "state"
    setup = bridge._setup_cycle_branch(work, cycle_id)
    assert setup["ok"]
    _commit_file(work, "feature.py", "def feature():\n    return 42\n", "feat: add feature")
    _run(work, "checkout", "main")  # boundary precondition: HEAD on clean main
    cycle_ledger.record_cycle_outcome(
        state_dir, cycle_id, "push_pending", "push_pending", [], setup["branch"],
        main_sha_before=setup["main_sha"],
    )
    return {"state_dir": state_dir, "branch": setup["branch"], "main_sha": setup["main_sha"]}


class TestFinishPendingPushes:
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

    def test_pending_open_increment_cleared_on_late_push_success(self, tmp_path):
        """N3 (round 2 external re-check, architect resolution
        2026-09-26): a late push is a genuine integration success --
        exactly like the immediate ``_integrate_cycle_to_main`` path,
        it must clear a pending open increment for the SAME cycle_id.
        Without this, a kept increment that finishes via late push
        still reads as pending forever, and the next ``keep`` fails
        with ``resume_branch_missing`` although the work already
        integrated.
        """
        from nanobot.runtime import open_increment

        origin, work = _init_repo(tmp_path)
        staged = _stage_push_pending(tmp_path, work, "cid-late-pending")

        open_increment.record_supply_interruption(
            staged["state_dir"], "cid-late-pending",
            retry_key="n3-late-push-test", plan_text="finish the kept increment",
            candidate_id=None, selfevo_repo=work, branch=staged["branch"],
        )
        assert open_increment.pending_open_increment(staged["state_dir"]) is not None

        resolved = bridge._finish_pending_pushes(work, staged["state_dir"])
        assert resolved == 1

        assert open_increment.pending_open_increment(staged["state_dir"]) is None, (
            "a successful late push must clear the pending open increment for this cycle"
        )
