"""Tests for #1119: deterministic test-weakening detector (nanobot.runtime.test_guard).

Real git repos, mirroring the fixture style of tests/test_bounded_gate.py /
tests/test_bridge_cycle_branch.py — no mocked subprocess for the git-diff
plumbing itself.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime import test_guard


def _git(repo: Path) -> list[str]:
    return ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(_git(repo) + list(args), capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _run(work, "init", "-q", "--initial-branch=main")
    _run(work, "config", "user.email", "guard@test.local")
    _run(work, "config", "user.name", "guard-test")
    (work / "tests").mkdir()
    (work / "tests" / "test_seed.py").write_text("def test_seed():\n    assert True\n")
    _run(work, "add", "-A")
    _run(work, "commit", "-q", "-m", "init")
    return work


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _commit(repo: Path, message: str) -> None:
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", message)


class TestNoChanges:
    def test_empty_diff_is_clean(self, tmp_path):
        work = _init_repo(tmp_path)
        verdict = test_guard.evaluate(work, "HEAD", "HEAD")
        assert verdict == {"blocked": False, "hard_violations": [], "soft_signals": []}

    def test_new_test_file_never_penalized(self, tmp_path):
        work = _init_repo(tmp_path)
        base = _run(work, "rev-parse", "HEAD").stdout.strip()
        _write(work, "tests/test_new.py", "def test_new():\n    assert 1 == 1\n")
        _commit(work, "add new test")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False
        assert verdict["hard_violations"] == []

    def test_new_assert_rich_file_never_flagged(self, tmp_path):
        work = _init_repo(tmp_path)
        base = _run(work, "rev-parse", "HEAD").stdout.strip()
        _write(
            work,
            "tests/test_rich.py",
            "def test_rich():\n    assert True\n    assert True\n    assert True\n",
        )
        _commit(work, "rich new test file")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False


class TestHardSignalDeletion:
    def test_deleted_test_file_with_non_test_change_blocks(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(work, "tests/test_victim.py", "def test_victim():\n    assert True\n")
        _write(work, "scripts/thing.py", "def thing():\n    return 1\n")
        _commit(work, "seed victim + script")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        (work / "tests" / "test_victim.py").unlink()
        _write(work, "scripts/thing.py", "def thing():\n    return 2\n")
        _commit(work, "delete victim test, edit script")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is True
        assert any("test_victim.py" in v for v in verdict["hard_violations"])

    def test_deleted_test_file_pure_test_only_commit_still_blocks(self, tmp_path):
        # Non-test-change scoping only exempts RENAMES (see below), not a
        # bare test-only deletion with no equivalent coverage anywhere else —
        # that is still a straightforward test-weakening deletion.
        work = _init_repo(tmp_path)
        _write(work, "tests/test_victim.py", "def test_victim():\n    assert True\n")
        _commit(work, "seed victim")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        (work / "tests" / "test_victim.py").unlink()
        _commit(work, "delete victim test only")

        verdict = test_guard.evaluate(work, base, "HEAD")

        # any_non_test_change is False here (only a test file touched), so
        # per spec this is NOT flagged as the "deleted alongside non-test
        # changes" hard signal — deliberately scoped narrowly per the issue.
        assert verdict["blocked"] is False

    def test_renamed_test_file_equivalent_coverage_not_blocked(self, tmp_path):
        work = _init_repo(tmp_path)
        body = (
            "import pytest\n\n\n"
            "def test_alpha():\n    assert 1 == 1\n\n\n"
            "def test_beta():\n    assert 2 == 2\n\n\n"
            "def test_gamma():\n    with pytest.raises(ValueError):\n        raise ValueError()\n"
        )
        _write(work, "tests/test_old_name.py", body)
        _write(work, "scripts/thing.py", "def thing():\n    return 1\n")
        _commit(work, "seed renamed-candidate + script")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        (work / "tests" / "test_old_name.py").unlink()
        _write(work, "tests/test_new_name.py", body)
        _write(work, "scripts/thing.py", "def thing():\n    return 2\n")
        _commit(work, "rename test file, edit script")

        status = test_guard._diff_status(work, base, "HEAD")
        assert any(row[0] == "R" for row in status), f"expected git to detect a rename: {status}"

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False
        assert verdict["hard_violations"] == []


class TestHardSignalAssertLoss:
    def test_removed_asserts_in_existing_file_blocks(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(
            work,
            "tests/test_math.py",
            "def test_math():\n    assert 1 == 1\n    assert 2 == 2\n",
        )
        _commit(work, "seed math test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(work, "tests/test_math.py", "def test_math():\n    assert 1 == 1\n")
        _commit(work, "weaken math test")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is True
        assert any("test_math.py" in v for v in verdict["hard_violations"])

    def test_removed_pytest_raises_blocks(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(
            work,
            "tests/test_err.py",
            "import pytest\n\n\n"
            "def test_err():\n    with pytest.raises(ValueError):\n        raise ValueError()\n",
        )
        _commit(work, "seed err test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(
            work,
            "tests/test_err.py",
            "def test_err():\n    pass\n",
        )
        _commit(work, "remove raises check")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is True

    def test_added_asserts_never_blocks(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(work, "tests/test_math.py", "def test_math():\n    assert 1 == 1\n")
        _commit(work, "seed math test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(
            work,
            "tests/test_math.py",
            "def test_math():\n    assert 1 == 1\n    assert 2 == 2\n",
        )
        _commit(work, "strengthen math test")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False

    def test_new_test_function_added_alongside_unrelated_edit_not_blocked(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(
            work,
            "tests/test_multi.py",
            "def test_one():\n    assert 1 == 1\n",
        )
        _commit(work, "seed multi test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(
            work,
            "tests/test_multi.py",
            "def test_one():\n    assert 1 == 1\n\n\ndef test_two():\n    assert 2 == 2\n",
        )
        _commit(work, "add second test function")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False


class TestHardSignalSkipMarker:
    def test_added_skip_marker_on_previously_unmarked_test_blocks(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(
            work,
            "tests/test_flaky.py",
            "def test_flaky():\n    assert True\n",
        )
        _commit(work, "seed flaky test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(
            work,
            "tests/test_flaky.py",
            "import pytest\n\n\n"
            "@pytest.mark.skip(reason='flaky')\n"
            "def test_flaky():\n    assert True\n",
        )
        _commit(work, "skip flaky test")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is True
        assert any("test_flaky.py" in v and "test_flaky" in v for v in verdict["hard_violations"])

    def test_added_xfail_marker_blocks(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(work, "tests/test_x.py", "def test_x():\n    assert True\n")
        _commit(work, "seed x test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(
            work,
            "tests/test_x.py",
            "import pytest\n\n\n@pytest.mark.xfail\ndef test_x():\n    assert True\n",
        )
        _commit(work, "xfail x test")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is True

    def test_preexisting_skip_marker_untouched_not_reblocked(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(
            work,
            "tests/test_marked.py",
            "import pytest\n\n\n"
            "@pytest.mark.skip(reason='known issue')\n"
            "def test_marked():\n    assert True\n",
        )
        _commit(work, "seed already-skipped test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(
            work,
            "tests/test_marked.py",
            "import pytest\n\n\n"
            "@pytest.mark.skip(reason='known issue, still true')\n"
            "def test_marked():\n    assert True\n",
        )
        _commit(work, "edit skip reason text only")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False


class TestSoftSignals:
    def test_shrunk_parametrize_recorded_not_blocked(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(
            work,
            "tests/test_param.py",
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2, 3, 4])\n"
            "def test_param(n):\n    assert n > 0\n",
        )
        _commit(work, "seed parametrized test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(
            work,
            "tests/test_param.py",
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\n"
            "def test_param(n):\n    assert n > 0\n",
        )
        _commit(work, "shrink parametrize list")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False
        assert any("parametrize" in s for s in verdict["soft_signals"])

    def test_grown_parametrize_not_recorded_as_shrink(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(
            work,
            "tests/test_param.py",
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\n"
            "def test_param(n):\n    assert n > 0\n",
        )
        _commit(work, "seed parametrized test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(
            work,
            "tests/test_param.py",
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2, 3, 4])\n"
            "def test_param(n):\n    assert n > 0\n",
        )
        _commit(work, "grow parametrize list")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["soft_signals"] == []

    def test_assert_loss_on_renamed_file_is_soft_not_hard(self, tmp_path):
        work = _init_repo(tmp_path)
        body_old = (
            "import pytest\n\n\n"
            "def test_alpha():\n    assert 1 == 1\n\n\n"
            "def test_beta():\n    assert 2 == 2\n\n\n"
            "def test_gamma():\n    assert 3 == 3\n\n\n"
            "def test_delta():\n    assert 4 == 4\n"
        )
        _write(work, "tests/test_old.py", body_old)
        _write(work, "scripts/thing.py", "def thing():\n    return 1\n")
        _commit(work, "seed renamed-candidate")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        # Rename + trim one function's body (still a majority-similar blob so
        # git's similarity heuristic still calls it a rename), plus the
        # unrelated script edit so any_non_test_change is True.
        body_new = (
            "import pytest\n\n\n"
            "def test_alpha():\n    assert 1 == 1\n\n\n"
            "def test_beta():\n    assert 2 == 2\n\n\n"
            "def test_gamma():\n    assert 3 == 3\n\n\n"
            "def test_delta():\n    pass\n"
        )
        (work / "tests" / "test_old.py").unlink()
        _write(work, "tests/test_new.py", body_new)
        _write(work, "scripts/thing.py", "def thing():\n    return 2\n")
        _commit(work, "rename + trim one assert")

        status = test_guard._diff_status(work, base, "HEAD")
        assert any(row[0] == "R" for row in status), f"expected a detected rename: {status}"

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False
        assert any("test_new.py" in s for s in verdict["soft_signals"])


class TestFailOpen:
    def test_bad_base_ref_fails_open(self, tmp_path):
        work = _init_repo(tmp_path)
        verdict = test_guard.evaluate(work, "not-a-real-ref", "HEAD")
        assert verdict == {"blocked": False, "hard_violations": [], "soft_signals": []}

    def test_nonexistent_repo_fails_open(self, tmp_path):
        verdict = test_guard.evaluate(tmp_path / "does-not-exist", "HEAD", "HEAD")
        assert verdict["blocked"] is False

    def test_syntax_error_in_new_blob_fails_open_for_that_file(self, tmp_path):
        work = _init_repo(tmp_path)
        _write(work, "tests/test_broken.py", "def test_broken():\n    assert True\n")
        _commit(work, "seed broken-candidate test")
        base = _run(work, "rev-parse", "HEAD").stdout.strip()

        _write(work, "tests/test_broken.py", "def test_broken(:\n    this is not python\n")
        _commit(work, "introduce syntax error")

        verdict = test_guard.evaluate(work, base, "HEAD")

        assert verdict["blocked"] is False


class TestHelpers:
    def test_is_test_path_matches_conventions(self):
        assert test_guard._is_test_path("tests/test_foo.py")
        assert test_guard._is_test_path("tests/sub/test_bar.py")
        assert test_guard._is_test_path("nanobot/thing_test.py")
        assert not test_guard._is_test_path("nanobot/runtime/bridge.py")
        assert not test_guard._is_test_path("scripts/foo.py")

    def test_collect_test_functions_ignores_non_test_functions(self):
        source = (
            "def helper():\n    assert True\n\n\n"
            "def test_real():\n    assert 1 == 1\n"
        )
        stats = test_guard._collect_test_functions(source)
        assert stats is not None
        assert "test_real" in stats
        assert "helper" not in stats

    def test_collect_test_functions_syntax_error_returns_none(self):
        assert test_guard._collect_test_functions("def broken(:\n") is None

    def test_file_parametrize_total_none_when_no_static_parametrize(self):
        source = "def test_plain():\n    assert True\n"
        assert test_guard._file_parametrize_total(source) is None
