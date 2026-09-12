"""Behavioral tests for the repository-owned pre-commit branch guard."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "hooks" / "pre-commit"


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = _run("git", *args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result


def _seed_copy(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"))
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Hook Test")
    (source / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(source, "add", "hooks", "seed.txt")
    _git(source, "commit", "-m", "seed")
    return source


def _install_hook(repo: Path) -> None:
    _git(repo, "config", "core.hooksPath", "hooks")
    hook = repo / "hooks" / "pre-commit"
    if not hook.exists():
        source_hook = ROOT / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_hook, hook)
    hook.chmod(hook.stat().st_mode | 0o111)


def _commit(repo: Path, message: str = "change") -> subprocess.CompletedProcess[str]:
    (repo / "change.txt").write_text(message + "\n", encoding="utf-8")
    _git(repo, "add", "change.txt")
    return _run("git", "commit", "-m", message, cwd=repo)


def test_main_commit_is_refused_and_hook_is_invoked(tmp_path: Path):
    repo = _seed_copy(tmp_path)
    _install_hook(repo)
    before = _git(repo, "rev-parse", "HEAD").stdout

    result = _commit(repo)

    assert result.returncode != 0
    assert "refusing commit on the shared main/default branch" in result.stderr
    assert "T:/Code/.worktrees/eeebot-<issue>" in result.stderr
    assert "git commit --no-verify" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout == before


def test_configured_default_branch_is_refused(tmp_path: Path):
    repo = _seed_copy(tmp_path)
    _git(repo, "branch", "-m", "trunk")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _install_hook(repo)

    result = _commit(repo)

    assert result.returncode != 0
    assert "refusing commit on the shared main/default branch" in result.stderr


def test_feature_branch_commit_is_unaffected(tmp_path: Path):
    repo = _seed_copy(tmp_path)
    _install_hook(repo)
    _git(repo, "switch", "-c", "feat/test")

    result = _commit(repo)

    assert result.returncode == 0, result.stderr


def test_feature_worktree_commit_is_unaffected(tmp_path: Path):
    repo = _seed_copy(tmp_path)
    _install_hook(repo)
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-b", "feat/worktree", str(worktree))
    _install_hook(worktree)

    result = _commit(worktree)

    assert result.returncode == 0, result.stderr


def test_detached_head_is_unaffected(tmp_path: Path):
    repo = _seed_copy(tmp_path)
    _install_hook(repo)
    commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--detach", commit)

    result = _commit(repo)

    assert result.returncode == 0, result.stderr
