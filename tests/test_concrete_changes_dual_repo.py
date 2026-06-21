"""
Tests for issue #509: _has_concrete_changes must also check eeebot-self-evolving repo.

In the two-repo topology, subagents commit to eeebot-self-evolving, not to the
canonical workspace.  Without checking that repo, reward is always 0.8 and every
cycle registers outcome=discard.
"""
import subprocess
import tempfile
from pathlib import Path

import pytest

from nanobot.runtime.coordinator import _has_concrete_changes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(*args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True)


def _init_repo(path: Path, initial_file: str = "README.md") -> None:
    """Initialise a minimal git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", "test@test.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / initial_file).write_text("init\n")
    _git("add", ".", cwd=path)
    _git("commit", "-m", "init", cwd=path)


def _add_commit(repo: Path, filename: str, content: str, msg: str) -> None:
    fpath = repo / filename
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(content)
    _git("add", ".", cwd=repo)
    _git("commit", "-m", msg, cwd=repo)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHasConcreteChanges:

    def test_no_state_root_canonical_only(self, tmp_path):
        """Without state_root, behaves as before: checks canonical workspace only."""
        canonical = tmp_path / "canonical"
        _init_repo(canonical)
        # No uncommitted changes, no autoevolve commit → should be False
        result = _has_concrete_changes(canonical, state_root=None)
        assert result is False

    def test_non_git_workspace_returns_true(self, tmp_path):
        """Non-git directory → True (safe default, same as original behaviour)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = _has_concrete_changes(workspace, state_root=None)
        assert result is True

    def test_selfevo_recent_commit_detected(self, tmp_path):
        """Recent commit in eeebot-self-evolving → True even if canonical unchanged."""
        # state structure: state_root.parent / "eeebot-self-evolving"
        state_root = tmp_path / "self-evolving-agent" / "state"
        state_root.mkdir(parents=True)
        selfevo = tmp_path / "self-evolving-agent" / "eeebot-self-evolving"
        _init_repo(selfevo)

        canonical = tmp_path / "canonical"
        _init_repo(canonical)

        # Add a recent commit to selfevo (just now)
        _add_commit(selfevo, "nanobot/utils/new_tool.py", "# new\n", "feat: add new tool")

        result = _has_concrete_changes(canonical, state_root=state_root)
        assert result is True, (
            "Should return True when eeebot-self-evolving has commits in last 15 min"
        )

    def test_selfevo_missing_does_not_crash(self, tmp_path):
        """If eeebot-self-evolving directory doesn't exist, falls back to canonical check."""
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        # eeebot-self-evolving does NOT exist → should not crash

        canonical = tmp_path / "canonical"
        _init_repo(canonical)

        result = _has_concrete_changes(canonical, state_root=state_root)
        # canonical has no changes → False
        assert result is False

    def test_selfevo_non_git_dir_skipped(self, tmp_path):
        """If eeebot-self-evolving exists but isn't a git repo, skip it gracefully."""
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        selfevo = tmp_path / "eeebot-self-evolving"
        selfevo.mkdir()
        (selfevo / "some_file.txt").write_text("not a repo")

        canonical = tmp_path / "canonical"
        _init_repo(canonical)

        result = _has_concrete_changes(canonical, state_root=state_root)
        assert result is False  # canonical clean, selfevo not git → False

    def test_canonical_unstaged_change_still_detected(self, tmp_path):
        """Unstaged changes in canonical workspace are still detected (original path)."""
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)

        canonical = tmp_path / "canonical"
        _init_repo(canonical)

        # Create unstaged .py change
        (canonical / "nanobot" / "runtime").mkdir(parents=True)
        (canonical / "nanobot" / "runtime" / "fix.py").write_text("# fix\n")
        _git("add", ".", cwd=canonical)
        # do NOT commit — leave it staged

        result = _has_concrete_changes(canonical, state_root=state_root)
        assert result is True

    def test_selfevo_path_is_state_root_parent_slash_name(self, tmp_path):
        """Verifies correct path derivation: state_root.parent / 'eeebot-self-evolving'."""
        # state_root = /tmp/xyz/self-evolving-agent/state
        # expected selfevo = /tmp/xyz/self-evolving-agent/eeebot-self-evolving
        agent_dir = tmp_path / "self-evolving-agent"
        state_root = agent_dir / "state"
        state_root.mkdir(parents=True)

        selfevo = agent_dir / "eeebot-self-evolving"
        _init_repo(selfevo)
        _add_commit(selfevo, "scripts/new_script.sh", "#!/bin/bash\n", "feat: new script")

        canonical = tmp_path / "canonical"
        _init_repo(canonical)

        result = _has_concrete_changes(canonical, state_root=state_root)
        assert result is True, (
            "Path derivation state_root.parent/'eeebot-self-evolving' must point to selfevo repo"
        )
