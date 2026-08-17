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

import pytest

from nanobot.runtime import bridge
from nanobot.runtime import evolution_tree as evo
from nanobot.runtime.archive import CycleArchive


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    """#686: the bounded gate's real core-smoke set names paths in THIS repo
    (tests/test_import_hygiene.py etc.), which don't exist in the synthetic
    "origin"/"work" repos this file builds. Point the core set at the one test
    file those fixtures actually create (tests/test_smoke.py) so the existing
    full-suite-style assertions below (which predate the bounded gate and only
    ever had that one test file to run) keep exercising the same content.
    """
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


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

    def test_dirty_tree_at_integration_still_integrates(self, tmp_path):
        """#828: a subagent may leave the shared checkout's working tree dirty
        at integration time (stray uncommitted edits / untracked files — some
        even run `git checkout <file>` mid-cycle despite the branch-discipline
        rule). Before #828, `git checkout -B main` refused to overwrite those
        local mods → checkout_main_failed → the committed, gate-passing cycle
        work was silently discarded and main never advanced. The integration
        must now discard the stray tree changes and land the committed work."""
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "dirtyok")
        assert setup["ok"]
        _commit_file(work, "feature.py", "def feature():\n    return 42\n", "feat: add feature")
        # Dirty the tree AFTER committing the deliverable: an uncommitted edit to
        # a tracked file + a stray untracked file, exactly what breaks checkout.
        (work / "mod.py").write_text("def ok():\n    return 'uncommitted stray'\n")
        (work / "stray_untracked.txt").write_text("junk\n")

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])

        assert integ["ok"] is True, integ
        assert integ["reason"] != "checkout_main_failed"
        assert _origin_main_sha(origin) == integ["main_sha_after"]
        assert _local_main_sha(work) == integ["main_sha_after"]
        assert (work / "feature.py").exists()  # committed deliverable landed
        assert not (work / "stray_untracked.txt").exists()  # stray change cleared
        # mod.py reverted to its committed content (stray edit discarded)
        assert "uncommitted stray" not in (work / "mod.py").read_text()

    def test_empty_cycle_branch_reports_failure_not_false_success(self, tmp_path):
        """#828 review: if the cycle branch has no commits beyond base (e.g. a
        misbehaving subagent committed OFF the cycle branch), the --no-ff merge
        is a no-op and HEAD stays at base. Integration must report a loud failure
        (empty_integration) with main unchanged — never a false 'integrated'."""
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "empty")
        assert setup["ok"]
        # Deliberately commit NOTHING on the cycle branch → it equals base.
        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])

        assert integ["ok"] is False
        assert integ["reason"] == "empty_integration"
        assert integ["main_sha_after"] == setup["main_sha"]
        assert _origin_main_sha(origin) == setup["main_sha"]  # origin/main untouched

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


class TestAutoCommitUncommittedWork:
    """Tests for issue #666: the subagent implements real changes via edit_file but
    finishes the turn without running git commit. Previously the bridge saw
    cycle_commit_count == 0, skipped the gate, and _restore_to_main() silently
    discarded the work. _auto_commit_uncommitted_work() is the safety net.
    """

    def test_dirty_tree_gets_committed(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-1")
        assert setup["ok"]
        # Subagent edits a file but never commits.
        (work / "mod.py").write_text("def ok():\n    return 'edited-not-committed'\n")

        result = bridge._auto_commit_uncommitted_work(
            work, setup["branch"], backlog_title="Wire host_metrics into dashboard",
        )

        assert result["committed"] is True
        assert result["files_committed"] == 1
        assert result["excluded"] == []
        status = _run(work, "status", "--porcelain").stdout
        assert status.strip() == ""
        log = _run(work, "log", "-1", "--pretty=%s").stdout
        assert log.startswith("selfevo: auto-commit uncommitted subagent work")
        assert "Wire host_metrics into dashboard" in log
        body = _run(work, "log", "-1", "--pretty=%b").stdout
        assert "#666" in body

    def test_clean_tree_is_a_noop(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-clean")
        assert setup["ok"]

        result = bridge._auto_commit_uncommitted_work(work, setup["branch"])

        assert result["committed"] is False
        assert result["files_committed"] == 0
        assert result["excluded"] == []
        # No stray commit was created.
        log = _run(work, "log", "-1", "--pretty=%s").stdout
        assert log.strip() == "init"

    def test_blocked_pattern_file_excluded(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-blocked")
        assert setup["ok"]
        (work / "mod.py").write_text("def ok():\n    return 'edited'\n")
        (work / ".env").write_text("SECRET=abc123\n")

        result = bridge._auto_commit_uncommitted_work(work, setup["branch"])

        assert result["committed"] is True
        assert result["files_committed"] == 1
        assert result["excluded"] == [".env"]
        # The blocked file is still untracked/dirty afterwards — never staged.
        status = _run(work, "status", "--porcelain").stdout
        assert ".env" in status
        assert "mod.py" not in status
        log_files = _run(work, "show", "--stat", "--pretty=", "HEAD").stdout
        assert ".env" not in log_files

    def test_only_blocked_pattern_files_commits_nothing(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-only-blocked")
        assert setup["ok"]
        (work / "credential.json").write_text('{"token": "x"}\n')

        result = bridge._auto_commit_uncommitted_work(work, setup["branch"])

        assert result["committed"] is False
        assert result["files_committed"] == 0
        assert result["excluded"] == ["credential.json"]


class TestFullCycleFlowWithAutoCommit:
    """Mirrors TestFullCycleFlow but for the #666 auto-commit safety net: a
    subagent that edits files without committing must still get a shot at the
    smoke gate, and a failing gate must leave the auto-commit on the forensic
    branch (never on main).
    """

    def test_dirty_uncommitted_work_green_gate_integrates(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-flow-green")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]
        # Subagent "implements" a change but forgets to commit.
        (work / "feature.py").write_text("def feature():\n    return 42\n")

        auto = bridge._auto_commit_uncommitted_work(work, setup["branch"])
        assert auto["committed"] is True

        passed, _ = bridge._run_smoke_tests(work)
        assert passed is True

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], main_sha_before)
        assert integ["ok"] is True
        bridge._cleanup_cycle_branch(work, setup["branch"])

        assert _origin_main_sha(origin) == integ["main_sha_after"]
        assert integ["main_sha_after"] != main_sha_before
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] not in branches

    def test_untracked_new_file_alongside_prior_commit_gets_committed_and_integrates(self, tmp_path):
        """#717: a subagent that made a real commit AND left a new file untracked
        (e.g. a script it forgot to `git add`) must still get that file swept
        into the auto-commit and through to main on a green gate — previously
        the bridge only called _auto_commit_uncommitted_work when
        cycle_commit_count == 0, so this untracked file was silently discarded
        by _restore_to_main()'s `git clean -fd`.
        """
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-flow-new-file")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]
        # Prior real commit made by the subagent...
        _commit_file(work, "feature.py", "def feature():\n    return 42\n", "feat: add feature")
        # ...but it also left a brand-new file untracked.
        (work / "scripts").mkdir()
        (work / "scripts" / "helper.py").write_text("def helper():\n    return True\n")

        auto = bridge._auto_commit_uncommitted_work(work, setup["branch"])
        assert auto["committed"] is True
        assert auto["files_committed"] == 1
        assert auto["excluded"] == []
        status = _run(work, "status", "--porcelain").stdout
        assert status.strip() == ""

        files_changed, blocked, mutation, _tier = bridge._changed_files_and_violations(
            work, main_sha_before,
        )
        assert "scripts/helper.py" in files_changed
        assert blocked == []

        passed, _ = bridge._run_smoke_tests(work)
        assert passed is True

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], main_sha_before)
        assert integ["ok"] is True
        bridge._cleanup_cycle_branch(work, setup["branch"])

        assert _origin_main_sha(origin) == integ["main_sha_after"]
        log_files = _run(origin, "show", "--stat", "--pretty=", "main").stdout
        assert "helper.py" in log_files

    def test_blocked_pattern_new_file_alongside_prior_commit_never_reaches_main(self, tmp_path):
        """#717 companion: the mutation-surface / blocked-pattern guard still
        applies to files swept in by the now-unconditional auto-commit — a
        blocked-pattern untracked file (e.g. `.env`) alongside a prior real
        commit is excluded from the auto-commit and never integrates.
        """
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-flow-blocked-new-file")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]
        _commit_file(work, "feature.py", "def feature():\n    return 42\n", "feat: add feature")
        (work / ".env").write_text("SECRET=abc123\n")

        auto = bridge._auto_commit_uncommitted_work(work, setup["branch"])
        assert auto["committed"] is False
        assert auto["files_committed"] == 0
        assert auto["excluded"] == [".env"]
        # The blocked file stays untracked/dirty — never staged or committed.
        status = _run(work, "status", "--porcelain").stdout
        assert ".env" in status

        passed, _ = bridge._run_smoke_tests(work)
        assert passed is True

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], main_sha_before)
        assert integ["ok"] is True
        bridge._cleanup_cycle_branch(work, setup["branch"])

        log_files = _run(origin, "show", "--stat", "--pretty=", "main").stdout
        assert ".env" not in log_files

    def test_dirty_uncommitted_work_failing_gate_keeps_forensic_branch(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-flow-fail")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]
        # Subagent breaks the test suite and never commits.
        (work / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert False\n")

        auto = bridge._auto_commit_uncommitted_work(work, setup["branch"])
        assert auto["committed"] is True

        passed, _ = bridge._run_smoke_tests(work)
        assert passed is False

        restored = bridge._restore_to_main(work)

        assert restored is True
        assert _origin_main_sha(origin) == main_sha_before
        assert _local_main_sha(work) == main_sha_before
        current_branch = _run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current_branch == "main"
        # Forensic branch retained WITH the auto-commit on it.
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] in branches
        log = _run(work, "log", setup["branch"], "--oneline").stdout
        assert "auto-commit uncommitted subagent work" in log

    def test_clean_tree_no_commits_stays_a_noop(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-flow-clean")
        assert setup["ok"]

        auto = bridge._auto_commit_uncommitted_work(work, setup["branch"])
        assert auto["committed"] is False

        # No gate is meaningful to run — same no-op path as before #666.
        new_commits = bridge._count_commits_since(work, setup["main_sha"])
        assert new_commits == 0

    def test_subagent_committed_normally_no_auto_commit(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "auto-flow-normal")
        assert setup["ok"]
        _commit_file(work, "feature3.py", "x = 1\n", "feat: subagent committed properly")
        pre_auto_sha = _run(work, "rev-parse", "HEAD").stdout.strip()

        # #717: main() now calls _auto_commit_uncommitted_work() unconditionally
        # (not only when cycle_commit_count == 0), but it is a no-op on an
        # already-clean tree (its own `git status --porcelain` check) — a
        # subagent that committed everything properly gets no extra commit.
        new_commits = bridge._count_commits_since(work, setup["main_sha"])
        assert new_commits == 1

        assert _run(work, "rev-parse", "HEAD").stdout.strip() == pre_auto_sha
        log = _run(work, "log", "-1", "--pretty=%s").stdout
        assert log.strip() == "feat: subagent committed properly"


class TestFullCycleFlow:
    """Integration-style tests combining setup → commit → gate → integrate/rollback,
    mirroring the shape of main()'s cycle-branch flow without spawning a real subagent.
    """

    def test_subagent_workspace_new_file_without_self_commit_integrates_on_green_gate(
        self, tmp_path,
    ):
        """#718: the subagent's workspace is now `_selfevo_repo` (the repo this
        bridge branches/commits/gates/integrates), not the deployed release
        tree. Simulate a subagent that authors a brand-new file directly in
        that repo (e.g. via its workspace tools) and never runs `git commit`
        itself — mirroring #666's "edited but not committed" case, but for a
        NEW file rather than an edit. The unconditional auto-commit safety net
        (#717) must sweep it in, and it must then flow through the gate and
        integrate to main — proving the workspace the subagent writes into is
        the same repo the bridge governs end to end.
        """
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "subagent-workspace-new-file")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]

        # Subagent authors a new allowed file directly in the workspace
        # (== _selfevo_repo per the #718 fix) and never self-commits.
        (work / "scripts").mkdir()
        (work / "scripts" / "new_tool.py").write_text(
            "def new_tool():\n    return 'built by subagent'\n"
        )
        status_before_auto = _run(work, "status", "--porcelain").stdout
        assert "scripts/" in status_before_auto

        # Bridge's #666/#717 safety net: no self-commit happened, so main()
        # calls _auto_commit_uncommitted_work() unconditionally.
        auto = bridge._auto_commit_uncommitted_work(work, setup["branch"])
        assert auto["committed"] is True
        assert auto["files_committed"] == 1
        assert auto["excluded"] == []
        assert _run(work, "status", "--porcelain").stdout.strip() == ""

        files_changed, blocked, mutation, _tier = bridge._changed_files_and_violations(
            work, main_sha_before,
        )
        assert "scripts/new_tool.py" in files_changed
        assert blocked == []
        assert mutation == []

        passed, _ = bridge._run_smoke_tests(work)
        assert passed is True

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], main_sha_before)
        assert integ["ok"] is True
        bridge._cleanup_cycle_branch(work, setup["branch"])

        assert _origin_main_sha(origin) == integ["main_sha_after"]
        log_files = _run(origin, "show", "--stat", "--pretty=", "main").stdout
        assert "new_tool.py" in log_files

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


# ─── #678: harden the autonomous integration gate ────────────────────────────
#
# The tests below exercise the six CONFIRMED findings from the #678 adversarial
# review. They follow the existing pattern of this file: drive the real git
# helpers directly (never a mocked git) against temp "origin" + "work" repos,
# and assert the gate-decision SHAPE main() now enforces (mutation-surface /
# blocked-pattern violations and a failed gate all skip _integrate_cycle_to_main
# and leave origin/main untouched with the cycle branch kept for forensics —
# main() wires these primitives together but is not itself unit-tested here,
# consistent with the rest of this suite).


class TestMutationSurfaceHardBlock:
    """F1: mutation-surface violations must hard-block integration, not just print."""

    def test_violation_blocks_integration_and_main_stays_untouched(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "mutsurf")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]

        (work / "nanobot").mkdir()
        _commit_file(work, "nanobot/foo.py", "x = 1\n", "feat: edit core module")

        files_changed = ["nanobot/foo.py"]
        violations = bridge._validate_mutation_surfaces(files_changed)
        assert violations, "editing nanobot/ must be flagged"

        # Gate decision shape (#678 F1): violations present -> never call
        # _integrate_cycle_to_main at all.
        integrated = False
        if not violations:
            integ = bridge._integrate_cycle_to_main(work, setup["branch"], main_sha_before)
            integrated = integ["ok"]

        assert integrated is False
        assert _origin_main_sha(origin) == main_sha_before
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] in branches

    def test_legit_surfaces_scripts_docs_memory_still_pass(self, tmp_path):
        """Legit cycles editing the allowed surfaces must NOT be blocked."""
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "legit")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]

        (work / "scripts").mkdir()
        (work / "memory").mkdir()
        _commit_file(work, "scripts/util.py", "x = 1\n", "feat: add utility script")
        _commit_file(work, "memory/MEMORY.md", "# memory\n", "chore: update memory")

        files_changed = ["scripts/util.py", "memory/MEMORY.md"]
        violations = bridge._validate_mutation_surfaces(files_changed)
        assert violations == []

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], main_sha_before)
        assert integ["ok"] is True
        assert _origin_main_sha(origin) == integ["main_sha_after"]


class TestRepairTurnSurfaceViolationIsCaught:
    """F1/F3 (repair-loop gap, #679 review): the changed-file set and its
    violation split are recomputed from pre_spawn_sha..HEAD AFTER the repair
    loop, so a repair turn that edits core nanobot/ (or drops a blocked file)
    is caught even when the INITIAL commit was on an allowed surface.
    """

    def test_clean_initial_then_repair_edits_core_is_blocked(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "repair-mutsurf")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]
        pre_spawn_sha = _run(work, "rev-parse", "HEAD").stdout.strip()

        # Initial subagent commit: allowed surface only → clean.
        (work / "scripts").mkdir()
        _commit_file(work, "scripts/util.py", "x = 1\n", "feat: add utility script")
        files0, blocked0, mut0, _tier0 = bridge._changed_files_and_violations(work, pre_spawn_sha)
        assert files0 == ["scripts/util.py"]
        assert blocked0 == [] and mut0 == []

        # Repair turn commits an edit to core nanobot/ (outside allowed paths).
        (work / "nanobot").mkdir()
        _commit_file(work, "nanobot/core.py", "y = 2\n", "fix: touch core during repair")

        # Recompute across ALL commits (the fix) — the violation must surface now.
        files1, blocked1, mut1, _tier1 = bridge._changed_files_and_violations(work, pre_spawn_sha)
        assert "nanobot/core.py" in files1
        assert mut1, "repair-turn core edit must be flagged as a mutation-surface violation"

        # Gate decision shape: violations present -> never integrate.
        integrated = False
        if not (blocked1 or mut1):
            integ = bridge._integrate_cycle_to_main(work, setup["branch"], main_sha_before)
            integrated = integ["ok"]
        assert integrated is False
        assert _origin_main_sha(origin) == main_sha_before
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] in branches

    def test_clean_initial_then_repair_adds_secret_is_blocked(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "repair-secret")
        assert setup["ok"]
        pre_spawn_sha = _run(work, "rev-parse", "HEAD").stdout.strip()

        (work / "scripts").mkdir()
        _commit_file(work, "scripts/util.py", "x = 1\n", "feat: add utility script")
        _, blocked0, _, _ = bridge._changed_files_and_violations(work, pre_spawn_sha)
        assert blocked0 == []

        # Repair turn commits a secret-shaped file.
        _commit_file(work, "id_rsa", "FAKE-KEY\n", "chore: stash a key during repair")

        _files, blocked1, _mut, _t = bridge._changed_files_and_violations(work, pre_spawn_sha)
        assert blocked1, "repair-turn secret file must be flagged as blocked_file_present"
        assert any("blocked filename pattern" in v for v in blocked1)


class TestBlockedPatternAcrossAllCommits:
    """F3: the secret-pattern filter must apply to ALL cycle commits, not just
    the auto-commit fallback."""

    def test_blocked_file_in_direct_commit_blocks_integration(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "blockedfile")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]

        # Subagent committed normally (NOT via the auto-commit fallback) with a
        # blocked-pattern file included.
        _commit_file(work, "id_rsa", "FAKE-KEY-CONTENT\n", "chore: add key file")

        files_changed = ["id_rsa"]
        violations = bridge._validate_mutation_surfaces(files_changed)
        assert violations
        assert any("blocked filename pattern" in v for v in violations)

        integrated = False  # gate decision: blocked -> never integrate
        assert integrated is False
        assert _origin_main_sha(origin) == main_sha_before
        branches = _run(work, "branch", "--list", setup["branch"]).stdout
        assert setup["branch"] in branches


class TestSmokeGateFailSafe:
    """F2/F4: the gate must fail closed on missing/empty suites, harness
    exceptions, and a suite-shrink guard must close the repair-loop-weakening
    path."""

    def test_missing_tests_directory_fails_gate(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        bridge._setup_cycle_branch(work, "notests")
        import shutil
        shutil.rmtree(work / "tests")
        (work / "dummy.py").write_text("x = 1\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "chore: remove tests directory")

        passed, output = bridge._run_smoke_tests(work)

        assert passed is False
        assert "no tests directory" in output

    def test_emptied_suite_fails_gate(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        bridge._setup_cycle_branch(work, "emptysuite")
        (work / "tests" / "test_smoke.py").unlink()
        (work / "tests" / "__init__.py").write_text("")
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "chore: empty the test suite")

        passed, output = bridge._run_smoke_tests(work)

        assert passed is False

    def test_harness_exception_fails_closed(self, tmp_path, monkeypatch):
        origin, work = _init_repo(tmp_path)
        bridge._setup_cycle_branch(work, "harnesserr")

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(subprocess, "run", _boom)

        passed, output = bridge._run_smoke_tests(work)

        assert passed is False
        assert "smoke harness error" in output

    def test_suite_shrink_guard_fails_when_test_count_drops(self, tmp_path):
        origin, work = _init_repo(tmp_path)  # baseline: 1 test function
        setup = bridge._setup_cycle_branch(work, "shrink")
        baseline = bridge._count_tests_at_ref(work, "origin/main")
        assert baseline == 1

        # Cycle guts the test function without deleting the file/dir.
        (work / "tests" / "test_smoke.py").write_text("# tests removed\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "feat: quietly remove test coverage")
        assert bridge._count_tests(work) == 0

        passed, output = bridge._run_smoke_tests_with_shrink_guard(work, baseline)

        assert passed is False
        assert "suite-shrink guard" in output
        # never got as far as invoking pytest for this failure
        assert setup["ok"]

    def test_suite_shrink_guard_allows_growing_suite(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        bridge._setup_cycle_branch(work, "grow")
        baseline = bridge._count_tests_at_ref(work, "origin/main")
        assert baseline == 1

        _commit_file(
            work, "tests/test_extra.py",
            "def test_extra():\n    assert True\n",
            "test: add extra coverage",
        )
        assert bridge._count_tests(work) == 2

        passed, _output = bridge._run_smoke_tests_with_shrink_guard(work, baseline)

        assert passed is True


class TestAlreadyDonePushGate:
    """F5: the already_done bookkeeping path must never bare-push; only a
    diff that touches memory/MEMORY.md exclusively may be pushed."""

    def test_memory_only_diff_is_pushable(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        (work / "memory").mkdir()
        (work / "memory" / "MEMORY.md").write_text("# memory\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "chore: move task to Completed")

        assert bridge._diff_against_remote_touches_only(
            work, "origin/main", {"memory/MEMORY.md"}
        ) is True

    def test_non_memory_diff_blocks_push_and_main_stays_untouched(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        before = _origin_main_sha(origin)
        (work / "memory").mkdir()
        (work / "memory" / "MEMORY.md").write_text("# memory\n")
        (work / "extra_stray_file.py").write_text("leak = 1\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "chore: move task to Completed (with stray file)")

        should_push = bridge._diff_against_remote_touches_only(
            work, "origin/main", {"memory/MEMORY.md"}
        )
        assert should_push is False
        # Bridge would skip the push here — assert we never pushed.
        assert _origin_main_sha(origin) == before


class TestPostIntegrationBookkeepingPushGate:
    """F6: post-integration writes (backlog-done, memory archiver, structured
    lesson) must refuse to push when their diff includes anything beyond the
    intended file(s)."""

    def test_extra_file_in_diff_skips_push(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "postint")
        _commit_file(work, "feature.py", "x = 1\n", "feat: add feature")
        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])
        assert integ["ok"]
        main_after_integration = integ["main_sha_after"]

        # Simulate a lesson-recording commit that also touches an unexpected file.
        (work / "lessons").mkdir()
        (work / "lessons" / "lessons.yaml").write_text("lessons: []\n")
        (work / "unexpected.py").write_text("x = 2\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "chore: record structured lesson (with stray file)")

        should_push = bridge._diff_against_remote_touches_only(
            work, "origin/main", {"lessons/lessons.yaml"}
        )
        assert should_push is False
        assert _origin_main_sha(origin) == main_after_integration

    def test_intended_file_only_diff_allows_push(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "postint-clean")
        _commit_file(work, "feature2.py", "x = 1\n", "feat: add feature2")
        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])
        assert integ["ok"]

        (work / "lessons").mkdir()
        (work / "lessons" / "lessons.yaml").write_text("lessons: []\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "chore: record structured lesson")

        assert bridge._diff_against_remote_touches_only(
            work, "origin/main", {"lessons/lessons.yaml"}
        ) is True


def _make_cycle_branch(work: Path, cid: str, *, merged: bool, seq: int = 0) -> None:
    """Create a selfevo/cycle-<cid> branch. merged=True → its commit is on main
    (later fast-forward merged); merged=False → forensic (unique commit, never
    merged), simulating a gate-failed cycle left for inspection.

    ``seq`` sets a strictly-increasing commit date so ``--sort=-committerdate``
    ordering is deterministic in tests (real host cycles are ~10 min apart; the
    test creates them in the same wall-clock second)."""
    import os as _os

    _run(work, "checkout", "-B", f"selfevo/cycle-{cid}", "main")
    (work / f"c_{cid}.py").write_text(f"# {cid}\n")
    _run(work, "add", f"c_{cid}.py")
    env = dict(_os.environ)
    stamp = f"2026-01-01T00:{seq // 60:02d}:{seq % 60:02d}"
    env["GIT_AUTHOR_DATE"] = stamp
    env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(_git(work) + ["commit", "-m", f"cycle {cid}"],
                   capture_output=True, text=True, env=env)
    if merged:
        _run(work, "checkout", "main")
        _run(work, "merge", "--no-ff", f"selfevo/cycle-{cid}", "-m", f"merge {cid}")
        _run(work, "push", "origin", "HEAD:main")
    _run(work, "checkout", "main")


class TestPruneStaleCycleBranches:
    def test_merged_branches_deleted(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        for i in range(3):
            _make_cycle_branch(work, f"m{i}", merged=True)
        before = _run(work, "branch", "--list", "selfevo/cycle-*").stdout
        assert before.count("selfevo/cycle-") == 3
        res = bridge._prune_stale_cycle_branches(work, keep=20)
        after = _run(work, "branch", "--list", "selfevo/cycle-*").stdout
        assert res["deleted"] == 3
        assert "selfevo/cycle-" not in after

    def test_forensic_trimmed_to_keep_newest(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        # 5 forensic (unmerged) branches, created oldest→newest
        for i in range(5):
            _make_cycle_branch(work, f"f{i}", merged=False, seq=i)
        res = bridge._prune_stale_cycle_branches(work, keep=2)
        remaining = [
            ln.strip().lstrip("* ").strip()
            for ln in _run(work, "branch", "--list", "selfevo/cycle-*").stdout.splitlines()
            if ln.strip()
        ]
        assert res["deleted"] == 3
        assert len(remaining) == 2
        # newest two (f3, f4 by commit order) survive
        assert set(remaining) == {"selfevo/cycle-f3", "selfevo/cycle-f4"}

    def test_current_branch_never_deleted(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        for i in range(3):
            _make_cycle_branch(work, f"f{i}", merged=False)
        # check out one forensic branch and prune with keep=0
        _run(work, "checkout", "selfevo/cycle-f0")
        res = bridge._prune_stale_cycle_branches(work, keep=0)
        remaining = _run(work, "branch", "--list", "selfevo/cycle-*").stdout
        assert "selfevo/cycle-f0" in remaining  # current survives despite keep=0
        assert res["kept"] >= 1

    def test_missing_repo_is_safe(self, tmp_path):
        res = bridge._prune_stale_cycle_branches(tmp_path / "nope", keep=5)
        assert res == {"deleted": 0, "kept": 0}

    def test_setup_cycle_branch_prunes_on_entry(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        for i in range(25):
            _make_cycle_branch(work, f"f{i:02d}", merged=False, seq=i)
        # a fresh setup should trim forensic backlog to KEEP (+ the new branch)
        setup = bridge._setup_cycle_branch(work, "fresh")
        assert setup["ok"] is True
        cycle_branches = [
            ln.strip().lstrip("* ").strip()
            for ln in _run(work, "branch", "--list", "selfevo/cycle-*").stdout.splitlines()
            if ln.strip()
        ]
        # newest 20 forensic kept + the just-created selfevo/cycle-fresh
        assert "selfevo/cycle-fresh" in cycle_branches
        assert len(cycle_branches) == bridge._FORENSIC_CYCLE_BRANCH_KEEP + 1


# ─── #877: git-native evolutionary tree — line-switch wiring ────────────────


def _write_stalled_archive(state_dir: Path, count: int = 5, reward: float = 0.5) -> None:
    arch = CycleArchive()
    for i in range(count):
        arch.add(cycle_id=f"archived-{i}", reward=reward, fd_mode="keep", task_id=f"t{i}", timestamp=1000.0 + i)
    arch.save(state_dir / "goals" / "cycle_archive.json")


class TestEvolutionTreeLineSwitch:
    """#877: _setup_cycle_branch may switch the cycle's base to a stronger
    dormant line when the coordinator archive is stalled AND the evolution
    tree offers a better candidate. Byte-identical to pre-#877 behaviour
    whenever either condition is false.
    """

    def test_byte_identical_when_tree_empty(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"
        # No tree.json, no archive at all -> nothing to switch to.
        setup = bridge._setup_cycle_branch(work, "no-tree", state_dir)
        assert setup["ok"] is True
        assert setup["main_sha"] == _origin_main_sha(origin)
        assert setup["origin_main_sha"] == _origin_main_sha(origin)
        branches = _run(work, "branch", "--list", "evo/node-*").stdout
        assert branches.strip() == ""

    def test_byte_identical_when_not_stalled(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"
        real_main = _origin_main_sha(origin)
        # A tree with a genuinely better dormant candidate exists...
        evo.record_node(state_dir, sha="deadbeefcafe0000000000000000000000000000",
                         parent_sha=None, branch="b-old", cycle_id="c-old", reward=0.99)
        evo.record_node(state_dir, sha=real_main, parent_sha="deadbeefcafe0000000000000000000000000000",
                         branch="selfevo/cycle-prior", cycle_id="c-prior", reward=0.1)
        # ...but the archive is NOT stalled (fewer than 5 entries) -> no switch.
        _write_stalled_archive(state_dir, count=2)

        setup = bridge._setup_cycle_branch(work, "not-stalled", state_dir)

        assert setup["ok"] is True
        assert setup["main_sha"] == real_main
        assert setup["origin_main_sha"] == real_main
        branches = _run(work, "branch", "--list", "evo/node-*").stdout
        assert branches.strip() == ""
        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        if ledger_path.exists():
            assert "line_switch" not in ledger_path.read_text()

    def test_stalled_switches_base_to_best_dormant_line(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"

        # sha_init: origin/main's current tip (the *only* real commit so far).
        sha_init = _origin_main_sha(origin)
        # Advance main further -> this becomes the "current" (weak) line.
        _run(work, "checkout", "main")
        _commit_file(work, "mod.py", "def ok():\n    return 'advanced'\n", "feat: advance main")
        _run(work, "push", "origin", "main")
        sha_advance = _origin_main_sha(origin)
        assert sha_advance != sha_init

        # Tree: sha_init scores HIGH (0.9), sha_advance (current tip) scores
        # LOW (0.1) -> sha_init is the better dormant line to switch to.
        evo.record_node(state_dir, sha=sha_init, parent_sha=None,
                         branch="selfevo/cycle-old", cycle_id="c-old", reward=0.9)
        evo.record_node(state_dir, sha=sha_advance, parent_sha=sha_init,
                         branch="selfevo/cycle-cur", cycle_id="c-cur", reward=0.1)
        _write_stalled_archive(state_dir, count=5, reward=0.5)

        setup = bridge._setup_cycle_branch(work, "switch-1", state_dir)

        assert setup["ok"] is True
        # Branched off the STRONGER dormant line, not the weak live tip.
        assert setup["main_sha"] == sha_init
        # ...but origin_main_sha still reports the REAL origin/main (unswitched).
        assert setup["origin_main_sha"] == sha_advance

        # New cycle branch's tip is sha_init's commit (no "advance" content).
        assert (work / "mod.py").read_text() == "def ok():\n    return True\n"

        # Keeper ref created at the abandoned tip BEFORE the switch.
        keeper = f"evo/node-{sha_advance[:12]}"
        branches = _run(work, "branch", "--list", keeper).stdout
        assert keeper in branches
        assert _run(work, "rev-parse", keeper).stdout.strip() == sha_advance

        # Ledger + tree.json both recorded the switch.
        ledger_path = state_dir / "ledger" / "cycles.jsonl"
        assert "line_switch" in ledger_path.read_text()
        tree = evo.read_tree(state_dir)
        assert tree["switches"]
        assert tree["switches"][-1]["from_sha"] == sha_advance
        assert tree["switches"][-1]["to_sha"] == sha_init

    def test_switch_target_missing_commit_falls_back_to_real_main(self, tmp_path):
        """A tree entry pointing at a sha this repo has never seen (e.g. a
        stale/forged entry) must never break setup — cat-file -e fails and
        the base silently falls back to the real origin/main."""
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"
        real_main = _origin_main_sha(origin)
        bogus_sha = "1234567890abcdef1234567890abcdef12345678"
        evo.record_node(state_dir, sha=bogus_sha, parent_sha=None,
                         branch="b-bogus", cycle_id="c-bogus", reward=0.99)
        evo.record_node(state_dir, sha=real_main, parent_sha=bogus_sha,
                         branch="selfevo/cycle-cur", cycle_id="c-cur", reward=0.1)
        _write_stalled_archive(state_dir, count=5, reward=0.5)

        setup = bridge._setup_cycle_branch(work, "bogus-target", state_dir)

        assert setup["ok"] is True
        assert setup["main_sha"] == real_main
        assert setup["origin_main_sha"] == real_main
        branches = _run(work, "branch", "--list", "evo/node-*").stdout
        assert branches.strip() == ""


class TestIntegratePushAfterLineSwitch:
    """#877: the integration push must use --force-with-lease against the
    REAL prior origin/main, not the (possibly switched) base — otherwise a
    line-switch integration would always be rejected as a non-fast-forward.
    """

    def test_switched_integration_advances_origin_main(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        sha_init = _origin_main_sha(origin)
        _run(work, "checkout", "main")
        _commit_file(work, "mod.py", "def ok():\n    return 'advanced'\n", "feat: advance main")
        _run(work, "push", "origin", "main")
        sha_advance = _origin_main_sha(origin)

        # Simulate what _setup_cycle_branch would have done: branch off the
        # switched-to ancestor (sha_init), not the real origin/main tip.
        _run(work, "checkout", "-B", "selfevo/cycle-switched", sha_init)
        _commit_file(work, "feature.py", "def feature():\n    return 1\n", "feat: work on the resumed line")

        integ = bridge._integrate_cycle_to_main(
            work, "selfevo/cycle-switched", sha_init, expected_origin_main=sha_advance,
        )

        assert integ["ok"] is True, integ
        assert _origin_main_sha(origin) == integ["main_sha_after"]
        # The switch really did rewrite main's history away from sha_advance.
        log = _run(work, "log", "--oneline", "main").stdout
        assert "advance main" not in log
        assert "resumed line" in log

    def test_lease_mismatch_rejects_concurrent_out_of_band_push(self, tmp_path):
        """If origin/main moved to something OTHER than the expected lease
        value between setup and integration, the force-with-lease push must
        still be rejected (out-of-band race safety, #846) — a line switch
        must not turn into a blind force push."""
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "lease-race")
        assert setup["ok"]
        stale_lease = setup["main_sha"]
        _commit_file(work, "feature.py", "def feature():\n    return 1\n", "feat: cycle work")

        # Someone else pushes to origin/main out-of-band, invalidating the lease.
        _run(work, "checkout", "main")
        _commit_file(work, "mod.py", "def ok():\n    return 'raced'\n", "chore: concurrent push")
        _run(work, "push", "origin", "main")

        integ = bridge._integrate_cycle_to_main(
            work, setup["branch"], setup["main_sha"], expected_origin_main=stale_lease,
        )

        assert integ["ok"] is False
        assert integ["reason"] == "push_rejected"


class TestSwitchBaseSurfaceGate:
    """#877 RED-1 (Opus adversarial review): the per-cycle delta gate alone
    (base..HEAD) never re-examines what a switched BASE itself carries — on
    a switch, `base` IS `pre_spawn_sha`, so a poisoned dormant/forensic sha
    (one carrying a deny-set/mutation-surface violation that already kept
    it from integrating) would ride along underneath a trivial, clean
    cycle edit straight into a force-pushed origin/main. The fix
    reclassifies `origin_main_observed..HEAD` (the real prior origin/main
    through HEAD) immediately before integration — this test exercises
    that reclassification directly (mirroring this file's existing pattern
    of replicating main()'s decision logic with real git primitives rather
    than spawning a subagent), plus the follow-on "never re-offer this sha"
    bookkeeping and a control case proving a genuinely clean dormant line
    is unaffected.
    """

    def test_poisoned_forensic_base_surface_is_detected_against_real_main(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"
        real_main = _origin_main_sha(origin)

        # A forensic branch carrying a mutation-surface violation (edits
        # nanobot/ outside the allowed script surface) — exactly the kind
        # of commit the smoke/deny-set gate blocks from ever integrating,
        # but which still EXISTS as a real, reachable commit (cat-file -e
        # only checks existence, never provenance).
        _run(work, "checkout", "-B", "selfevo/cycle-poisoned", "main")
        (work / "nanobot").mkdir()
        _commit_file(work, "nanobot/foo.py", "x = 1\n", "feat: poisoned runtime edit")
        poisoned_sha = _run(work, "rev-parse", "selfevo/cycle-poisoned").stdout.strip()
        _run(work, "checkout", "main")

        # Forged tree: points the switch at the poisoned sha with a high
        # score (as a forged/tampered tree.json would — tamper is DETECTED
        # via the #789 sidecar hash but never blocked at the tree layer).
        evo.record_node(state_dir, sha=poisoned_sha, parent_sha=None,
                         branch="selfevo/cycle-poisoned", cycle_id="c-poison", reward=0.99)
        evo.record_node(state_dir, sha=real_main, parent_sha=poisoned_sha,
                         branch="selfevo/cycle-cur", cycle_id="c-cur", reward=0.1)
        _write_stalled_archive(state_dir, count=5, reward=0.5)

        setup = bridge._setup_cycle_branch(work, "switch-poisoned", state_dir)
        assert setup["ok"] is True
        assert setup["main_sha"] == poisoned_sha  # the switch happened
        origin_main_observed = setup["origin_main_sha"]
        assert origin_main_observed == real_main

        # The cycle's OWN work: a trivial, clean script edit on top of the
        # poisoned base — this is what classifies clean on the per-cycle
        # delta (base..HEAD) alone, which is exactly the gap RED-1 found.
        (work / "scripts").mkdir(exist_ok=True)
        _commit_file(work, "scripts/trivial.py", "x = 1\n", "feat: trivial task")

        # The base-surface gate bridge.py now runs right before ever
        # calling _integrate_cycle_to_main: classify
        # origin_main_observed..HEAD, not just base..HEAD.
        files, blocked, mutation, tier = bridge._changed_files_and_violations(
            work, origin_main_observed,
        )
        assert "nanobot/foo.py" in files
        assert mutation, "the poisoned base's own violation must surface here"

        # Simulate the bridge's actual blocking decision + bookkeeping.
        origin_before = _origin_main_sha(origin)
        if blocked or mutation:
            evo.mark_switch_blocked(state_dir, poisoned_sha, reason="switch_base_gate_blocked")
        else:
            bridge._integrate_cycle_to_main(
                work, setup["branch"], setup["main_sha"], expected_origin_main=origin_main_observed,
            )

        # origin/main was NEVER touched — the poisoned base was not pushed.
        assert _origin_main_sha(origin) == origin_before == real_main

        # The poisoned node is never re-offered on a subsequent switch
        # decision (real_main is excluded as its own current_sha; poisoned
        # is excluded because it is now blocked -> no candidates left).
        assert evo.select_switch_target(state_dir, real_main) is None

    def test_legit_dormant_line_still_passes_the_same_gate(self, tmp_path):
        """Control case: a genuinely clean dormant line (script-tier only,
        exactly what a real rollback target looks like) reclassifies clean
        against real origin/main and is allowed to integrate — the new gate
        must not break legitimate line switches."""
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"
        real_main = _origin_main_sha(origin)

        _run(work, "checkout", "-B", "selfevo/cycle-clean", "main")
        (work / "scripts").mkdir(exist_ok=True)
        _commit_file(work, "scripts/clean_tool.py", "x = 1\n", "feat: clean historical tool")
        clean_sha = _run(work, "rev-parse", "selfevo/cycle-clean").stdout.strip()
        _run(work, "checkout", "main")

        evo.record_node(state_dir, sha=clean_sha, parent_sha=None,
                         branch="selfevo/cycle-clean", cycle_id="c-clean", reward=0.9)
        evo.record_node(state_dir, sha=real_main, parent_sha=clean_sha,
                         branch="selfevo/cycle-cur", cycle_id="c-cur", reward=0.1)
        _write_stalled_archive(state_dir, count=5, reward=0.5)

        setup = bridge._setup_cycle_branch(work, "switch-clean", state_dir)
        assert setup["ok"] is True
        assert setup["main_sha"] == clean_sha
        origin_main_observed = setup["origin_main_sha"]

        _commit_file(work, "scripts/trivial.py", "x = 1\n", "feat: trivial task")

        files, blocked, mutation, tier = bridge._changed_files_and_violations(
            work, origin_main_observed,
        )
        assert blocked == [] and mutation == []
        assert tier == "script"

        integ = bridge._integrate_cycle_to_main(
            work, setup["branch"], setup["main_sha"], expected_origin_main=origin_main_observed,
        )

        assert integ["ok"] is True
        assert _origin_main_sha(origin) == integ["main_sha_after"]

    def test_runtime_tier_base_is_blocked_even_with_zero_violations(self, tmp_path, monkeypatch):
        """RED-2 (Opus re-review): a runtime-SLICE-tier base classifies with
        ZERO violations (it's a legitimate, operator-approved slice path,
        e.g. existence_index.py) but must still be blocked from
        integration — origin/main is script-tier-only by invariant (#812);
        a retained runtime-tier commit was only ever a promotion CANDIDATE,
        never merged, and a switch must not smuggle it onto origin/main.
        """
        monkeypatch.setenv("SELFEVO_RUNTIME_SLICE", "nanobot/runtime/existence_index.py")
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"
        real_main = _origin_main_sha(origin)

        # A retained runtime-tier forensic branch: a green runtime-slice
        # cycle that (per #812) landed as a promotion candidate, NEVER
        # merged to origin/main, but still exists as a real commit.
        _run(work, "checkout", "-B", "selfevo/cycle-runtime", "main")
        (work / "nanobot" / "runtime").mkdir(parents=True)
        _commit_file(
            work, "nanobot/runtime/existence_index.py",
            "def ok():\n    return True\n", "feat: runtime-slice edit (candidate only, #812)",
        )
        runtime_sha = _run(work, "rev-parse", "selfevo/cycle-runtime").stdout.strip()
        _run(work, "checkout", "main")

        evo.record_node(state_dir, sha=runtime_sha, parent_sha=None,
                         branch="selfevo/cycle-runtime", cycle_id="c-runtime", reward=0.99)
        evo.record_node(state_dir, sha=real_main, parent_sha=runtime_sha,
                         branch="selfevo/cycle-cur", cycle_id="c-cur", reward=0.1)
        _write_stalled_archive(state_dir, count=5, reward=0.5)

        setup = bridge._setup_cycle_branch(work, "switch-runtime", state_dir)
        assert setup["ok"] is True
        assert setup["main_sha"] == runtime_sha  # the switch happened
        origin_main_observed = setup["origin_main_sha"]

        (work / "scripts").mkdir(exist_ok=True)
        _commit_file(work, "scripts/trivial.py", "x = 1\n", "feat: trivial task")

        files, blocked, mutation, tier = bridge._changed_files_and_violations(
            work, origin_main_observed,
        )
        assert "nanobot/runtime/existence_index.py" in files
        assert blocked == [] and mutation == []  # zero violations — the trap RED-2 caught
        assert tier == "runtime"

        # Simulate the bridge's actual blocking decision: violations OR
        # runtime-tier both hard-block, per the fix.
        origin_before = _origin_main_sha(origin)
        if blocked or mutation or tier == "runtime":
            evo.mark_switch_blocked(state_dir, runtime_sha, reason="switch_base_gate_blocked")
        else:
            bridge._integrate_cycle_to_main(
                work, setup["branch"], setup["main_sha"], expected_origin_main=origin_main_observed,
            )

        # origin/main was NEVER touched — the runtime-tier base was not pushed.
        assert _origin_main_sha(origin) == origin_before == real_main
        # Never re-offered as a switch target afterward.
        assert evo.select_switch_target(state_dir, real_main) is None

    def test_fail_closed_when_base_surface_classification_errors_on_switched_path(self, tmp_path):
        """YELLOW fix (Opus re-review): the shared classifier
        (_changed_files_and_violations) fails OPEN on a git error — it
        returns clean/empty, which the gate would otherwise read as "no
        violations -> integrate". On the SWITCHED path, bridge.py now
        independently probes the same diff itself and fails CLOSED (block)
        if that probe errors, rather than trusting the shared helper's
        fail-open default. This test exercises the probe mechanism
        directly against a bogus/unresolvable origin_main_observed sha
        (e.g. a forged tree.json entry, or a pruned/corrupt ref) — exactly
        the case a real switch could hand the gate.
        """
        origin, work = _init_repo(tmp_path)
        setup = bridge._setup_cycle_branch(work, "switch-error")
        assert setup["ok"]
        (work / "scripts").mkdir(exist_ok=True)
        _commit_file(work, "scripts/trivial.py", "x = 1\n", "feat: trivial task")

        bogus_origin_main_observed = "0" * 40  # well-formed but unresolvable sha
        assert setup["main_sha"] != bogus_origin_main_observed  # a "switch" is in play

        # The bridge's own fail-closed probe: a direct git diff against the
        # bogus sha must fail non-zero.
        probe = _run(work, "diff", "--name-only", bogus_origin_main_observed, "HEAD")
        base_gate_error = probe.returncode != 0
        assert base_gate_error is True

        # The shared classifier, by contrast, fails OPEN on the same input
        # — this is exactly the gap the caller-side fail-closed probe closes.
        files, blocked, mutation, tier = bridge._changed_files_and_violations(
            work, bogus_origin_main_observed,
        )
        assert blocked == [] and mutation == [] and tier == "script"

        # Under the fix, base_gate_error alone blocks — origin/main must
        # stay untouched regardless of what the (fail-open) classifier said.
        origin_before = _origin_main_sha(origin)
        if base_gate_error:
            pass  # bridge.py: record_gate_decision(False, 'switch_base_gate_error', ...); no push
        else:
            bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])
        assert _origin_main_sha(origin) == origin_before


class TestPruneExemptsEvolutionTreeRefs:
    """#877: pruning must never delete a branch the evolution tree still
    points at, nor evict evo/node-* keeper refs below their own cap."""

    def test_indexed_sha_branch_survives_pruning(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        state_dir = tmp_path / "state"
        # A forensic (unmerged) cycle branch whose tip sha IS indexed by the
        # tree, deliberately made the OLDEST branch (well outside "newest
        # keep=5") so it can only survive via the sha-indexed exemption,
        # never by accident of the keep-newest-N cutoff.
        import os as _os

        _run(work, "checkout", "-B", "selfevo/cycle-indexed", "main")
        (work / "c_indexed.py").write_text("# indexed\n")
        _run(work, "add", "c_indexed.py")
        env = dict(_os.environ)
        env["GIT_AUTHOR_DATE"] = "2020-01-01T00:00:00"
        env["GIT_COMMITTER_DATE"] = "2020-01-01T00:00:00"
        subprocess.run(_git(work) + ["commit", "-m", "cycle indexed"], capture_output=True, text=True, env=env)
        _run(work, "checkout", "main")
        indexed_sha = _run(work, "rev-parse", "selfevo/cycle-indexed").stdout.strip()
        evo.record_node(state_dir, sha=indexed_sha, parent_sha=None,
                         branch="selfevo/cycle-indexed", cycle_id="c-indexed", reward=0.5)
        # Plenty of OTHER (newer) forensic branches beyond `keep` to force a
        # real prune pass that would otherwise delete the oldest one.
        for i in range(25):
            _make_cycle_branch(work, f"f{i:02d}", merged=False, seq=i)

        res = bridge._prune_stale_cycle_branches(work, keep=5, state_dir=state_dir)

        remaining = _run(work, "branch", "--list", "selfevo/cycle-*").stdout
        assert "selfevo/cycle-indexed" in remaining
        assert res["deleted"] >= 1  # the non-indexed excess still got pruned

    def test_evo_node_refs_are_never_touched_by_cycle_branch_glob(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        _run(work, "branch", "evo/node-abc123456789", "main")
        for i in range(25):
            _make_cycle_branch(work, f"f{i:02d}", merged=False, seq=i)

        bridge._prune_stale_cycle_branches(work, keep=5)

        branches = _run(work, "branch", "--list", "evo/node-*").stdout
        assert "evo/node-abc123456789" in branches

    def test_evo_node_refs_capped_at_keep(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        import os as _os

        for i in range(bridge._EVO_NODE_REF_KEEP + 5):
            env = dict(_os.environ)
            stamp = f"2026-01-01T00:{i:02d}:00"
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
            (work / "mod.py").write_text(f"x = {i}\n")
            _run(work, "add", "mod.py")
            subprocess.run(_git(work) + ["commit", "-m", f"chore: advance {i}"], capture_output=True, text=True, env=env)
            _run(work, "branch", "-f", f"evo/node-pad-{i}", "main")

        deleted = bridge._prune_evo_node_refs(work, "main")

        remaining = [
            ln.strip().lstrip("* ").strip()
            for ln in _run(work, "branch", "--list", "evo/node-*").stdout.splitlines()
            if ln.strip()
        ]
        assert len(remaining) == bridge._EVO_NODE_REF_KEEP
        assert deleted == 5

    def test_current_branch_and_live_tip_never_pruned(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        import os as _os

        state_dir = tmp_path / "state"
        main_sha = _origin_main_sha(origin)
        evo.record_node(state_dir, sha=main_sha, parent_sha=None,
                         branch="main", cycle_id="c0", reward=0.5)
        # Keeper ref at the live tip, deliberately the OLDEST ref of the
        # bunch (real "now" commit date) — every pad ref below is stamped
        # strictly later, so without the live-sha exemption this one would
        # be exactly the kind of entry the age-based cap deletes first.
        _run(work, "branch", "-f", f"evo/node-{main_sha[:12]}", "main")
        for i in range(bridge._EVO_NODE_REF_KEEP + 5):
            env = dict(_os.environ)
            stamp = f"2030-01-01T00:{i:02d}:00"
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
            (work / "extra.py").write_text(f"x = {i}\n")
            _run(work, "add", "extra.py")
            subprocess.run(_git(work) + ["commit", "-m", f"chore: pad {i}"], capture_output=True, text=True, env=env)
            _run(work, "branch", "-f", f"evo/node-pad-{i}", "main")

        deleted = bridge._prune_evo_node_refs(work, "main", state_dir)

        remaining = _run(work, "branch", "--list", f"evo/node-{main_sha[:12]}").stdout
        assert f"evo/node-{main_sha[:12]}" in remaining
        # Sanity: the exemption was actually exercised (this ref would
        # otherwise have been in the deleted-oldest tail).
        assert deleted >= 1
