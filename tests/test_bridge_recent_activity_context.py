"""Issue #713: novelty pressure — inject recent-activity context into the
subagent proposal prompt so it doesn't re-propose/re-implement recently
completed or recently rejected work.
"""
from __future__ import annotations

import json
import os
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


def test_build_task_includes_origin_report_line_when_source_nonempty():
    """#913: report_source is now optional, but a non-empty value keeps
    today's exact prompt line unchanged."""
    req = {"task_title": "some task", "request_id": "r1", "cycle_id": "c1", "goal_id": "g1"}
    task = build_task(req, "mission text", "report_source.json")

    assert "Origin report: report_source.json" in task


def test_build_task_omits_origin_report_line_when_source_empty():
    """#913: an empty report_source (fresh install / registry-only bootstrap,
    no outbox/) must omit the "Origin report:" line entirely rather than
    printing it empty."""
    req = {"task_title": "some task", "request_id": "r1", "cycle_id": "c1", "goal_id": "g1"}
    task = build_task(req, "mission text", "")

    assert "Origin report:" not in task


def test_recent_activity_fail_open(tmp_path: Path):
    missing_repo = tmp_path / "does-not-exist"
    missing_state = tmp_path / "also-missing"

    ctx = _recent_activity_context(state_dir=missing_state, selfevo_repo_root=missing_repo)

    assert ctx == ""


def test_results_scandir_single_pass(tmp_path: Path, monkeypatch):
    """#1040: _iter_result_entries performs a single os.scandir pass across result entries."""
    from nanobot.runtime import bridge

    bridge._clear_result_entries_cache()
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    results_dir.mkdir(parents=True)
    for i in range(5):
        (results_dir / f"r{i}.json").write_text(
            json.dumps({
                "request_id": f"req_{i}",
                "materialized_from": "bridge_llm_execution",
                "backlog_title": f"Task {i}",
                "cycle_id": f"c{i}",
            }),
            encoding="utf-8",
        )

    orig_scandir = os.scandir
    scandir_calls = 0

    def counting_scandir(path):
        nonlocal scandir_calls
        if str(path) == str(results_dir):
            scandir_calls += 1
        return orig_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    entries = bridge._iter_result_entries(results_dir)
    assert len(entries) == 5
    assert scandir_calls == 1

    # find_pending_request uses the single-pass helper and reuses cached results
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagents" / "bridge")
    (state_dir / "subagents" / "requests").mkdir(parents=True)
    (state_dir / "subagents" / "requests" / "req_99.json").write_text(
        json.dumps({"request_id": "req_99"}),
        encoding="utf-8",
    )
    p, d = bridge.find_pending_request()
    assert p is not None
    # No extra scandir was made on results_dir
    assert scandir_calls == 1


def test_bridge_composition_single_scandir_across_all_consumers(tmp_path: Path, monkeypatch):
    """#1040: All bridge results consumers reuse the cached/single scan pass."""
    from nanobot.runtime import bridge

    bridge._clear_result_entries_cache()
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    results_dir.mkdir(parents=True)
    for i in range(3):
        (results_dir / f"r{i}.json").write_text(
            json.dumps({
                "request_id": f"req_{i}",
                "materialized_from": "bridge_llm_execution",
                "backlog_title": f"Create test for module {i}",
                "task_title": f"Create test for module {i}",
                "summary": f"Create test for module {i}",
                "cycle_id": f"c{i}",
                "result_status": "blocked",
                "rollback": {"reason": "smoke_failed"},
            }),
            encoding="utf-8",
        )

    orig_scandir = os.scandir
    scandir_calls = 0

    def counting_scandir(path):
        nonlocal scandir_calls
        if str(path) == str(results_dir):
            scandir_calls += 1
        return orig_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagents" / "bridge")
    (state_dir / "subagents" / "requests").mkdir(parents=True)
    (state_dir / "subagents" / "requests" / "req_99.json").write_text(
        json.dumps({"request_id": "req_99"}),
        encoding="utf-8",
    )

    # 1. find_pending_request
    p, _ = bridge.find_pending_request()
    assert p is not None
    assert scandir_calls == 1

    # 2. _get_previous_attempts reuses cache
    prev = bridge._get_previous_attempts(state_dir=state_dir, backlog_title="Create test for module 0", cycle_id="c99")
    assert len(prev) >= 1
    assert scandir_calls == 1

    # 3. _migrate_backlog_title_in_results reuses cache
    mig = bridge._migrate_backlog_title_in_results(results_dir)
    assert mig == 0
    assert scandir_calls == 1

    # 4. _recent_activity_context reuses cache
    act = bridge._recent_activity_context(state_dir=state_dir, selfevo_repo_root=None)
    assert "Recently rejected" in act
    assert scandir_calls == 1

    # 5. _recent_failure_match reuses cache
    match = bridge._recent_failure_match(
        dup_check_title="Create test for module 0",
        state_dir=state_dir,
    )
    assert match is not None
    assert scandir_calls == 1

    # Invocations can be cleared for isolation
    bridge._clear_result_entries_cache()
    entries = bridge._iter_result_entries(results_dir)
    assert len(entries) == 3
    assert scandir_calls == 2
