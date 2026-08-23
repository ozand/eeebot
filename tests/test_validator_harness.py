"""Tests for nanobot.runtime.validator_harness (#925).

Covers selection (allowlist pattern, birth-window exclusion, least-recently-run
rotation), bounded execution (per-script timeout, total budget, process-group
kill), the findings-parse heuristic, the no-trust-input posture (the harness
writes nothing under state/ but its own bookkeeping — the systemd sandbox is
the enforcing control), the read-only repo posture, fail-open behavior, and the
CLI entrypoint.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import demand, validator_harness


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

    def test_archived_marker_excludes_script(self, tmp_path):
        """#928: an archived script's declared contract is "do not run me";
        it must never even be a candidate, regardless of exit code."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_archived.py",
            "print('WARNING: scripts/check_archived.py is deprecated and "
            "marked as archived (decay-36bd86468443) as unused.')\n",
            days_ago=2,
        )
        _add_script(repo, "check_ok.py", "x = 1\n", days_ago=2)
        names = sorted(p.name for p in validator_harness._candidate_scripts(repo))
        assert names == ["check_ok.py"]

    def test_archived_marker_other_form_excludes_script(self, tmp_path):
        """The second observed marker form ('script is archived') must also
        be excluded, not just the 'marked as archived' wording."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_disabled.py",
            "raise SystemExit('Error: Execution is disabled because this "
            "script is archived.')\n",
            days_ago=2,
        )
        names = [p.name for p in validator_harness._candidate_scripts(repo)]
        assert "check_disabled.py" not in names

    def test_unreadable_script_is_still_a_candidate(self, tmp_path):
        """Fail-open direction (#928): when the archived-marker scan cannot
        even read the file, it must NOT be excluded -- an unreadable script
        will simply fail to run on its own, which is honest, not a
        fabricated archival verdict. A directory sharing the allowlisted
        name stands in for "unreadable" here (_scan_head's open() raises)."""
        repo = _init_repo(tmp_path)
        (repo / "scripts" / "check_weird.py").mkdir()
        names = [p.name for p in validator_harness._candidate_scripts(repo)]
        assert "check_weird.py" in names

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

    def test_repo_mutation_is_not_policed_in_process(self, tmp_path):
        """The sandbox mounts the instance repo read-only, so the harness no
        longer compares/restores git state: doing that on the checkout the
        bridge shares (it holds uncommitted subagent work mid-cycle) could
        destroy that work and mis-blame an innocent validator. A run is still
        recorded; the repo_dirtied field is gone."""
        state = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_writes.py",
            "open('stray.txt', 'w').write('x')" + chr(10),
            days_ago=5,
        )

        validator_harness.run_validator_harness(state, repo)

        rows = _last_runs(state)
        assert len(rows) == 1
        assert "repo_dirtied" not in rows[0]
        assert demand._validator_defect_items(state) == []

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


# ─── output cap enforced during capture, not after (#928) ────────────────


class TestOutputCapDuringCapture:
    """#928: ``proc.communicate(timeout=...)`` buffers the WHOLE stream in
    memory before the ``_MAX_OUTPUT_BYTES`` slice is applied, so a runaway
    printer was previously bounded only by ``MemoryMax`` (an OOM kill of the
    unit), not by the module's own cap. The fix must keep draining BOTH
    pipes after the cap is reached, discarding the excess -- if it stopped
    reading instead, the child would block on a full pipe buffer and hang
    until the per-script timeout kills it, turning a merely chatty (but
    otherwise fine) validator into a bogus timeout record."""

    def test_runaway_printer_completes_with_real_exit_code(self, tmp_path, monkeypatch):
        # Bounded so a REGRESSION (stop-draining-at-cap) fails this test
        # quickly instead of burning the default 60s per-script timeout.
        monkeypatch.setattr(validator_harness, "_PER_SCRIPT_TIMEOUT", 15.0)
        monkeypatch.setattr(validator_harness, "_TOTAL_BUDGET_SECONDS", 20.0)
        repo = _init_repo(tmp_path)
        # Several MB on BOTH stdout and stderr -- many times the OS pipe
        # buffer (tens of KB) and _MAX_OUTPUT_BYTES (64KB), so the child
        # WILL block on write() unless something keeps draining past the
        # cap on each stream independently.
        script = (
            "import sys\n"
            "for _ in range(50000):\n"
            "    sys.stdout.write('o' * 100 + chr(10))\n"
            "    sys.stderr.write('e' * 100 + chr(10))\n"
            "sys.exit(7)\n"
        )
        _add_script(repo, "check_noisy.py", script, days_ago=2)
        state_dir = _state_dir(tmp_path)
        validator_harness.run_validator_harness(state_dir, repo)
        rows = _last_runs(state_dir)
        assert len(rows) == 1
        # The child's REAL exit code, not None (which is what a timeout or
        # a generic execution error records) -- proves the run finished
        # rather than hanging on a full pipe.
        assert rows[0]["exit_code"] == 7
        assert "timeout" not in (rows[0]["stderr_tail"] or "")
        # stderr_tail is the last 2000 chars of the internally-capped
        # stream: since the cap keeps only the FIRST _MAX_OUTPUT_BYTES
        # chars and every retained char here is 'e' or newline, an
        # uncapped/broken capture would still show only 'e' too -- the
        # real proof is the exit code above; this just sanity-checks the
        # tail is well-formed capped text, not garbage.
        assert set(rows[0]["stderr_tail"].replace("\n", "")) <= {"e"}


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


# ─── writable-directory probe (#928) ──────────────────────────────────────


class TestWritableProbe:
    """#928: every write into state/validator_harness/ was fail-open, so a
    broken writable carve-out previously made the unit exit 0 while
    recording nothing. The probe at the start of run_validator_harness must
    turn that into a loud, reported failure instead."""

    def test_non_writable_state_dir_fails_loudly(self, tmp_path):
        if os.name != "posix":
            pytest.skip("chmod-based read-only enforcement is not reliable on Windows")
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_ok.py", "x = 1\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        harness_dir = state_dir / "validator_harness"
        harness_dir.mkdir(parents=True)
        os.chmod(harness_dir, 0o500)  # read+execute only, no write
        try:
            probe = harness_dir / "permission_probe"
            try:
                probe.write_text("x", encoding="utf-8")
                probe.unlink()
                pytest.skip(
                    "directory write permission not enforced in this "
                    "environment (e.g. running as root)"
                )
            except OSError:
                pass
            result = validator_harness.run_validator_harness(state_dir, repo)
            assert result["errors"] == ["state_dir_not_writable"]
            assert result["ran"] == []
        finally:
            os.chmod(harness_dir, 0o700)

    def test_writable_state_dir_has_no_probe_error(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_ok.py", "x = 1\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert result["errors"] == []


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

    def test_main_exits_nonzero_when_state_dir_not_writable(self, tmp_path, monkeypatch, capsys):
        """#928: main()'s exit code must reflect a probe failure rather than
        the misleadingly successful 0 a fail-open write would have left."""
        monkeypatch.setattr(validator_harness, "_probe_writable", lambda _state_dir: False)
        repo = _init_repo(tmp_path)
        state_dir = _state_dir(tmp_path)
        rc = validator_harness.main(["--state-root", str(state_dir), "--repo", str(repo)])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["errors"] == ["state_dir_not_writable"]
        assert out["ran"] == []


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


# ─── #928 review: the harness prunes its own store ──────────────────────


class TestPruneLastRuns:
    """#928 review: ``demand`` presents the LAST row per script, so a row
    outlives what produced it until a newer row for the same path replaces
    it. An archived or deleted script is never selected again, so without a
    prune its failing row stays newest for the ~25 days it takes to scroll
    out of the 500-line window — and the 7-day completed-TTL re-presents it
    as demand every week in the meantime."""

    def _rows(self, state_dir, *paths):
        d = state_dir / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "last_runs.jsonl").open("w", encoding="utf-8") as fh:
            for path in paths:
                fh.write(json.dumps({"path": path, "exit_code": 1}) + "\n")
        return d / "last_runs.jsonl"

    def _paths_in(self, sidecar):
        return [
            json.loads(ln)["path"]
            for ln in sidecar.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    def test_row_for_non_candidate_is_dropped(self, tmp_path):
        sidecar = self._rows(
            tmp_path, "scripts/check_gone.py", "scripts/check_here.py"
        )
        validator_harness._prune_last_runs(tmp_path, {"scripts/check_here.py"})
        assert self._paths_in(sidecar) == ["scripts/check_here.py"]

    def test_untouched_when_every_row_is_a_candidate(self, tmp_path):
        sidecar = self._rows(tmp_path, "scripts/check_a.py", "scripts/check_b.py")
        before = sidecar.read_bytes()
        validator_harness._prune_last_runs(
            tmp_path, {"scripts/check_a.py", "scripts/check_b.py"}
        )
        assert sidecar.read_bytes() == before

    def test_overlong_line_is_dropped(self, tmp_path):
        """A validator can append here itself, and one line past demand's
        2 MB sidecar guard silences ALL validator demand — real defects
        included — while the line-based trim keeps it alive for 500 runs."""
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        sidecar = d / "last_runs.jsonl"
        with sidecar.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": "scripts/check_ok.py", "exit_code": 1}) + "\n")
            fh.write(json.dumps(
                {"path": "scripts/check_fat.py", "exit_code": 1,
                 "stderr_tail": "x" * (32 * 1024)}
            ) + "\n")
        validator_harness._prune_last_runs(
            tmp_path, {"scripts/check_ok.py", "scripts/check_fat.py"}
        )
        assert self._paths_in(sidecar) == ["scripts/check_ok.py"]

    def test_missing_sidecar_is_a_no_op(self, tmp_path):
        validator_harness._prune_last_runs(tmp_path, {"scripts/check_a.py"})
        assert not (tmp_path / "validator_harness" / "last_runs.jsonl").exists()

    def test_run_does_not_prune_when_candidates_fail_open_to_empty(self, tmp_path):
        """``_candidate_scripts`` fails open to ``[]``; pruning against an
        empty valid set would wipe every verdict on a transient error.

        The repo below EXISTS but has no ``scripts/`` directory, so execution
        reaches the ``if not candidates`` guard. Pointing at a non-existent
        repo would return earlier, at ``not selfevo_repo.is_dir()``, and the
        test would pass even with the prune call moved above the guard."""
        sidecar = self._rows(tmp_path, "scripts/check_a.py")
        before = sidecar.read_bytes()
        repo = tmp_path / "repo-without-scripts"
        repo.mkdir()
        assert validator_harness._candidate_scripts(repo) == []
        validator_harness.run_validator_harness(tmp_path, repo)
        assert sidecar.read_bytes() == before

    def test_many_medium_rows_are_pruned_below_demands_read_guard(self, tmp_path):
        """Round-2 review: the first cut short-circuited only above 8 MB and
        otherwise dropped lines over 16 KB, so ~300 rows of ~10 KB each — no
        single line over the per-line cap — sailed through untouched at ~3 MB,
        which is ABOVE demand's 2 MB refusal threshold and therefore silenced
        every validator defect, real ones included."""
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        sidecar = d / "last_runs.jsonl"
        with sidecar.open("w", encoding="utf-8") as fh:
            for _ in range(300):
                fh.write(json.dumps(
                    {"path": "scripts/check_noise.py", "exit_code": 0,
                     "stderr_tail": "x" * 10_000}
                ) + "\n")
            fh.write(json.dumps(
                {"path": "scripts/check_real.py", "exit_code": 1,
                 "stderr_tail": "genuine failure"}
            ) + "\n")
        assert sidecar.stat().st_size > 2_000_000
        validator_harness._prune_last_runs(
            tmp_path, {"scripts/check_noise.py", "scripts/check_real.py"}
        )
        assert sidecar.stat().st_size < 2_000_000
        # The newest row must survive: the budget drops OLDEST rows first.
        assert "scripts/check_real.py" in self._paths_in(sidecar)

    def test_squatted_tmp_name_does_not_disable_pruning(self, tmp_path):
        """A validator can create paths in this directory. A fixed ``.tmp``
        name is squattable — ``mkdir`` it and every write here fails open —
        so the atomic write uses a uuid suffix."""
        sidecar = self._rows(tmp_path, "scripts/check_gone.py", "scripts/check_here.py")
        (tmp_path / "validator_harness" / "last_runs.jsonl.tmp").mkdir()
        validator_harness._prune_last_runs(tmp_path, {"scripts/check_here.py"})
        assert self._paths_in(sidecar) == ["scripts/check_here.py"]


# ─── #928 review: a sandbox denial is not a script defect ────────────────


class TestSandboxDenialMarker:
    """#928: the unit makes several state subtrees inaccessible, so a
    validator that reads one crashes with ``PermissionError`` through no
    fault of its own. That must be recorded as an environment problem, not
    scored as a failing validator."""

    def test_permission_error_is_marked(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_denies.py",
            "import sys\n"
            "sys.stderr.write('PermissionError: [Errno 13] Permission denied: x')\n"
            "sys.exit(1)\n",
            days_ago=2,
        )
        script = repo / "scripts" / "check_denies.py"
        record = validator_harness._run_one(script, repo, 30.0)
        assert record["exit_code"] == 1
        assert record["harness_env_error"] == "permission_denied"

    def test_ordinary_failure_is_not_marked(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(
            repo, "check_boom.py", "raise SystemExit('boom')\n", days_ago=2
        )
        record = validator_harness._run_one(repo / "scripts" / "check_boom.py", repo, 30.0)
        assert record["exit_code"] != 0
        assert "harness_env_error" not in record

    def test_clean_exit_is_not_marked(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_fine.py", "print('ok')\n", days_ago=2)
        record = validator_harness._run_one(repo / "scripts" / "check_fine.py", repo, 30.0)
        assert record["exit_code"] == 0
        assert "harness_env_error" not in record


# ─── #928 round 2: _run_one must always return ──────────────────────────


class TestRunOneAlwaysReturns:
    """Round-2 review: an explicit ``proc.stdout.close()`` deadlocked
    ``_run_one`` — ``close()`` wants the same io lock a reader thread holds
    while blocked in ``read()``, which is exactly the state once its
    ``join(timeout=5)`` has expired. Measured then: 10.2s to return before
    that change, never returning after it.

    A hang here is not merely slow: the run record is appended and the
    rotation stamped only AFTER this function returns, so the script would
    get no row and no rotation stamp, be selected first again next time
    (never-run sorts first), and the unit would be SIGKILLed at
    ``TimeoutStartSec`` every 6h with nothing recorded to explain it."""

    def test_returns_when_a_detached_grandchild_holds_the_pipes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_detaches.py",
            "import subprocess, sys\n"
            "subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "    start_new_session=True,\n"
            ")\n"
            "sys.exit(1)\n",
            days_ago=2,
        )
        started = time.monotonic()
        record = validator_harness._run_one(
            repo / "scripts" / "check_detaches.py", repo, 5.0
        )
        elapsed = time.monotonic() - started
        # Bound chosen to FAIL rather than hang. close() on a stream whose
        # reader thread is blocked in read() does not block forever: it
        # returns once the last pipe writer exits, measured at 16.1s against
        # a 20s sleeper. So with the 30s grandchild above, a reintroduced
        # close returns at ~30s and trips this assert, while the legitimate
        # path measures ~10s. That matters because .github/workflows/ci.yml
        # sets no timeout-minutes, so a genuine hang would have burned the
        # 6h default on all three matrix legs before going red.
        assert elapsed < 20, f"_run_one took {elapsed:.1f}s"
        assert record["path"] == "scripts/check_detaches.py"


# ─── #928 round 2: the pgid must never be our own group ─────────────────


class TestProcessGroupId:
    """Round-2 review: ``os.getpgid(proc.pid)`` raced the child's ``setsid()``
    and could return the HARNESS's own pgid, which ``_kill_process_group``
    would then SIGKILL — taking the systemd unit down with it. The pgid is now
    derived from the pid (guaranteed equal under ``start_new_session=True``)
    and is refused outright if it matches our own group."""

    def test_none_for_missing_proc(self):
        assert validator_harness._process_group_id(None) is None

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
    def test_returns_child_pid(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            start_new_session=True,
        )
        try:
            assert validator_harness._process_group_id(proc) == proc.pid
        finally:
            proc.kill()
            proc.wait(timeout=5)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
    def test_does_not_consult_getpgid(self):
        """The regression this class exists for, made detectable. Asserting
        the returned value alone cannot catch it: outside the narrow race
        ``os.getpgid(pid)`` returns the pid too, so the old implementation
        passed. Poison ``getpgid`` with what it used to return in the race —
        our own group — and the old code hands back a pgid that killpg would
        SIGKILL the harness with."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            start_new_session=True,
        )
        try:
            monkey = lambda _pid: os.getpgrp()  # noqa: E731 - one-line stub
            original = validator_harness.os.getpgid
            validator_harness.os.getpgid = monkey  # type: ignore[assignment]
            try:
                assert validator_harness._process_group_id(proc) == proc.pid
            finally:
                validator_harness.os.getpgid = original  # type: ignore[assignment]
        finally:
            proc.kill()
            proc.wait(timeout=5)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
    def test_refuses_our_own_group(self, monkeypatch):
        """The guard, exercised directly: whatever the arithmetic says, a pgid
        equal to our own group must never be handed to killpg."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            start_new_session=True,
        )
        try:
            monkeypatch.setattr(validator_harness.os, "getpgrp", lambda: proc.pid)
            assert validator_harness._process_group_id(proc) is None
        finally:
            proc.kill()
            proc.wait(timeout=5)


# ─── #928 round 3: pruning must not evict other scripts' verdicts ───────


class TestPrunePreservesNewestPerPath:
    """Round-3 review: the byte budget was newest-first but not PER PATH, and
    it broke out of the loop — so a validator appending rows naming ITSELF
    filled the newest megabyte and the prune then DELETED every other
    script's newest verdict. Worse than the channel it replaced, where the
    row at least survived on disk unread."""

    def _paths_in(self, sidecar):
        return [
            json.loads(ln)["path"]
            for ln in sidecar.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    def test_flooding_validator_cannot_evict_another_scripts_verdict(self, tmp_path):
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        sidecar = d / "last_runs.jsonl"
        with sidecar.open("w", encoding="utf-8") as fh:
            # The genuine verdict is the OLDEST row — the worst case for a
            # newest-first budget.
            fh.write(json.dumps(
                {"path": "scripts/check_victim.py", "exit_code": 1,
                 "stderr_tail": "genuine failure"}
            ) + "\n")
            for _ in range(200):
                fh.write(json.dumps(
                    {"path": "scripts/check_evil.py", "exit_code": 0,
                     "stderr_tail": "z" * 15_000}
                ) + "\n")
        assert sidecar.stat().st_size > 2_000_000

        validator_harness._prune_last_runs(
            tmp_path, {"scripts/check_victim.py", "scripts/check_evil.py"}
        )

        assert sidecar.stat().st_size < 2_000_000
        assert "scripts/check_victim.py" in self._paths_in(sidecar)
        items = demand._validator_defect_items(tmp_path)
        assert [i["affected_path"] for i in items] == ["scripts/check_victim.py"]

    def test_keep_budget_stays_under_demands_read_guard(self):
        """The two constants live in two modules on purpose — demand must not
        import the harness — which is exactly the kind of pair that drifts."""
        assert (
            validator_harness._MAX_LAST_RUNS_KEEP_BYTES
            < demand._MAX_VALIDATOR_SIDECAR_BYTES
        )

    def test_append_enforces_the_byte_bound_too(self, tmp_path):
        """The prune runs once, at the top of an invocation. Without a bound
        here, a validator could push the file past demand's read guard during
        its own run and silence every validator defect for the whole 6h until
        the next prune."""
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        sidecar = d / "last_runs.jsonl"
        with sidecar.open("w", encoding="utf-8") as fh:
            for _ in range(200):
                fh.write(json.dumps(
                    {"path": "scripts/check_evil.py", "exit_code": 0,
                     "stderr_tail": "z" * 15_000}
                ) + "\n")
        assert sidecar.stat().st_size > 2_000_000
        validator_harness._append_last_run(
            tmp_path, {"path": "scripts/check_ok.py", "exit_code": 0}
        )
        assert sidecar.stat().st_size < 2_000_000
        assert "scripts/check_ok.py" in self._paths_in(sidecar)

    def test_atomic_write_leaves_no_temp_file(self, tmp_path):
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "last_runs.jsonl"
        validator_harness._atomic_write(target, "x\n")
        assert target.read_text(encoding="utf-8") == "x\n"
        assert list(d.glob("*.tmp")) == []
