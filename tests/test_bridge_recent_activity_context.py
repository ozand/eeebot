"""Issue #713: novelty pressure — inject recent-activity context into the
subagent proposal prompt so it doesn't re-propose/re-implement recently
completed or recently rejected work.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from nanobot.runtime.bridge import (
    _recent_activity_context,
    _recent_commit_is_bookkeeping_only,
    _recent_commits_with_paths,
    build_task,
)
from tests.test_goal_backlog_routing import _make_git_repo_with_commit


# ---------------------------------------------------------------------------
# #1843: a diary bookkeeping commit (written every cycle) must not occupy a
# slot in the "Recently completed" window meant for WORK (written only on an
# accepted cycle) -- helpers build a repo with both kinds, interleaved.
# ---------------------------------------------------------------------------


def _init_bare_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    return repo


def _commit_work(repo: Path, i: int, prefix: str = "feat") -> None:
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / f"work_{i}.py").write_text(f"# work {i}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"{prefix}: implement work item {i}"], cwd=repo, check=True)


def _commit_diary(repo: Path, cycle_id: str) -> None:
    """Mirrors ADR-028 rule 1/2's real shape: one file per day, appended to,
    one commit per cycle, fixed subject -- but the fixture asserts on PATH,
    not this exact subject text (see _recent_commit_is_bookkeeping_only)."""
    diary_dir = repo / "diary"
    diary_dir.mkdir(exist_ok=True)
    path = diary_dir / "2026-09-21.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# Diary\n"
    path.write_text(existing + f"entry for {cycle_id}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"diary: cycle {cycle_id} opening entry (ADR-028)"],
        cwd=repo, check=True,
    )


def _commit_mixed(repo: Path, i: int) -> None:
    """A commit touching BOTH diary/ and a work path -- must NOT be treated
    as bookkeeping-only (it carries real content too)."""
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "diary").mkdir(exist_ok=True)
    (repo / "scripts" / f"mixed_{i}.py").write_text(f"# mixed {i}\n", encoding="utf-8")
    (repo / "diary" / "2026-09-21.md").write_text(f"mixed entry {i}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"feat: mixed commit {i}"], cwd=repo, check=True)


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


def test_build_task_anti_duplicate_instruction_moved_to_operating_md():
    """#1723(b): the skip-if-already-done instruction moved to OPERATING.md's
    'Before editing: skip check' section (release root, loaded into the
    system prompt by the loop-profile loader, #1725); build_task keeps only
    the one-line pointer."""
    req = {"task_title": "some task", "request_id": "r1", "cycle_id": "c1", "goal_id": "g1"}
    task = build_task(req, "mission text", "report_source.json")

    assert "if this task is already done, do NOT re-implement it" not in task
    assert "Rules: see OPERATING.md in your system prompt." in task


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


def test_1727_recent_activity_skips_merge_commits_keeps_eight(tmp_path: Path):
    """#1727 AC: a fixture git log with four merge subjects and six feature
    subjects yields six non-merge lines and no `merge:` line."""
    messages = []
    for i in range(4):
        messages.append(f"merge: integrate selfevo/cycle-cycle-{i}")
        messages.append(f"feat: implement feature {i}")
    messages.append("feat: implement feature 4")
    messages.append("feat: implement feature 5")
    # 4 merge + 6 feature commits, oldest first.
    repo = _make_git_repo_with_commit(tmp_path, *messages)

    ctx = _recent_activity_context(state_dir=None, selfevo_repo_root=repo)

    assert "merge:" not in ctx
    feature_lines = [ln for ln in ctx.splitlines() if ln.startswith("- ") and "implement feature" in ln]
    assert len(feature_lines) == 6


# ---------------------------------------------------------------------------
# #1843 -- diary bookkeeping commits must not occupy a slot in the window
# ---------------------------------------------------------------------------


def test_recent_commit_is_bookkeeping_only_classifies_by_path():
    assert _recent_commit_is_bookkeeping_only(["diary/2026-09-21.md"]) is True
    assert _recent_commit_is_bookkeeping_only(["diary/2026-09-21.md", "diary/notes.md"]) is True
    assert _recent_commit_is_bookkeeping_only(["scripts/foo.py"]) is False
    # A commit touching diary/ AND something else carries real content too --
    # never pure bookkeeping just because part of its diff is under diary/.
    assert _recent_commit_is_bookkeeping_only(["diary/2026-09-21.md", "scripts/foo.py"]) is False
    # No evidence either way -- kept visible, never guessed away.
    assert _recent_commit_is_bookkeeping_only([]) is False


def test_recent_commits_with_paths_parses_sha_subject_and_changed_files(tmp_path: Path):
    repo = _init_bare_repo(tmp_path)
    _commit_work(repo, 0)
    _commit_diary(repo, "cycle-abc")

    commits = _recent_commits_with_paths(repo, since="7 days ago")
    assert len(commits) == 2
    # Newest first.
    sha, subject, paths = commits[0]
    assert subject == "diary: cycle cycle-abc opening entry (ADR-028)"
    assert paths == ["diary/2026-09-21.md"]
    assert len(sha) >= 7  # abbreviated sha, not empty/truncated to nothing

    _sha2, subject2, paths2 = commits[1]
    assert subject2 == "feat: implement work item 0"
    assert paths2 == ["scripts/work_0.py"]


def test_recent_activity_excludes_diary_bookkeeping_commits(tmp_path: Path):
    """The exact live shape #1843 measured: diary commits interleaved with
    work commits must contribute zero lines to the rendered window, and
    must never be mistaken for the CURRENT cycle's own opening commit
    telling itself not to repeat."""
    repo = _init_bare_repo(tmp_path)
    for i in range(3):
        _commit_diary(repo, f"cycle-{i}a")
        _commit_work(repo, i)
        _commit_diary(repo, f"cycle-{i}b")

    ctx = _recent_activity_context(state_dir=None, selfevo_repo_root=repo)

    assert "diary:" not in ctx
    work_lines = [ln for ln in ctx.splitlines() if ln.startswith("- ") and "implement work item" in ln]
    assert len(work_lines) == 3


def test_recent_activity_window_of_n_work_commits_stays_n_for_any_diary_count(tmp_path: Path):
    """AC: the window of N work commits stays N regardless of how many
    diary commits are interleaved -- not merely "diary lines don't
    appear" but "real work lines are never pushed out by diary volume"."""
    for diary_per_work in (1, 5, 20):
        repo = _init_bare_repo(tmp_path, name=f"repo-{diary_per_work}")
        work_count = 3
        for i in range(work_count):
            for j in range(diary_per_work):
                _commit_diary(repo, f"cycle-{i}-{j}")
            _commit_work(repo, i)

        ctx = _recent_activity_context(state_dir=None, selfevo_repo_root=repo)
        work_lines = [ln for ln in ctx.splitlines() if ln.startswith("- ") and "implement work item" in ln]
        assert len(work_lines) == work_count, (
            f"diary_per_work={diary_per_work}: expected {work_count} work lines, got {len(work_lines)}"
        )


def test_recent_activity_keeps_a_commit_that_touches_diary_and_work_together(tmp_path: Path):
    repo = _init_bare_repo(tmp_path)
    _commit_mixed(repo, 0)

    ctx = _recent_activity_context(state_dir=None, selfevo_repo_root=repo)
    assert "mixed commit 0" in ctx


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
                "semantic_task_id": f"task-{i}",
                "target_path": f"scripts/module_{i}.py",
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
    prev = bridge._get_previous_attempts(
        state_dir=state_dir, semantic_task_id="task-0", target_path="scripts/module_0.py",
    )
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
