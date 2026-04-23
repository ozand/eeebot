from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from nanobot.runtime.autoevolve import (
    create_candidate_release,
    apply_candidate_release,
    health_check_release,
    rollback_release,
    write_failure_learning_artifact,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "bot@example.com")
    _git(repo, "config", "user.name", "Bot")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    bare = repo.parent / "origin.git"
    _git(repo.parent, "init", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-u", "origin", "master")
    return _git(repo, "rev-parse", "HEAD")


def test_create_candidate_release_writes_git_provenance_and_archive(tmp_path: Path):
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    head = _init_repo(repo)

    record = create_candidate_release(repo_root=repo, workspace=workspace)

    assert record["commit"] == head
    assert record["remote_name"] == "origin"
    assert record["branch"] in {"master", "main"}
    assert record["clean_worktree"] is True
    assert record["remote_commit_visible"] is True
    assert Path(record["archive_path"]).exists()
    latest = json.loads((workspace / "state" / "self_evolution" / "candidates" / "latest.json").read_text())
    assert latest["candidate_id"] == record["candidate_id"]


def test_apply_candidate_release_switches_current_symlink_and_preserves_previous(tmp_path: Path):
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    _init_repo(repo)
    record1 = create_candidate_release(repo_root=repo, workspace=workspace)
    apply1 = apply_candidate_release(workspace=workspace, candidate_record=record1)
    current_link = workspace / "state" / "self_evolution" / "runtime" / "current"
    assert current_link.is_symlink()
    assert current_link.resolve() == Path(apply1["release_dir"]).resolve()

    (repo / "README.md").write_text("hello 2\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "second")
    _git(repo, "push", "origin", "HEAD")
    record2 = create_candidate_release(repo_root=repo, workspace=workspace)
    apply2 = apply_candidate_release(workspace=workspace, candidate_record=record2)
    assert Path(apply2["previous_release_dir"]).resolve() == Path(apply1["release_dir"]).resolve()
    assert current_link.resolve() == Path(apply2["release_dir"]).resolve()


def test_health_check_release_reports_fail_for_stale_report_and_pass_for_fresh_state(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    (state / "reports").mkdir(parents=True, exist_ok=True)
    (state / "control_plane").mkdir(parents=True, exist_ok=True)
    (state / "goals").mkdir(parents=True, exist_ok=True)
    (state / "reports" / "evolution-test.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (state / "control_plane" / "current_summary.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (state / "goals" / "current.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

    stale = health_check_release(workspace=workspace, max_report_age_seconds=0)
    assert stale["ok"] is False
    assert "stale_report" in stale["reasons"]

    fresh = health_check_release(workspace=workspace, max_report_age_seconds=3600)
    assert fresh["ok"] is True


def test_rollback_release_restores_previous_and_writes_failure_learning(tmp_path: Path):
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    _init_repo(repo)
    record1 = create_candidate_release(repo_root=repo, workspace=workspace)
    apply1 = apply_candidate_release(workspace=workspace, candidate_record=record1)

    (repo / "README.md").write_text("hello 2\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "second")
    _git(repo, "push", "origin", "HEAD")
    record2 = create_candidate_release(repo_root=repo, workspace=workspace)
    apply2 = apply_candidate_release(workspace=workspace, candidate_record=record2)

    rollback = rollback_release(workspace=workspace, failed_candidate_record=record2, previous_release_dir=Path(apply2["previous_release_dir"]))
    current_link = workspace / "state" / "self_evolution" / "runtime" / "current"
    assert current_link.resolve() == Path(apply1["release_dir"]).resolve()
    assert rollback["rolled_back_to_release_dir"] == str(Path(apply1["release_dir"]).resolve())

    health = {"ok": False, "reasons": ["stale_report", "service_inactive"]}
    learning = write_failure_learning_artifact(workspace=workspace, failed_candidate_record=record2, health_result=health, rollback_result=rollback)
    assert Path(learning["path"]).exists()
    latest = json.loads((workspace / "state" / "self_evolution" / "failure_learning" / "latest.json").read_text())
    assert latest["candidate_id"] == record2["candidate_id"]
    assert latest["health_reasons"] == ["stale_report", "service_inactive"]
