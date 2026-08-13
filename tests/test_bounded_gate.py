"""Tests for #686: bounded fast smoke gate.

Replaces "run all of tests/" with a targeted selection: import-smoke of
changed .py files, tests affected by what changed, plus a small fixed core
smoke set. Exercises ``_select_gate_tests`` directly and ``_run_smoke_tests``
end to end against temp repos, mirroring the fixture style of
tests/test_bridge_cycle_branch.py (real git repos, no mocked subprocess for
the integration-style cases).
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


def _init_repo(tmp_path: Path) -> Path:
    """Minimal working repo (no bare origin needed for these tests)."""
    work = tmp_path / "work"
    work.mkdir()
    _run(work, "init", "-q", "--initial-branch=main")
    _run(work, "config", "user.email", "bridge@test.local")
    _run(work, "config", "user.name", "bridge-test")
    (work / "tests").mkdir()
    (work / "tests" / "test_import_hygiene.py").write_text(
        "def test_hygiene():\n    assert True\n"
    )
    _run(work, "add", "-A")
    _run(work, "commit", "-q", "-m", "init")
    return work


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    """As in test_bridge_cycle_branch.py: point the core set at a file this
    module's synthetic repos actually create.
    """
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_import_hygiene.py",))


class TestSelectGateTests:
    def test_script_change_selects_its_test_plus_core(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "scripts").mkdir()
        (work / "scripts" / "foo.py").write_text("def foo():\n    return 1\n")
        (work / "tests" / "test_foo.py").write_text(
            "def test_foo():\n    assert True\n"
        )
        # An unrelated core-looking test that must NOT be pulled in just
        # because it happens to exist — only the fixed _CORE_SMOKE_TESTS set
        # and files matching the changed stem are selected.
        (work / "tests" / "test_unrelated.py").write_text(
            "def test_unrelated():\n    assert True\n"
        )

        test_paths, import_targets = bridge._select_gate_tests(
            work, ["scripts/foo.py"]
        )

        assert "tests/test_foo.py" in test_paths
        assert "tests/test_import_hygiene.py" in test_paths  # core set
        assert "tests/test_unrelated.py" not in test_paths
        assert import_targets == ["scripts/foo.py"]

    def test_changed_test_file_selects_itself(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "tests" / "test_bar.py").write_text(
            "def test_bar():\n    assert True\n"
        )

        test_paths, import_targets = bridge._select_gate_tests(
            work, ["tests/test_bar.py"]
        )

        assert "tests/test_bar.py" in test_paths
        assert "tests/test_import_hygiene.py" in test_paths
        assert "tests/test_bar.py" in import_targets

    def test_no_matching_test_still_returns_core_set(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "docs").mkdir()
        (work / "docs" / "notes.md").write_text("# notes\n")

        test_paths, import_targets = bridge._select_gate_tests(
            work, ["docs/notes.md"]
        )

        assert test_paths == ["tests/test_import_hygiene.py"]
        assert import_targets == []  # not a .py file

    def test_deleted_file_not_included_as_import_target(self, tmp_path):
        work = _init_repo(tmp_path)
        # Changed-file set can include files no longer present at HEAD
        # (deleted by the cycle) — nothing to py_compile there.
        test_paths, import_targets = bridge._select_gate_tests(
            work, ["scripts/deleted.py"]
        )
        assert import_targets == []
        assert test_paths == ["tests/test_import_hygiene.py"]


class TestImportSmoke:
    def test_syntax_error_fails_gate_before_pytest_runs(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "scripts").mkdir()
        (work / "scripts" / "broken.py").write_text("def broken(:\n    pass\n")

        passed, output = bridge._run_smoke_tests(work, changed_files=["scripts/broken.py"])

        assert passed is False
        assert "import-smoke" in output

    def test_valid_syntax_proceeds_to_pytest(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "scripts").mkdir()
        (work / "scripts" / "ok.py").write_text("def ok():\n    return 1\n")

        passed, _output = bridge._run_smoke_tests(work, changed_files=["scripts/ok.py"])

        assert passed is True  # core smoke test still passes


class TestAffectedAndCoreTests:
    def test_affected_test_fails_gate(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "scripts").mkdir()
        (work / "scripts" / "foo.py").write_text("def foo():\n    return 1\n")
        (work / "tests" / "test_foo.py").write_text(
            "def test_foo():\n    assert False\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-q", "-m", "feat: add foo + failing test")

        passed, output = bridge._run_smoke_tests(work, changed_files=["scripts/foo.py"])

        assert passed is False
        assert "test_foo" in output

    def test_affected_test_passes_and_core_passes_gate_passes(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "scripts").mkdir()
        (work / "scripts" / "foo.py").write_text("def foo():\n    return 1\n")
        (work / "tests" / "test_foo.py").write_text(
            "def test_foo():\n    assert True\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-q", "-m", "feat: add foo + passing test")

        passed, _output = bridge._run_smoke_tests(work, changed_files=["scripts/foo.py"])

        assert passed is True

    def test_no_matching_test_core_smoke_still_runs_not_an_autopass(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "docs").mkdir()
        (work / "docs" / "notes.md").write_text("# notes\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-q", "-m", "docs: add notes")

        passed, output = bridge._run_smoke_tests(work, changed_files=["docs/notes.md"])

        # Core smoke test exists and passes -> gate passes, but it actually
        # ran something (not a bare auto-pass on an empty selection).
        assert passed is True
        assert "test_import_hygiene" in output or "1 passed" in output

    def test_core_smoke_failure_fails_gate_even_with_unrelated_change(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "tests" / "test_import_hygiene.py").write_text(
            "def test_hygiene():\n    assert False\n"
        )
        (work / "docs").mkdir()
        (work / "docs" / "notes.md").write_text("# notes\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-q", "-m", "docs: add notes (core broken)")

        passed, _output = bridge._run_smoke_tests(work, changed_files=["docs/notes.md"])

        assert passed is False


class TestFailSafe:
    def test_missing_core_and_no_affected_tests_fails_closed(self, tmp_path):
        work = _init_repo(tmp_path)
        (work / "tests" / "test_import_hygiene.py").unlink()
        (work / "docs").mkdir()
        (work / "docs" / "notes.md").write_text("# notes\n")
        _run(work, "add", "-A")
        _run(work, "commit", "-q", "-m", "docs: add notes")

        passed, output = bridge._run_smoke_tests(work, changed_files=["docs/notes.md"])

        assert passed is False
        assert "no tests selected" in output

    def test_missing_tests_directory_fails_gate(self, tmp_path):
        work = _init_repo(tmp_path)
        import shutil

        shutil.rmtree(work / "tests")

        passed, output = bridge._run_smoke_tests(work, changed_files=["docs/notes.md"])

        assert passed is False
        assert "no tests directory" in output

    def test_pytest_timeout_fails_gate(self, tmp_path, monkeypatch):
        work = _init_repo(tmp_path)

        def _boom(*_args, **kwargs):
            raise subprocess.TimeoutExpired("pytest", kwargs.get("timeout", 300))

        monkeypatch.setattr(subprocess, "run", _boom)

        passed, output = bridge._run_smoke_tests(work, changed_files=[])

        assert passed is False
        assert "timed out" in output

    def test_harness_exception_fails_closed(self, tmp_path, monkeypatch):
        work = _init_repo(tmp_path)

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(subprocess, "run", _boom)

        passed, output = bridge._run_smoke_tests(work, changed_files=[])

        assert passed is False
        assert "smoke harness error" in output


class TestIntegrationFullCycle:
    """End-to-end: bounded gate wired into the same setup -> gate -> integrate
    shape used by test_bridge_cycle_branch.py's TestFullCycleFlow, but driving
    the bounded selection via changed_files derived from a real diff.
    """

    def _bare_origin_and_clone(self, tmp_path):
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
        (work / "tests" / "test_import_hygiene.py").write_text(
            "def test_hygiene():\n    assert True\n"
        )
        _run(work, "add", ".")
        _run(work, "commit", "-m", "init")
        _run(work, "push", "origin", "HEAD:main")
        return origin, work

    def test_scripts_change_with_passing_test_integrates(self, tmp_path):
        origin, work = self._bare_origin_and_clone(tmp_path)
        setup = bridge._setup_cycle_branch(work, "bounded-green")
        assert setup["ok"]
        pre_spawn_sha = _run(work, "rev-parse", "HEAD").stdout.strip()

        (work / "scripts").mkdir()
        (work / "scripts" / "foo.py").write_text("def foo():\n    return 1\n")
        (work / "tests" / "test_foo.py").write_text(
            "def test_foo():\n    assert True\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "feat: add foo")

        files_changed, blocked, mutation, _tier = bridge._changed_files_and_violations(
            work, pre_spawn_sha
        )
        assert not blocked and not mutation
        passed, _output = bridge._run_smoke_tests(work, changed_files=files_changed)
        assert passed is True

        integ = bridge._integrate_cycle_to_main(work, setup["branch"], setup["main_sha"])
        assert integ["ok"] is True
        assert (
            subprocess.run(
                _git(origin) + ["rev-parse", "main"], capture_output=True, text=True
            ).stdout.strip()
            == integ["main_sha_after"]
        )

    def test_scripts_change_with_failing_test_does_not_integrate(self, tmp_path):
        origin, work = self._bare_origin_and_clone(tmp_path)
        setup = bridge._setup_cycle_branch(work, "bounded-fail")
        assert setup["ok"]
        main_sha_before = setup["main_sha"]
        pre_spawn_sha = _run(work, "rev-parse", "HEAD").stdout.strip()

        (work / "scripts").mkdir()
        (work / "scripts" / "foo.py").write_text("def foo():\n    return 1\n")
        (work / "tests" / "test_foo.py").write_text(
            "def test_foo():\n    assert False\n"
        )
        _run(work, "add", "-A")
        _run(work, "commit", "-m", "feat: add foo (broken test)")

        files_changed, blocked, mutation, _tier = bridge._changed_files_and_violations(
            work, pre_spawn_sha
        )
        assert not blocked and not mutation
        passed, _output = bridge._run_smoke_tests(work, changed_files=files_changed)
        assert passed is False

        # Gate failed -> never integrate; restore checkout to main.
        restored = bridge._restore_to_main(work)
        assert restored is True
        origin_main = subprocess.run(
            _git(origin) + ["rev-parse", "main"], capture_output=True, text=True
        ).stdout.strip()
        assert origin_main == main_sha_before
