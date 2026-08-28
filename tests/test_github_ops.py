from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime import autoevolve, github_ops


def test_github_ops_exports_canonical_implementations() -> None:
    assert github_ops.ensure_selfevo_issue is autoevolve.ensure_selfevo_issue
    assert github_ops.ensure_selfevo_pr is autoevolve.ensure_selfevo_pr
    assert github_ops.merge_selfevo_pr is autoevolve.merge_selfevo_pr
    assert github_ops.close_selfevo_issue_if_open is autoevolve.close_selfevo_issue_if_open
    assert github_ops.commit_and_push_self_evolution is autoevolve.commit_and_push_self_evolution


def test_canonical_commit_stages_untracked_files(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    (repo / "new.txt").write_text("new", encoding="utf-8")
    result = autoevolve.commit_and_push_self_evolution(repo, "include new files")
    assert result["created_commit"] is True
    names = subprocess.run(["git", "show", "--format=", "--name-only", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.split()
    assert "new.txt" in names


def test_ensure_merge_close_use_mocked_gh(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Completed:
        stdout = "[]"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[1:3] == ["pr", "create"]:
            return type("R", (), {"stdout": "https://github.com/x/y/pull/7\n"})()
        if args[1:3] == ["issue", "create"]:
            return type("R", (), {"stdout": "https://github.com/x/y/issues/8\n"})()
        if args[1:3] == ["issue", "view"]:
            return type("R", (), {"stdout": "OPEN\n"})()
        return Completed()

    monkeypatch.setattr(autoevolve.subprocess, "run", fake_run)
    assert autoevolve.ensure_selfevo_issue(repo="x/y", title="t", body="b")["created"] is True
    assert autoevolve.ensure_selfevo_pr(repo="x/y", head_branch="h", base_branch="main", title="t", body="b")["created"] is True
    assert autoevolve.merge_selfevo_pr(repo="x/y", pr_number=7)["merged"] is True
    assert autoevolve.close_selfevo_issue_if_open(repo="x/y", issue_number=8)["attempted_close"] is True
    assert any("merge" in call for call in calls)
    assert any("close" in call for call in calls)
