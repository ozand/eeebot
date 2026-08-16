"""Tests for the #846 gate-audit backstops added to nanobot/runtime/bridge.py.

Backstop A: ``_detect_out_of_band_main`` — positive-only, fail-open detection
of an origin/main push that bypassed ``_integrate_cycle_to_main`` (the only
sanctioned way origin/main is supposed to advance during a cycle).

Backstop B: the suite-shrink guard (``_run_smoke_tests_with_shrink_guard``)
now also requires the baseline's test FUNCTION NAMES to be a subset of the
current tree's names, closing the "N real tests swapped for N stub tests"
hole that a count-only guard misses.

Follows the git-fixture style of tests/test_bridge_cycle_branch.py: real temp
git repos (a bare "origin" + a working clone), no mocked git.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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


class TestDetectOutOfBandMain:
    """Backstop A (#846 audit hole #1)."""

    def test_no_movement_returns_empty(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        main_sha_before = _origin_main_sha(origin)

        result = bridge._detect_out_of_band_main(work, main_sha_before)

        assert result == ""

    def test_out_of_band_push_is_detected(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        main_sha_before = _origin_main_sha(origin)

        # Simulate a subagent (or something) pushing directly to origin/main,
        # bypassing _integrate_cycle_to_main entirely.
        _run(work, "checkout", "main")
        _commit_file(work, "mod.py", "def ok():\n    return 'bypassed the gate'\n", "chore: direct push")
        _run(work, "push", "origin", "main")
        new_sha = _origin_main_sha(origin)
        assert new_sha != main_sha_before

        result = bridge._detect_out_of_band_main(work, main_sha_before)

        assert result != ""
        assert result == new_sha

    def test_empty_main_sha_before_fails_open(self, tmp_path):
        origin, work = _init_repo(tmp_path)

        result = bridge._detect_out_of_band_main(work, "")

        assert result == ""

    def test_non_git_dir_fails_open(self, tmp_path):
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()

        result = bridge._detect_out_of_band_main(not_a_repo, "deadbeef")

        assert result == ""

    def test_missing_dir_fails_open(self, tmp_path):
        missing = tmp_path / "does-not-exist"

        result = bridge._detect_out_of_band_main(missing, "deadbeef")

        assert result == ""


class TestTestFunctionNames:
    """Backstop B (#846 audit hole #3) — name-extraction helpers."""

    def test_extracts_names_from_working_tree(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        (work / "tests" / "test_smoke.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
        )

        names = bridge._test_function_names(work)

        assert names == {"test_a", "test_b"}

    def test_missing_tests_dir_returns_empty_set(self, tmp_path):
        empty = tmp_path / "no-tests"
        empty.mkdir()

        assert bridge._test_function_names(empty) == set()

    def test_extracts_names_at_ref(self, tmp_path):
        origin, work = _init_repo(tmp_path)
        (work / "tests" / "test_smoke.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "test: add test_a and test_b")
        _run(work, "push", "origin", "main")

        names = bridge._test_function_names_at_ref(work, "origin/main")

        assert names == {"test_a", "test_b"}

    def test_missing_ref_fails_open(self, tmp_path):
        origin, work = _init_repo(tmp_path)

        names = bridge._test_function_names_at_ref(work, "origin/does-not-exist")

        assert names == set()


class TestSuiteShrinkGuardNameSuperset:
    """Backstop B: guard blocks WHOLESALE gutting (majority of baseline test
    names swapped for stubs at a flat count) while tolerating a legitimate
    rename/small refactor (#846 rename-tolerant threshold)."""

    def test_wholesale_gutting_with_flat_count_is_blocked(self, tmp_path, monkeypatch):
        origin, work = _init_repo(tmp_path)
        # Baseline: four real tests.
        (work / "tests" / "test_smoke.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_b():\n    assert True\n\n\n"
            "def test_c():\n    assert True\n\n\n"
            "def test_d():\n    assert True\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "test: add test_a..test_d")
        _run(work, "push", "origin", "main")
        baseline_count = bridge._count_tests_at_ref(work, "origin/main")
        baseline_names = bridge._test_function_names_at_ref(work, "origin/main")
        assert baseline_count == 4
        assert baseline_names == {"test_a", "test_b", "test_c", "test_d"}

        # Cycle guts 3 of 4 real tests, replacing them with no-op stubs under
        # NEW names — count stays flat at 4 but the majority vanished.
        (work / "tests" / "test_smoke.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_w():\n    pass\n\n\n"
            "def test_x():\n    pass\n\n\n"
            "def test_y():\n    pass\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "feat: quietly gut most tests to stubs")
        assert bridge._count_tests(work) == 4  # count-only guard would NOT catch this

        passed, output = bridge._run_smoke_tests_with_shrink_guard(
            work, baseline_count, baseline_test_names=baseline_names,
        )

        assert passed is False
        assert "gutting" in output
        assert "test_b" in output

    def test_single_rename_is_tolerated(self, tmp_path, monkeypatch):
        origin, work = _init_repo(tmp_path)
        (work / "tests" / "test_smoke.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_b():\n    assert True\n\n\n"
            "def test_c():\n    assert True\n\n\n"
            "def test_d():\n    assert True\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "test: add test_a..test_d")
        _run(work, "push", "origin", "main")
        baseline_count = bridge._count_tests_at_ref(work, "origin/main")
        baseline_names = bridge._test_function_names_at_ref(work, "origin/main")

        # Cycle renames a single test (test_d -> test_renamed) — a legitimate
        # refactor. One baseline name vanishes; not > half → must NOT block.
        (work / "tests" / "test_smoke.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_b():\n    assert True\n\n\n"
            "def test_c():\n    assert True\n\n\n"
            "def test_renamed():\n    assert True\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "refactor: rename test_d")
        monkeypatch.setattr(bridge, "_run_smoke_tests", lambda *a, **k: (True, "ok"))

        passed, output = bridge._run_smoke_tests_with_shrink_guard(
            work, baseline_count, baseline_test_names=baseline_names,
        )

        assert passed is True
        assert output == "ok"

    def test_baseline_names_subset_of_current_falls_through(self, tmp_path, monkeypatch):
        origin, work = _init_repo(tmp_path)
        (work / "tests" / "test_smoke.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "test: add test_a and test_b")
        _run(work, "push", "origin", "main")
        baseline_count = bridge._count_tests_at_ref(work, "origin/main")
        baseline_names = bridge._test_function_names_at_ref(work, "origin/main")

        # Cycle adds a new test on top — baseline names remain a subset.
        (work / "tests" / "test_smoke.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_b():\n    assert True\n\n\n"
            "def test_c():\n    assert True\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "test: add test_c")

        # Isolate the guard from the actual pytest run.
        monkeypatch.setattr(bridge, "_run_smoke_tests", lambda *a, **k: (True, "ok"))

        passed, output = bridge._run_smoke_tests_with_shrink_guard(
            work, baseline_count, baseline_test_names=baseline_names,
        )

        assert passed is True
        assert output == "ok"

    def test_no_baseline_names_never_blocks(self, tmp_path, monkeypatch):
        origin, work = _init_repo(tmp_path)
        monkeypatch.setattr(bridge, "_run_smoke_tests", lambda *a, **k: (True, "ok"))

        passed, output = bridge._run_smoke_tests_with_shrink_guard(
            work, 0, baseline_test_names=None,
        )

        assert passed is True
        assert output == "ok"
