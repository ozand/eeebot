"""Tests for issue #1381: the shared ``eeebot-self-evolving`` workspace's
local ``main`` falling permanently behind ``origin/main``.

Two defects, one incident (2026-09-06): the workspace had ``main...origin/main
[ahead 3, behind 1]`` — three local bookkeeping commits (each touching only
``lessons/errors.yaml``) that a two-point ``git diff remote_ref HEAD`` in
``bridge._diff_against_remote_touches_only`` refused to push the moment
upstream landed anything (it read upstream's own change as "ours" too), and
nothing in the bridge ever moved local ``main`` up to ``origin/main`` on its
own. This file exercises the fix: the guard's diff direction
(``remote_ref...HEAD``, merge-base to HEAD — "what WE changed"), the new
``bridge._catch_up_main`` decision table, ``_restore_to_main`` calling it, and
``bridge._push_main_or_report`` surfacing a rejected push instead of eating it
silently.

Follows the pattern in tests/test_bridge_cycle_branch.py: a bare "origin" plus
real git clones standing in for the shared checkout. Here there are two
clones — "seed" plays the upstream committer, "workspace" is the bridge's
shared checkout under test — so upstream and local history can diverge the
way the live incident did.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nanobot.runtime import bridge
from nanobot.runtime.cycle_ledger import read_events


def _git(repo: Path) -> list[str]:
    return ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(_git(repo) + list(args), capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Bare 'origin', a 'seed' clone (plays upstream commits), and a
    'workspace' clone (the repo under test, standing in for the shared
    eeebot-self-evolving checkout). Both clones start with one commit on main.
    Returns (origin, seed, workspace).
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    _run(seed, "config", "user.email", "seed@test.local")
    _run(seed, "config", "user.name", "seed-test")
    _run(seed, "checkout", "-B", "main")
    (seed / "tests").mkdir()
    (seed / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    (seed / "mod.py").write_text("def ok():\n    return True\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-m", "init")
    _run(seed, "push", "origin", "HEAD:main")

    subprocess.run(["git", "clone", str(origin), str(workspace)], check=True, capture_output=True)
    _run(workspace, "config", "user.email", "bridge@test.local")
    _run(workspace, "config", "user.name", "bridge-test")
    _run(workspace, "checkout", "-B", "main")
    return origin, seed, workspace


def _fetch_origin_main(workspace: Path) -> None:
    """The refresh _setup_cycle_branch always does before reading origin/main."""
    result = _run(workspace, "fetch", "origin", "main")
    assert result.returncode == 0, result.stderr


def _seed_commit_and_push(seed: Path, name: str, content: str, message: str) -> None:
    path = seed / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _run(seed, "add", name)
    _run(seed, "commit", "-m", message)
    push = _run(seed, "push", "origin", "HEAD:main")
    assert push.returncode == 0, push.stderr


def _workspace_commit(workspace: Path, name: str, content: str, message: str) -> None:
    path = workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _run(workspace, "add", name)
    result = _run(workspace, "commit", "-m", message)
    assert result.returncode == 0, result.stderr


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return _run(repo, "rev-parse", ref).stdout.strip()


def _last_ledger_row(state_dir: Path, phase: str = "workspace_sync") -> dict:
    rows = [r for r in read_events(state_dir) if r.get("phase") == phase]
    assert rows, f"no {phase!r} row found in ledger"
    return rows[-1]


class TestDiffAgainstRemoteGuardDirection:
    """#1381: the guard must diff ``remote_ref...HEAD`` (merge-base to HEAD —
    what OUR commits changed), not a two-point ``remote_ref HEAD`` diff that
    also picks up whatever upstream changed since we last caught up.
    """

    def test_upstream_only_change_is_not_read_as_ours(self, tmp_path):
        origin, seed, workspace = _init_repo(tmp_path)
        # Upstream adds a .gitignore rule...
        _seed_commit_and_push(seed, ".gitignore", "lessons/index.md\n", "chore: ignore index")
        # ...workspace never merges it, and separately makes its own
        # bookkeeping commit on top of the OLD base (the live divergence shape).
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: []\n", "chore: record error")
        _fetch_origin_main(workspace)

        assert bridge._diff_against_remote_touches_only(
            workspace, "origin/main", {"lessons/errors.yaml"},
        ) is True

    def test_no_local_commits_returns_false(self, tmp_path):
        origin, seed, workspace = _init_repo(tmp_path)
        _fetch_origin_main(workspace)

        # Nothing to push -> fail closed, not vacuously True.
        assert bridge._diff_against_remote_touches_only(
            workspace, "origin/main", {"lessons/errors.yaml"},
        ) is False

    def test_local_commit_outside_allowed_set_returns_false(self, tmp_path):
        origin, seed, workspace = _init_repo(tmp_path)
        _workspace_commit(workspace, "scripts/x.py", "x = 1\n", "feat: add script")
        _fetch_origin_main(workspace)

        assert bridge._diff_against_remote_touches_only(
            workspace, "origin/main", {"lessons/errors.yaml"},
        ) is False


class TestCatchUpMainDecisionTable:
    def test_in_sync_is_noop_and_writes_no_ledger_row(self, tmp_path):
        origin, seed, workspace = _init_repo(tmp_path)
        _fetch_origin_main(workspace)
        state_dir = tmp_path / "state"
        before = _sha(workspace, "main")

        row = bridge._catch_up_main(workspace, state_dir)

        assert row["action"] == "noop"
        assert row["behind"] == 0
        assert row["ahead"] == 0
        assert _sha(workspace, "main") == before
        assert [r for r in read_events(state_dir) if r.get("phase") == "workspace_sync"] == []

    def test_behind_only_fast_forwards(self, tmp_path):
        origin, seed, workspace = _init_repo(tmp_path)
        _seed_commit_and_push(seed, "upstream.py", "y = 1\n", "feat: upstream change")
        _fetch_origin_main(workspace)
        state_dir = tmp_path / "state"

        row = bridge._catch_up_main(workspace, state_dir, cycle_id="cyc-ff")

        assert row["action"] == "ff"
        assert row["behind"] == 1
        assert row["ahead"] == 0
        assert row["cycle_id"] == "cyc-ff"
        assert _sha(workspace, "main") == _sha(workspace, "origin/main")
        ledger_row = _last_ledger_row(state_dir)
        assert ledger_row["action"] == "ff"
        assert ledger_row["behind"] == 1
        assert ledger_row["ahead"] == 0
        assert ledger_row["cycle_id"] == "cyc-ff"

    def test_behind_zero_ahead_bookkeeping_pushes_without_rebase(self, tmp_path):
        """behind 0, ahead > 0, all bookkeeping -> a guard-free push of our own
        commits, with no rebase needed (local already contains all of origin)."""
        origin, seed, workspace = _init_repo(tmp_path)
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [1]\n", "chore: record error 1")
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [1, 2]\n", "chore: record error 2")
        _fetch_origin_main(workspace)
        state_dir = tmp_path / "state"

        row = bridge._catch_up_main(workspace, state_dir)

        assert row["action"] == "push"
        assert row["behind"] == 0
        assert row["ahead"] == 2
        assert row["pushed"] is True
        origin_log = _run(origin, "log", "--format=%s", "main").stdout
        assert "chore: record error 1" in origin_log
        assert "chore: record error 2" in origin_log
        assert _sha(workspace, "main") == _run(origin, "rev-parse", "main").stdout.strip()
        ledger_row = _last_ledger_row(state_dir)
        assert ledger_row["action"] == "push"
        assert ledger_row["pushed"] is True

    def test_bookkeeping_ahead_rebases_and_pushes(self, tmp_path):
        """The live incident shape: 3 local-only commits each touching only
        lessons/errors.yaml, upstream 1 commit ahead (a .gitignore rule)."""
        origin, seed, workspace = _init_repo(tmp_path)
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [1]\n", "chore: record error 1")
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [1, 2]\n", "chore: record error 2")
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [1, 2, 3]\n", "chore: record error 3")
        _seed_commit_and_push(seed, ".gitignore", "lessons/index.md\n", "chore: ignore index")
        _fetch_origin_main(workspace)
        state_dir = tmp_path / "state"

        row = bridge._catch_up_main(workspace, state_dir)

        assert row["action"] == "rebase"
        assert row["pushed"] is True
        assert row["local_files"] == ["lessons/errors.yaml"]
        assert row["local_files_total"] == 1
        # origin/main now carries both the 3 error commits and upstream's rule.
        origin_log = _run(origin, "log", "--format=%s", "main").stdout
        assert "chore: record error 1" in origin_log
        assert "chore: record error 2" in origin_log
        assert "chore: record error 3" in origin_log
        assert "chore: ignore index" in origin_log
        assert _sha(workspace, "main") == _run(origin, "rev-parse", "main").stdout.strip()
        check_ignore = _run(workspace, "check-ignore", "lessons/index.md")
        assert check_ignore.returncode == 0
        ledger_row = _last_ledger_row(state_dir)
        assert ledger_row["action"] == "rebase"
        assert ledger_row["pushed"] is True
        assert ledger_row["local_files"] == ["lessons/errors.yaml"]

    def test_non_bookkeeping_ahead_is_left_and_parks_the_stranded_ref(self, tmp_path):
        origin, seed, workspace = _init_repo(tmp_path)
        _workspace_commit(workspace, "scripts/x.py", "x = 1\n", "feat: local script")
        _seed_commit_and_push(seed, "upstream.py", "y = 1\n", "feat: upstream change")
        _fetch_origin_main(workspace)
        state_dir = tmp_path / "state"
        before = _sha(workspace, "main")

        row = bridge._catch_up_main(workspace, state_dir, cycle_id="cyc-left")

        assert row["action"] == "left"
        assert "scripts/x.py" in row["local_files"]
        assert row["stranded_ref"] == "refs/bridge/stranded-main"
        assert row["cycle_id"] == "cyc-left"
        # main is untouched: same sha, still behind.
        assert _sha(workspace, "main") == before
        after_behind = _run(
            workspace, "rev-list", "--count", "main..origin/main",
        ).stdout.strip()
        assert after_behind == "1"
        # The stranded ref parks the (unchanged) local tip so the later
        # integration path's `checkout -B main <base>` can't silently lose it.
        parked = _run(workspace, "rev-parse", "refs/bridge/stranded-main").stdout.strip()
        assert parked == before
        ledger_row = _last_ledger_row(state_dir)
        assert ledger_row["action"] == "left"
        assert "scripts/x.py" in ledger_row["local_files"]
        assert ledger_row["stranded_ref"] == "refs/bridge/stranded-main"

    def test_rebase_conflict_aborts_leaves_main_untouched_and_parks_the_ref(self, tmp_path):
        origin, seed, workspace = _init_repo(tmp_path)
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [workspace]\n", "chore: workspace edit")
        _seed_commit_and_push(seed, "lessons/errors.yaml", "errors: [upstream]\n", "chore: upstream edit")
        _fetch_origin_main(workspace)
        state_dir = tmp_path / "state"
        before = _sha(workspace, "main")

        row = bridge._catch_up_main(workspace, state_dir)

        assert row["action"] == "rebase_conflict"
        assert row["stranded_ref"] == "refs/bridge/stranded-main"
        assert _sha(workspace, "main") == before
        assert _run(workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        status = _run(workspace, "status", "--porcelain").stdout
        assert status.strip() == ""
        assert not (workspace / ".git" / "rebase-merge").exists()
        assert not (workspace / ".git" / "rebase-apply").exists()
        parked = _run(workspace, "rev-parse", "refs/bridge/stranded-main").stdout.strip()
        assert parked == before
        ledger_row = _last_ledger_row(state_dir)
        assert ledger_row["action"] == "rebase_conflict"
        assert ledger_row["stranded_ref"] == "refs/bridge/stranded-main"

    def test_no_origin_remote_is_unavailable(self, tmp_path):
        repo = tmp_path / "standalone"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "--initial-branch=main", str(repo)],
            check=True, capture_output=True,
        )
        _run(repo, "config", "user.email", "solo@test.local")
        _run(repo, "config", "user.name", "solo-test")
        (repo / "mod.py").write_text("x = 1\n")
        _run(repo, "add", ".")
        _run(repo, "commit", "-m", "init")
        state_dir = tmp_path / "state"
        before = _sha(repo, "main")

        row = bridge._catch_up_main(repo, state_dir)

        assert row["action"] == "unavailable"
        assert _sha(repo, "main") == before


class TestRestoreToMainCatchesUp:
    def test_restore_from_cycle_branch_catches_up_bookkeeping(self, tmp_path):
        origin, seed, workspace = _init_repo(tmp_path)
        # 3 unpushed local bookkeeping commits sitting on the resting main...
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [1]\n", "chore: record error 1")
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [1, 2]\n", "chore: record error 2")
        _workspace_commit(workspace, "lessons/errors.yaml", "errors: [1, 2, 3]\n", "chore: record error 3")
        # ...meanwhile upstream moved ahead by one commit.
        _seed_commit_and_push(seed, ".gitignore", "lessons/index.md\n", "chore: ignore index")
        # A cycle branches off origin/main (fetches), leaving main resting
        # with its 3 unpushed commits untouched underneath.
        setup = bridge._setup_cycle_branch(workspace, "restore-catchup")
        assert setup["ok"]
        (workspace / "scratch.tmp").write_text("leftover\n")
        state_dir = tmp_path / "state"

        restored = bridge._restore_to_main(workspace, state_dir)

        assert restored is True
        assert _run(workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        assert _run(workspace, "status", "--porcelain").stdout.strip() == ""
        behind = _run(workspace, "rev-list", "--count", "main..origin/main").stdout.strip()
        assert behind == "0"
        assert [r for r in read_events(state_dir) if r.get("phase") == "workspace_sync"]

    def test_gitignore_rule_on_origin_protects_the_first_restore(self, tmp_path):
        """#1354: order is reset -> clean -> checkout main -> _catch_up_main ->
        clean AGAIN, so the second clean runs with the rule origin/main
        already has (fetched by _setup_cycle_branch below) rather than a
        stale local .gitignore — the untracked generated file survives on
        this very first _restore_to_main call, no second call needed."""
        origin, seed, workspace = _init_repo(tmp_path)
        _seed_commit_and_push(seed, ".gitignore", "lessons/index.md\n", "chore: ignore index")
        setup = bridge._setup_cycle_branch(workspace, "restore-gitignore")
        assert setup["ok"]
        (workspace / "lessons").mkdir(parents=True, exist_ok=True)
        (workspace / "lessons" / "index.md").write_text("# index\n")
        state_dir = tmp_path / "state"

        restored = bridge._restore_to_main(workspace, state_dir)

        assert restored is True
        assert _run(workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
        assert (workspace / "lessons" / "index.md").exists()
        assert _run(workspace, "check-ignore", "lessons/index.md").returncode == 0

    def test_without_the_rule_restore_deletes_the_file(self, tmp_path):
        """Negative control: no .gitignore rule anywhere (the pre-fix/#1354
        shape) -> the untracked file does NOT survive _restore_to_main's clean."""
        origin, seed, workspace = _init_repo(tmp_path)
        state_dir = tmp_path / "state"

        (workspace / "lessons").mkdir(parents=True, exist_ok=True)
        (workspace / "lessons" / "index.md").write_text("# index\n")

        restored = bridge._restore_to_main(workspace, state_dir)

        assert restored is True
        assert not (workspace / "lessons" / "index.md").exists()


class TestPushMainOrReport:
    def test_non_fast_forward_push_is_reported_and_returns_false(self, tmp_path, capsys):
        origin, seed, workspace = _init_repo(tmp_path)
        # workspace commits locally without ever fetching seed's push below,
        # so its origin/main tracking ref is stale relative to the real origin.
        _workspace_commit(workspace, "mine.py", "x = 1\n", "feat: local change")
        _seed_commit_and_push(seed, "theirs.py", "y = 1\n", "feat: upstream change")
        state_dir = tmp_path / "state"

        git = bridge._git_cmd(workspace)
        result = bridge._push_main_or_report(git, "test-label", state_dir)

        assert result is False
        captured = capsys.readouterr()
        assert "test-label: push origin main rejected" in captured.out
        ledger_row = _last_ledger_row(state_dir)
        assert ledger_row["action"] == "push_rejected"
        assert ledger_row["label"] == "test-label"
        assert ledger_row["detail"]

    def test_no_state_dir_still_reports_without_writing_a_row(self, tmp_path, capsys):
        origin, seed, workspace = _init_repo(tmp_path)
        _workspace_commit(workspace, "mine.py", "x = 1\n", "feat: local change")
        _seed_commit_and_push(seed, "theirs.py", "y = 1\n", "feat: upstream change")

        git = bridge._git_cmd(workspace)
        result = bridge._push_main_or_report(git, "test-label")

        assert result is False
        captured = capsys.readouterr()
        assert "push origin main rejected" in captured.out

    def test_pushable_commit_returns_true(self, tmp_path, capsys):
        origin, seed, workspace = _init_repo(tmp_path)
        _workspace_commit(workspace, "mine.py", "x = 1\n", "feat: local change")

        git = bridge._git_cmd(workspace)
        result = bridge._push_main_or_report(git, "test-label")

        assert result is True
        assert _run(origin, "log", "--format=%s", "main").stdout.strip().splitlines()[0] == "feat: local change"
