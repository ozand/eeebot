"""Issue #713: novelty pressure — inject recent-activity context into the
subagent proposal prompt so it doesn't re-propose/re-implement recently
completed or recently rejected work.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.bridge import _recent_activity_context, build_task
from tests.test_goal_backlog_routing import _make_git_repo_with_commit


def test_recent_activity_includes_recent_commits(tmp_path: Path):
    repo = _make_git_repo_with_commit(tmp_path, "feat: add novelty pressure to bridge prompt")

    ctx = _recent_activity_context(state_dir=None, selfevo_repo_root=repo)

    assert "## Recent activity (do not repeat)" in ctx
    assert "add novelty pressure to bridge prompt" in ctx


def test_recent_activity_includes_rejected_results(tmp_path: Path):
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "r1.json").write_text(
        json.dumps({
            "backlog_title": "flaky rollback candidate",
            "rollback": {"reason": "mutation_surface_violation"},
        }),
        encoding="utf-8",
    )

    ctx = _recent_activity_context(state_dir=state_dir, selfevo_repo_root=None)

    assert "Recently rejected" in ctx
    assert "flaky rollback candidate" in ctx
    assert "mutation_surface_violation" in ctx


def test_build_task_has_anti_duplicate_instruction():
    req = {"task_title": "some task", "request_id": "r1", "cycle_id": "c1", "goal_id": "g1"}
    task = build_task(req, "mission text", "report_source.json")

    assert "if this task is already done, do NOT re-implement it" in task
    assert "report outcome: skipped" in task


def test_recent_activity_fail_open(tmp_path: Path):
    missing_repo = tmp_path / "does-not-exist"
    missing_state = tmp_path / "also-missing"

    ctx = _recent_activity_context(state_dir=missing_state, selfevo_repo_root=missing_repo)

    assert ctx == ""
