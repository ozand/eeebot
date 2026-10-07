"""Tests for data-only exporter authority closure comparison (ADR-038)."""

import os
import subprocess
from pathlib import Path

import pytest

from nanobot.runtime.context_authority import assert_same_authority


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="authority tests",
        GIT_AUTHOR_EMAIL="authority-tests@example.invalid",
        GIT_COMMITTER_NAME="authority tests",
        GIT_COMMITTER_EMAIL="authority-tests@example.invalid",
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "trusted.py").write_text("value = 1\n", encoding="utf-8")
    anchor = _commit(repo, "anchor")
    return repo


def test_same_authority_non_head_candidate_compares_as_data(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    anchor = _git(repo, "rev-parse", "HEAD")
    (repo / "unrelated.txt").write_text("candidate-only\n", encoding="utf-8")
    target = _commit(repo, "candidate")
    assert target != anchor

    assert_same_authority(str(repo), target, anchor, ("trusted.py",))


def test_changed_authority_fails_closed_without_running_target_sentinel(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    anchor = _git(repo, "rev-parse", "HEAD")
    marker = tmp_path / "executed"
    (repo / "trusted.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8",
    )
    target = _commit(repo, "tampered exporter")

    with pytest.raises(ValueError, match="authority differs"):
        assert_same_authority(str(repo), target, anchor, ("trusted.py",))
    assert not marker.exists()


@pytest.mark.parametrize("mutation", ["missing", "symlink", "mode"])
def test_missing_symlink_and_mode_changed_entries_reject(tmp_path: Path, mutation: str) -> None:
    repo = _repo(tmp_path)
    anchor = _git(repo, "rev-parse", "HEAD")

    (repo / "trusted.py").unlink()
    if mutation == "symlink":
        (repo / "outside.py").write_text("outside = True\\n", encoding="utf-8")
        (repo / "trusted.py").symlink_to("outside.py")
    elif mutation == "mode":
        (repo / "trusted.py").write_text("value = 1\\n", encoding="utf-8")
        (repo / "trusted.py").chmod(0o755)
    target = _commit(repo, f"{mutation} authority")
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        assert_same_authority(str(repo), target, anchor, ("trusted.py",))


def test_manifest_paths_must_be_unique_relative_regular_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    anchor = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="non-empty and unique"):
        assert_same_authority(str(repo), anchor, anchor, ("trusted.py", "trusted.py"))
    with pytest.raises(ValueError, match="invalid authority path"):
        assert_same_authority(str(repo), anchor, anchor, ("../escape",))
