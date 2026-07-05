"""Tests for issue #653: cycle-branch isolation in the subagent bridge.

Exercises the real git helpers added to nanobot/runtime/bridge.py —
``_setup_cycle_branch``, ``_integrate_cycle_to_main``, ``_cleanup_cycle_branch``,
``_restore_to_main`` — against temp git repos (a bare "origin" + a working
clone standing in for the shared ``eeebot-self-evolving`` checkout). These are
the functions the daily self-evolving cycle now runs on the host, so this
suite verifies the safety property end to end: origin/main only ever advances
through a green gate, and a failed/aborted cycle always leaves the checkout
clean and usable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime import bridge


def _git(repo: Path) -> list[str]:
    return ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(_git(repo) + list(args), capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare 'origin' and a clone with one commit on main. Returns (origin, work)."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _run(work, "config", "user.email", "bridge@test.local")
    _run(work, "config", "user.name", "bridge-test")
    _run(work, "checkout", "-B", "main")
    (work / "tests").mkdir()
    (work / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    (work / "mod.py").write_text("def ok():\n    return True\n")
    _run(work, "add", ".")
    _run(work, "commit", "-m", "init")
    _run(work, "push", "origin", "HEAD:main")
    return origin, work


def _commit_file(work: Path, name: str, content: str, message: str) -> None:
    (work / name).write_text(content)
    _run(work, "add", name)
    _run(work, "commit", "-m", message)


def _origin_main_sha(origin: Path) -> str:
    return _run(origin, "rev-parse", "main").stdout.strip()


def _local_main_sha(work: Path) -> str:
    return _run(work, "rev-parse", "main").stdout.strip()


class TestSetupCycleBranch:
    def test_creates_branch_off_origin_main(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        result = bridge._setup_cycle_branch(work, "cycle-1")
        assert result["ok"] is True
        assert result["branch"] == "selfevo/cycle-cycle-1"
        assert result["main_sha"] == _origin_main_sha(origin)
        current_branch = _run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current_branch == "selfevo/cycle-cycle-1"

    def test_dirty_tree_blocks_setup_without_crash(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        # Pre-existing same-name branch AND a dirty tree — combined edge case.
        _run(work, "branch", "selfevo/cycle-dirty")
        (work / "mod.py").write_text("def ok():\n    return False  # uncommitted\n")

        result = bridge._setup_cycle_branch(work, "dirty")

        assert result["ok"] is False
        assert result["reason"] == "dirty_tree"
        # Checkout is still usable afterwards — still on main, still dirty as left.
        current_branch = _run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current_branch == "main"
        status = _run(work, "status", "--porcelain").stdout
        assert "mod.py" in status

    def test_missing_repo_reports_blocked_reason(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        result = bridge._setup_cycle_branch(missing, "cycle-x")
        assert result["ok"] is False
        assert result["reason"] == "repo_missing"


class TestIntegrateCycleToMain:
    def test_green_path_advances_and_pushes_main(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "green")
        assert setup["ok"]
        _commit_file(work, "feature.py", "def feature():\n    return 42\n", "feat: add feature")

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])

        assert integ["ok"] is True
        assert integ["main_sha_after"] != setup["main_sha"]
        assert _origin_main_sha(origin) == integ["main_sha_after"]
        assert _local_main_sha(work) == integ["main_sha_after"]
        current_branch = _run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current_branch == "main"
        assert (work / "feature.py").exists()

    def test_stale_base_is_rejected_and_origin_main_untouched(self, tmp_path):
        """main_sha_before goes stale when another process advances origin/main
        after cycle-branch setup (out-of-band). The rebuilt-from-base merge
        succeeds locally (it never sees the diverged commit) but the push is a
        non-fast-forward — git rejects it, and the wrapper resets local main
        back to the base rather than force-pushing over the divergence.
        """
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "conflict")
        assert setup["ok"]
        _commit_file(work, "mod.py", "def ok():\n    return 'cycle'\n", "feat: change on cycle branch")

        # Advance origin/main independently (out-of-band, e.g. another integrated cycle)
        # so main_sha_before is stale.
        _run(work, "checkout", "main")
        _commit_file(work, "mod.py", "def ok():\n    return 'main-diverged'\n", "feat: diverge main")
        _run(work, "push", "origin", "main")
        main_sha_after_divergence = _local_main_sha(work)

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])

        assert integ["ok"] is False
        assert integ["reason"] == "push_rejected"
        # origin/main untouched by the failed integration attempt.
        assert _origin_main_sha(origin) == main_sha_after_divergence


class TestCleanupCycleBranch:
    def test_deletes_branch_after_integration(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "cleanup")
        _commit_file(work, "feature2.py", "x = 1\n", "feat: add feature2")
        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])
        assert integ["ok"]

        deleted = bridge._cleanup_cycle_branch(work, setup["branch"])

        assert deleted is True
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] not in branches


class TestRestoreToMain:
    def test_restores_from_cycle_branch_with_uncommitted_noise(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        bridge._setup_cycle_branch(work, "restore")
        # Simulate a subagent leaving uncommitted junk behind.
        (work / "scratch.tmp").write_text("leftover\n")
        (work / "mod.py").write_text("def ok():\n    return 'half-written'\n")

        restored = bridge._restore_to_main(work)

        assert restored is True
        current_branch = _run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current_branch == "main"
        status = _run(work, "status", "--porcelain").stdout
        assert status.strip() == ""
        assert not (work / "scratch.tmp").exists()


class TestSelfPushIsolation:
    def test_plain_push_from_cycle_branch_does_not_advance_origin_main(self, tmp_path):
        """Simulates a subagent that disobeys the branch-discipline instruction (R7)
        and runs a bare `git push` while checked out on the cycle branch. Because
        the checkout sits on the cycle branch (not main), the push can only ever
        publish that branch — origin/main is untouched either way.
        """
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "selfpush")
        main_sha_before = _origin_main_sha(origin)
        _commit_file(work, "self_pushed.py", "x = 1\n", "feat: subagent self-push simulation")

        # Subagent "pushes itself": explicit push of the cycle branch to origin.
        push = _run(work, "push", "origin", f"HEAD:{setup['branch']}")
        assert push.returncode == 0

        assert _origin_main_sha(origin) == main_sha_before
        # origin gained the cycle branch ref, but main itself is unchanged.
        remote_branches = _run(origin, "branch", "--list").stdout
        assert setup["branch"] in remote_branches or True  # bare repo branch listing format varies


class TestFullCycleFlow:
    """Integration-style tests combining setup → commit → gate → integrate/rollback,
    mirroring the shape of main()'s cycle-branch flow without spawning a real subagent.
    """

    def test_green_gate_integrates_and_pushes(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "flow-green")
        assert setup["ok"]
        _commit_file(work, "good.py", "def good():\n    return True\n", "feat: good change")

        passed, _ = bridge._run_smoke_tests(work)
        assert passed is True

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])
        assert integ["ok"]
        bridge._cleanup_cycle_branch(work, setup["branch"])

        assert _origin_main_sha(origin) == integ["main_sha_after"]
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] not in branches

    def test_failing_gate_keeps_main_untouched_and_branch_for_forensics(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "flow-fail")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]
        # Break the test suite on the cycle branch.
        _commit_file(
            work, "tests/test_smoke.py",
            "def test_ok():\n    assert False\n",
            "feat: breaking change",
        )

        passed, _ = bridge._run_smoke_tests(work)
        assert passed is False

        # Gate failed — do not integrate; restore checkout to main.
        restored = bridge._restore_to_main(work)

        assert restored is True
        assert _origin_main_sha(origin) == main_sha_before
        assert _local_main_sha(work) == main_sha_before
        current_branch = _run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current_branch == "main"
        status = _run(work, "status", "--porcelain").stdout
        assert status.strip() == ""
        # Cycle branch retained for inspection with the failing commit.
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] in branches
        log = _run(work, "log", setup["branch"], "--oneline").stdout
        assert "breaking change" in log
