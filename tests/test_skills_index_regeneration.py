"""#1857: ``bridge._regenerate_skills_index_if_needed`` -- the harness-owned
commit that keeps ``skills/index.md`` from ever going stale. Mirrors
``test_diary_plan_block.py``'s git-repo fixture and push-failure proof.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime.bridge import _regenerate_skills_index_if_needed, _should_regenerate_skills_index
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


# ---------------------------------------------------------------------------
# #1857 follow-up: the bootstrap gap. The resident catalogue leaves every
# prompt the moment this ships; skills/index.md is born only on the FIRST
# later cycle whose own diff touches skills/ -- rare (two renames in the
# loop's whole history). _should_regenerate_skills_index also fires when the
# index is simply missing, independent of files_changed, so that window
# cannot run for days with neither channel present.
# ---------------------------------------------------------------------------


def test_should_regenerate_when_index_missing_even_with_an_unrelated_diff(tmp_path: Path):
    """A cycle that touched nothing under skills/ still triggers
    regeneration when the index does not exist at all -- the bootstrap
    case this property exists for."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert not (repo / INDEX_RELPATH).exists()

    assert _should_regenerate_skills_index(repo, ["docs/README.md"]) is True


def test_should_not_regenerate_when_index_exists_and_diff_is_unrelated(tmp_path: Path):
    """An existing index and an unrelated diff must not trigger a
    bookkeeping-only regeneration attempt -- the property #1857 shipped
    with (no commit when nothing changed) must survive this fix."""
    repo = tmp_path / "repo"
    target = repo / INDEX_RELPATH
    target.parent.mkdir(parents=True)
    target.write_text("# Skills index\n\n(no skills yet)\n", encoding="utf-8")

    assert _should_regenerate_skills_index(repo, ["docs/README.md"]) is False


def test_should_regenerate_when_diff_touches_skills_even_with_an_existing_index(tmp_path: Path):
    repo = tmp_path / "repo"
    target = repo / INDEX_RELPATH
    target.parent.mkdir(parents=True)
    target.write_text("# Skills index\n\n(no skills yet)\n", encoding="utf-8")

    assert _should_regenerate_skills_index(repo, ["skills/alpha/SKILL.md"]) is True


def test_end_to_end_bootstrap_creates_the_index_for_a_cycle_that_did_not_touch_skills(tmp_path: Path):
    """The full gate-plus-write path: a cycle whose own diff never touched
    skills/, on a repo with no index.md at all yet, still ends with the
    index created and pushed."""
    repo = _init_repo_with_origin(tmp_path)
    _add_skill(repo, "alpha")  # a skill exists on disk, just not from THIS cycle's diff
    _git(repo, "add", "skills/alpha/SKILL.md")
    _git(repo, "commit", "-m", "add alpha skill (an earlier cycle)")
    _git(repo, "push", "origin", "main")
    assert not (repo / INDEX_RELPATH).exists()

    files_changed = ["docs/README.md"]  # this cycle's own diff -- unrelated to skills/
    assert _should_regenerate_skills_index(repo, files_changed) is True
    result = _regenerate_skills_index_if_needed(repo, files_changed)

    assert result["outcome"] == "integrated"
    assert (repo / INDEX_RELPATH).is_file()
    assert "- alpha: does things" in (repo / INDEX_RELPATH).read_text(encoding="utf-8")

    # And a second such cycle, with the index now current, makes no
    # bookkeeping-only commit.
    before_sha = _git(repo, "rev-parse", "HEAD")
    assert _should_regenerate_skills_index(repo, files_changed) is False
    second = _regenerate_skills_index_if_needed(repo, files_changed)
    assert second["outcome"] == "unchanged"
    assert _git(repo, "rev-parse", "HEAD") == before_sha
