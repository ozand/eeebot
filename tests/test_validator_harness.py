"""Tests for #925: the validator harness — run built validator-class
scripts and consume their findings.

Covers selection (allowlist pattern, birth-window exclusion, least-
recently-run rotation persistence), execution (ok/failing/timeout/
repo-mutating scripts), the tiny findings-count parse heuristic, the
value-gated usage-evidence writer integration (2026-08 security review
findings-only posture — the harness writes no trust input), the repo-restore
bracket (2026-08 security review BLOCKER fix, detection backstop for the
systemd sandbox), the total per-invocation time budget, process-group kill
on timeout (MINOR fix), and fail-open behavior on corrupt state / a missing
repo / no git.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import demand, usage_evidence, validator_harness


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "scripts").mkdir()
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def _add_script(repo: Path, name: str, content: str, *, days_ago: float | None = None) -> Path:
    """Write ``scripts/<name>`` and commit it, optionally backdated so the
    birth-window check sees it as an old (eligible) script. ``days_ago=None``
    commits with the current time (a freshly "born" script)."""
    path = repo / "scripts" / name
    path.write_text(content, encoding="utf-8")
    env = os.environ.copy()
    if days_ago is not None:
        ts = _iso(datetime.now(timezone.utc) - timedelta(days=days_ago))
        env["GIT_AUTHOR_DATE"] = ts
        env["GIT_COMMITTER_DATE"] = ts
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"add {name}"], cwd=repo, check=True, env=env)
    return path


def _last_runs(state_dir: Path) -> list[dict]:
    path = state_dir / "validator_harness" / "last_runs.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _rotation(state_dir: Path) -> dict:
    path = state_dir / "validator_harness" / "rotation.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ─── selection ───────────────────────────────────────────────────────────


class TestCandidateSelection:
    def test_allowlist_pattern(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_foo.py", "x = 1\n", days_ago=2)
        _add_script(repo, "helper.py", "x = 1\n", days_ago=2)
        _add_script(repo, "validate_bar.py", "x = 1\n", days_ago=2)
        _add_script(repo, "test_check_foo.py", "x = 1\n", days_ago=2)
        names = sorted(p.name for p in validator_harness._candidate_scripts(repo))
        assert names == ["check_foo.py", "validate_bar.py"]

    def test_all_five_prefixes_match(self, tmp_path):
        repo = _init_repo(tmp_path)
        for prefix in ("check", "validate", "audit", "analyze", "verify"):
            _add_script(repo, f"{prefix}_x.py", "x = 1\n", days_ago=2)
        names = sorted(p.name for p in validator_harness._candidate_scripts(repo))
        assert len(names) == 5

    def test_no_scripts_dir_yields_empty(self, tmp_path):
        assert validator_harness._candidate_scripts(tmp_path / "nope") == []

    def test_birth_window_excludes_fresh_scripts(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_old.py", "x = 1\n", days_ago=2)
        _add_script(repo, "check_new.py", "x = 1\n")  # committed just now
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert result["skipped_birth_window"] == ["scripts/check_new.py"]
        assert result["ran"] == ["scripts/check_old.py"]

    def test_rotation_prefers_never_run_over_recently_run(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_a.py", "x = 1\n", days_ago=5)
        _add_script(repo, "check_b.py", "x = 1\n", days_ago=5)
        state_dir = _state_dir(tmp_path)
        rot_dir = state_dir / "validator_harness"
        rot_dir.mkdir(parents=True)
        (rot_dir / "rotation.json").write_text(
            json.dumps({
                "schema_version": "validator-harness-rotation-v1",
                "served": {"scripts/check_a.py": _iso(datetime.now(timezone.utc))},
            }),
            encoding="utf-8",
        )
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert result["selected"].index("scripts/check_b.py") < result["selected"].index(
            "scripts/check_a.py"
        )

    def test_max_k_env_limits_selection(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_VALIDATOR_HARNESS_MAX", "1")
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_a.py", "x = 1\n", days_ago=5)
        _add_script(repo, "check_b.py", "x = 1\n", days_ago=5)
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert len(result["selected"]) == 1
        assert len(result["ran"]) == 1

    def test_rotation_persists_only_for_scripts_actually_run(self, tmp_path):
        """#925 design: stamping happens at RUN time, not selection time —
        a script merely selected but never executed (budget exhaustion)
        must not appear more recently run than it truly was."""
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_a.py", "x = 1\n", days_ago=5)
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)
        rotation = _rotation(state_dir)
        assert "scripts/check_a.py" in rotation["served"]

    def test_rotation_prunes_stale_entries(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_a.py", "x = 1\n", days_ago=5)
        state_dir = _state_dir(tmp_path)
        rot_dir = state_dir / "validator_harness"
        rot_dir.mkdir(parents=True)
        (rot_dir / "rotation.json").write_text(
            json.dumps({
                "schema_version": "validator-harness-rotation-v1",
                "served": {
                    "scripts/check_a.py": _iso(datetime.now(timezone.utc)),
                    "scripts/long_gone.py": _iso(datetime.now(timezone.utc)),
                },
            }),
            encoding="utf-8",
        )
        validator_harness.run_validator_harness(state_dir, repo)
        rotation = _rotation(state_dir)
        assert "scripts/long_gone.py" not in rotation["served"]


# ─── execution ───────────────────────────────────────────────────────────


class TestExecution:
    def test_ok_script_exits_zero(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_ok.py", "print('fine')\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert result["ran"] == ["scripts/check_ok.py"]
        rows = _last_runs(state_dir)
        assert len(rows) == 1
        assert rows[0]["exit_code"] == 0
        assert rows[0]["repo_dirtied"] is False

    def test_failing_script_becomes_demand(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_fail.py", "import sys\nsys.exit(1)\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)
        rows = _last_runs(state_dir)
        assert rows[0]["exit_code"] == 1
        items = demand._validator_defect_items(state_dir)
        assert any(
            i["summary"] == "validator scripts/check_fail.py fails when run" for i in items
        )

    def test_timeout_script_is_killed_and_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validator_harness, "_PER_SCRIPT_TIMEOUT", 1.0)
        monkeypatch.setattr(validator_harness, "_TOTAL_BUDGET_SECONDS", 10.0)
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_slow.py", "import time\ntime.sleep(30)\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)
        rows = _last_runs(state_dir)
        assert len(rows) == 1
        assert rows[0]["exit_code"] is None
        assert "timeout" in rows[0]["stderr_tail"]
        items = demand._validator_defect_items(state_dir)
        assert items == []  # a None exit code is not a "fails when run" defect (no crash claim fabricated)

    def test_repo_mutating_script_is_restored_and_flagged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_mutate.py",
            "from pathlib import Path\nPath('scripts/mutated.txt').write_text('x')\n",
            days_ago=2,
        )
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)

        assert not (repo / "scripts" / "mutated.txt").exists()  # restored
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        )
        assert status.stdout.strip() == ""

        rows = _last_runs(state_dir)
        assert rows[0]["repo_dirtied"] is True
        items = demand._validator_defect_items(state_dir)
        assert items[0]["summary"] == "validator scripts/check_mutate.py mutates the repo when run"

    def test_json_findings_are_counted(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_findings.py",
            "import argparse, json\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--json', action='store_true')\n"
            "p.parse_args()\n"
            "print(json.dumps({'findings': ['a', 'b', 'c']}))\n",
            days_ago=2,
        )
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)
        rows = _last_runs(state_dir)
        assert rows[0]["exit_code"] == 0
        assert rows[0]["findings_count"] == 3
        items = demand._validator_defect_items(state_dir)
        assert items[0]["summary"] == "validator scripts/check_findings.py reports 3 findings"

    def test_plain_invocation_when_script_has_no_json_flag(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_plain.py", "print('no flags here')\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)
        rows = _last_runs(state_dir)
        assert rows[0]["exit_code"] == 0
        assert rows[0]["findings_count"] is None

    def test_non_json_stdout_yields_no_findings_count(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_text.py", "print('plain text output, not json')\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)
        rows = _last_runs(state_dir)
        assert rows[0]["findings_count"] is None


# ─── process-group kill on timeout (security review MINOR fix) ────────────


class TestProcessGroupKill:
    @pytest.mark.skipif(os.name != "posix", reason="process-group kill (os.killpg) is POSIX-only")
    def test_timeout_kills_forked_grandchild(self, tmp_path, monkeypatch):
        """A validator that forks a grandchild and then blocks must not
        leave that grandchild running past the timeout cap -- a plain
        proc.kill() on the direct child only would let it survive."""
        monkeypatch.setattr(validator_harness, "_PER_SCRIPT_TIMEOUT", 1.0)
        monkeypatch.setattr(validator_harness, "_TOTAL_BUDGET_SECONDS", 10.0)
        repo = _init_repo(tmp_path)
        marker = tmp_path / "grandchild_survived.txt"
        grandchild_code = f"import time; time.sleep(3); open(r'{marker}', 'w').write('alive')"
        script = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}])\n"
            "time.sleep(30)\n"
        )
        _add_script(repo, "check_fork.py", script, days_ago=2)
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)
        # The grandchild sleeps 3s before writing; give it well past that to
        # prove it never got the chance -- it should have died with the
        # process group at the ~1s timeout, long before its own sleep ends.
        time.sleep(4)
        assert not marker.exists()


# ─── total budget ────────────────────────────────────────────────────────


class TestTotalBudget:
    def test_budget_cap_stops_further_runs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validator_harness, "_TOTAL_BUDGET_SECONDS", 1.0)
        monkeypatch.setattr(validator_harness, "_PER_SCRIPT_TIMEOUT", 5.0)
        repo = _init_repo(tmp_path)
        for i in range(3):
            _add_script(repo, f"check_{i}.py", "import time\ntime.sleep(0.9)\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, repo)
        # The budget (1s) cannot fit 3 * ~0.9s runs plus overhead -- at least
        # one selected script must be left un-run.
        assert len(result["ran"]) < len(result["selected"])
        assert len(result["selected"]) == 3


# ─── fail-open ───────────────────────────────────────────────────────────


class TestFailOpen:
    def test_missing_repo_yields_empty_result(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, tmp_path / "does-not-exist")
        assert result == {"selected": [], "ran": [], "skipped_birth_window": [], "errors": []}

    def test_corrupt_rotation_still_runs(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_ok.py", "x = 1\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        rot_dir = state_dir / "validator_harness"
        rot_dir.mkdir(parents=True)
        (rot_dir / "rotation.json").write_text("{not json", encoding="utf-8")
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert result["ran"] == ["scripts/check_ok.py"]

    def test_corrupt_last_runs_does_not_break_demand_collection(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        rd = state_dir / "validator_harness"
        rd.mkdir(parents=True)
        (rd / "last_runs.jsonl").write_bytes(b"\xff\xfe not utf8 garbage")
        assert demand._validator_defect_items(state_dir) == []

    def test_no_git_repo_fails_open_to_birth_window_skip(self, tmp_path):
        """No ``.git`` at all: ``git log`` fails, creation date is
        unknowable, and the module's own discipline is "cannot determine
        age -> skip" — never run something whose age it cannot establish."""
        repo = tmp_path / "notgit"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "check_ok.py").write_text("x = 1\n", encoding="utf-8")
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert result["skipped_birth_window"] == ["scripts/check_ok.py"]
        assert result["ran"] == []

    def test_empty_scripts_dir_yields_empty_result(self, tmp_path):
        repo = _init_repo(tmp_path)
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert result == {"selected": [], "ran": [], "skipped_birth_window": [], "errors": []}


# ─── CLI entrypoint ──────────────────────────────────────────────────────


class TestMain:
    def test_main_with_explicit_args_runs_harness(self, tmp_path, capsys):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_ok.py", "x = 1\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        rc = validator_harness.main(["--state-root", str(state_dir), "--repo", str(repo)])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ran"] == ["scripts/check_ok.py"]

    def test_default_repo_derives_from_state_root(self, tmp_path):
        state_root = tmp_path / "state"
        assert validator_harness._default_repo(state_root) == state_root.parent / "eeebot-self-evolving"


# ─── findings-only posture (2026-08 security review outcome) ──────────────


class TestWritesNoTrustInput:
    """The harness is a findings producer only: it must never write usage
    evidence (or any other fitness-trust input). Recording usage from a phase
    that also executes instance-authored code was forgeable and raced the
    bridge's own sidecar writes, so the whole write path was removed."""

    def test_run_writes_no_usage_sidecar(self, tmp_path):
        state = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_findings.py",
            "import json" + chr(10) + "print(json.dumps({'findings': [1, 2]}))" + chr(10),
            days_ago=5,
        )

        result = validator_harness.run_validator_harness(state, repo)

        assert result["ran"] == ["scripts/check_findings.py"]
        assert not (state / "usage").exists()
        assert not (state / "usage" / "last_used.json").exists()

    def test_module_exposes_no_usage_recording_helpers(self):
        for gone in ("record_validator_run", "_confirms_value", "integrity_incidents_path"):
            assert not hasattr(validator_harness, gone), gone

    def test_findings_still_become_demand(self, tmp_path):
        state = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_findings.py",
            "import json" + chr(10) + "print(json.dumps({'findings': [1, 2]}))" + chr(10),
            days_ago=5,
        )

        validator_harness.run_validator_harness(state, repo)
        items = demand._validator_defect_items(state)

        assert len(items) == 1
        assert "reports 2 findings" in items[0]["summary"]
