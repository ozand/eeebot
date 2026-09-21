"""ADR-031 rule 5 (#1852): the planning session's plan block survives
whatever happens to the cycle branch, the same way ADR-028 rule 3 already
does for the bridge's opening diary entry -- see ``test_diary_open_entry.py``
for the sibling proof this mirrors, including the exact git sequence.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime.bridge import _write_diary_plan_block
from nanobot.runtime.day_diary import PLAN_BEGIN, PLAN_END, diary_relpath


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _init_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
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
    return repo, origin


def _origin_main_show(repo: Path, rel: str) -> "str | None":
    _git(repo, "fetch", "origin", "main")
    proc = subprocess.run(["git", "-C", str(repo), "show", f"origin/main:{rel}"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def test_plan_block_creates_a_fresh_day_file_and_pushes_it(tmp_path: Path):
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    result = _write_diary_plan_block(repo, state, "cycle-1", "Insight: nothing yet\nPlan: connect the validator")

    assert result["outcome"] == "integrated"
    relpath = diary_relpath()
    pushed = _origin_main_show(repo, relpath)
    assert pushed is not None
    assert "connect the validator" in pushed
    assert pushed.count(PLAN_BEGIN) == 1
    assert pushed.count(PLAN_END) == 1
    assert (repo / relpath).read_text(encoding="utf-8") == pushed
    assert _git(repo, "status", "--porcelain") == ""


def test_plan_block_replaces_a_prior_plan_on_the_second_run(tmp_path: Path):
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _write_diary_plan_block(repo, state, "cycle-1", "first plan text")
    result = _write_diary_plan_block(repo, state, "cycle-2", "second plan text")

    assert result["outcome"] == "integrated"
    pushed = _origin_main_show(repo, diary_relpath())
    assert pushed is not None
    assert "second plan text" in pushed
    assert "first plan text" not in pushed
    assert pushed.count(PLAN_BEGIN) == 1


def test_plan_block_commit_is_path_scoped_to_diary_only(tmp_path: Path):
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    result = _write_diary_plan_block(repo, state, "cycle-1", "a plan")

    files = _git(repo, "show", "--name-only", "--format=", result["commit_sha"]).splitlines()
    assert [f for f in files if f.strip()] == [diary_relpath()]


def test_plan_block_preserves_entries_already_in_the_file(tmp_path: Path):
    """The plan block and the entries list are different regions of the
    same file -- writing one must never disturb the other."""
    from nanobot.runtime.bridge import _write_diary_open_entry

    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _write_diary_open_entry(repo, state, "cycle-1", "Implement and commit: task one")
    result = _write_diary_plan_block(repo, state, "cycle-1", "do task two next")

    pushed = _origin_main_show(repo, diary_relpath())
    assert pushed is not None
    assert "task one" in pushed
    assert "do task two next" in pushed
    assert result["outcome"] == "integrated"


def test_plan_block_push_failure_rolls_back_main_and_leaves_tree_clean(tmp_path: Path):
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    pre_sha = _git(repo, "rev-parse", "HEAD")

    result = _write_diary_plan_block(repo, state, "cycle-1", "a plan that never leaves the checkout")

    assert result["outcome"] == "push_failed"
    assert _git(repo, "rev-parse", "HEAD") == pre_sha
    assert _git(repo, "status", "--porcelain") == ""
