"""#1857: ``bridge._regenerate_skills_index_if_needed`` -- the harness-owned
commit that keeps ``skills/index.md`` from ever going stale. Mirrors
``test_diary_plan_block.py``'s git-repo fixture and push-failure proof.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime.bridge import _regenerate_skills_index_if_needed
from nanobot.runtime.skills_index import INDEX_RELPATH


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _init_repo_with_origin(tmp_path: Path) -> Path:
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _add_skill(repo: Path, name: str, description: str = "does things") -> None:
    path = repo / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n", encoding="utf-8")


def test_regenerates_and_pushes_when_skills_changed(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    _add_skill(repo, "alpha")
    _git(repo, "add", "skills/alpha/SKILL.md")
    _git(repo, "commit", "-m", "add alpha skill")
    _git(repo, "push", "origin", "main")

    result = _regenerate_skills_index_if_needed(repo, ["skills/alpha/SKILL.md"])

    assert result["outcome"] == "integrated"
    _git(repo, "fetch", "origin", "main")
    pushed = subprocess.run(
        ["git", "-C", str(repo), "show", f"origin/main:{INDEX_RELPATH}"], capture_output=True, text=True,
    ).stdout
    assert "- alpha: does things" in pushed
    assert _git(repo, "status", "--porcelain") == ""


def test_no_op_when_index_already_current(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    _add_skill(repo, "alpha")
    _git(repo, "add", "skills/alpha/SKILL.md")
    _git(repo, "commit", "-m", "add alpha skill")

    first = _regenerate_skills_index_if_needed(repo, ["skills/alpha/SKILL.md"])
    assert first["outcome"] == "integrated"
    before_sha = _git(repo, "rev-parse", "HEAD")

    second = _regenerate_skills_index_if_needed(repo, ["skills/alpha/SKILL.md"])
    assert second["outcome"] == "unchanged"
    assert _git(repo, "rev-parse", "HEAD") == before_sha


def test_commit_is_scoped_to_the_index_only(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    _add_skill(repo, "alpha")
    _git(repo, "add", "skills/alpha/SKILL.md")
    _git(repo, "commit", "-m", "add alpha skill")

    result = _regenerate_skills_index_if_needed(repo, ["skills/alpha/SKILL.md"])

    files = _git(repo, "show", "--name-only", "--format=", result["commit_sha"]).splitlines()
    assert [f for f in files if f.strip()] == [INDEX_RELPATH]


def test_push_failure_rolls_back_and_leaves_tree_clean(tmp_path: Path):
    repo = _init_repo_with_origin(tmp_path)
    _add_skill(repo, "alpha")
    _git(repo, "add", "skills/alpha/SKILL.md")
    _git(repo, "commit", "-m", "add alpha skill")
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    pre_sha = _git(repo, "rev-parse", "HEAD")

    result = _regenerate_skills_index_if_needed(repo, ["skills/alpha/SKILL.md"])

    assert result["outcome"] == "push_failed"
    assert _git(repo, "rev-parse", "HEAD") == pre_sha
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / INDEX_RELPATH).exists()
