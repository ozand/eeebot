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
import re
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

    def test_bare_mention_without_self_declaration_stays_a_candidate(self, tmp_path):
        """#934: the pre-#934 rule matched the bare phrase 'script is
        archived' anywhere in the source head -- a MENTION test. This text
        mentions archival but is neither the canonical self-declaration
        phrase ('is deprecated and marked as archived'/'scheduled for
        removal') nor paired with the script's own path, so under the
        current self-declaration rule it must stay a candidate. See
        TestDecayDeclarationExclusion for the properties this replaced."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_disabled.py",
            "raise SystemExit('Error: Execution is disabled because this "
            "script is archived.')\n",
            days_ago=2,
        )
        names = [p.name for p in validator_harness._candidate_scripts(repo)]
        assert "check_disabled.py" in names

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


# ─── #934: self-declaration, not mention, excludes a script ──────────────


class TestDecayDeclarationExclusion:
    """#934: the old ``_ARCHIVED_RE`` matched any MENTION of the archival
    phrases anywhere in a script's source head, not a self-declaration --
    so the copy-pasted helper docstring

        \"\"\"Check if a script is archived/deprecated by reading its
        content.\"\"\"

    (present verbatim in many validators that IMPLEMENT decay detection)
    tripped the ``script is archived`` alternative and silently excluded
    them from ever running. Measured live: the old rule excluded 25 of 42
    allowlisted validators; only 13 genuinely self-declare. This class
    reproduces that shape with a synthetic 42-script fixture and also pins
    the two properties individually."""

    def test_mention_only_stays_a_candidate(self, tmp_path):
        """A script whose head merely MENTIONS archival (the copy-pasted
        helper docstring) must remain a candidate -- it is not declaring
        itself archived, it is looking for scripts that are."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "validate_decay_helper.py",
            '    """Check if a script is archived/deprecated by reading '
            'its content."""\n'
            "def helper():\n    pass\n",
            days_ago=2,
        )
        names = [p.name for p in validator_harness._candidate_scripts(repo)]
        assert "validate_decay_helper.py" in names

    def test_self_declaration_with_own_path_is_excluded(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "analyze_repo_size.py",
            "print('WARNING: scripts/analyze_repo_size.py is deprecated "
            "and marked as archived (decay-36bd86468443) as unused.')\n",
            days_ago=2,
        )
        names = [p.name for p in validator_harness._candidate_scripts(repo)]
        assert "analyze_repo_size.py" not in names

    def test_scheduled_for_removal_rung_is_excluded(self, tmp_path):
        """Class C in #934: the rung BEFORE 'marked as archived' -- a
        script declining to run for exactly the same reason, which the old
        rule missed entirely (it does not contain either old alternative)."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "verify_eeepc_self_evolving_service_guard.py",
            "print('WARNING: scripts/verify_eeepc_self_evolving_service_"
            "guard.py is deprecated and scheduled for removal after 14+ "
            "days of disuse.')\n",
            days_ago=2,
        )
        names = [
            p.name for p in validator_harness._candidate_scripts(repo)
        ]
        assert "verify_eeepc_self_evolving_service_guard.py" not in names

    def test_self_declaration_missing_own_path_fails_open(self, tmp_path):
        """The own-path requirement fails open the same direction as an
        unreadable file: a genuine declaration that omits its own path
        stays a candidate (and will go on to produce a VISIBLE false
        defect when it refuses to run) rather than being silently
        silenced on an unverifiable claim."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_vague.py",
            "print('WARNING: this script is deprecated and marked as "
            "archived as unused.')\n",
            days_ago=2,
        )
        names = [p.name for p in validator_harness._candidate_scripts(repo)]
        assert "check_vague.py" in names

    def test_old_rule_over_excludes_new_rule_does_not_synthetic_42(self, tmp_path):
        """Reproduces the measured live shape: 42 allowlisted scripts, 13 of
        which genuinely self-declare (11 'marked as archived' + 2 'scheduled
        for removal', each naming its own path) and 14 of which merely carry
        the copy-pasted mention-only docstring. The OLD (mention) rule
        excludes 25 (13 - 2 genuine 'scheduled for removal' misses it
        entirely + 14 false positives = 11 + 14); the NEW (self-declaration)
        rule excludes exactly the 13 genuine ones."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()

        # 11 genuine self-declarations, "marked as archived" rung.
        for i in range(11):
            name = f"check_archived_{i:02d}.py"
            (scripts_dir / name).write_text(
                f"print('WARNING: scripts/{name} is deprecated and marked "
                "as archived (decay-deadbeef) as unused.')\n",
                encoding="utf-8",
            )

        # 2 genuine self-declarations, "scheduled for removal" rung.
        for i in range(2):
            name = f"verify_removal_{i:02d}.py"
            (scripts_dir / name).write_text(
                f"print('WARNING: scripts/{name} is deprecated and "
                "scheduled for removal after 14+ days of disuse.')\n",
                encoding="utf-8",
            )

        # 14 mention-only scripts: the copy-pasted helper docstring, which
        # implements decay DETECTION rather than declaring itself decayed.
        for i in range(14):
            name = f"validate_decay_helper_{i:02d}.py"
            (scripts_dir / name).write_text(
                '    """Check if a script is archived/deprecated by '
                'reading its content."""\n'
                "def helper():\n    pass\n",
                encoding="utf-8",
            )

        # 15 plain scripts, no marker at all.
        for i in range(15):
            name = f"analyze_plain_{i:02d}.py"
            (scripts_dir / name).write_text("x = 1\n", encoding="utf-8")

        all_scripts = list(scripts_dir.glob("*.py"))
        assert len(all_scripts) == 42

        old_mention_rule = re.compile(r"marked as archived|script is archived")
        old_excluded = [
            p for p in all_scripts
            if old_mention_rule.search(validator_harness._scan_head(p))
        ]
        assert len(old_excluded) == 25

        candidates = validator_harness._candidate_scripts(tmp_path)
        assert len(candidates) == 42 - 13

        candidate_names = {p.name for p in candidates}
        for i in range(11):
            assert f"check_archived_{i:02d}.py" not in candidate_names
        for i in range(2):
            assert f"verify_removal_{i:02d}.py" not in candidate_names
        for i in range(14):
            assert f"validate_decay_helper_{i:02d}.py" in candidate_names
        for i in range(15):
            assert f"analyze_plain_{i:02d}.py" in candidate_names


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
        # #934: a timed-out run now DOES produce demand — but it must never
        # fabricate a crash claim. The original #925 guarantee is that the
        # item does not say "fails when run"; what changed is that the run
        # stopped being invisible (it used to yield nothing at all while
        # still burning a quarter of the invocation's budget every rotation).
        assert len(items) == 1
        assert items[0]["affected_path"] == "scripts/check_slow.py"
        assert "fails when run" not in items[0]["summary"]
        assert "time budget" in items[0]["summary"]

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
        # Re-tuned (#934 review round 2). With budget 1.0 and per-script 5.0
        # the loop now breaks on the FIRST iteration, so `ran` was empty and
        # `0 < 3` held whether or not the loop guard worked at all — the only
        # test named for that guard had stopped discriminating in either
        # direction. Budget 7.0 against three ~1.5s runs makes it exercise
        # the real shape again: some run, then the cap stops the rest.
        monkeypatch.setattr(validator_harness, "_TOTAL_BUDGET_SECONDS", 7.0)
        monkeypatch.setattr(validator_harness, "_PER_SCRIPT_TIMEOUT", 5.0)
        repo = _init_repo(tmp_path)
        for i in range(3):
            _add_script(repo, f"check_{i}.py", "import time\ntime.sleep(1.5)\n", days_ago=2)
        state_dir = _state_dir(tmp_path)
        result = validator_harness.run_validator_harness(state_dir, repo)
        assert len(result["selected"]) == 3
        assert 0 < len(result["ran"]) < len(result["selected"])


# ─── #934: a script that always times out is reclassified, not excluded ──


def _seed_last_runs(state_dir: Path, rows: list[dict]) -> None:
    """Append raw sidecar rows, bypassing the harness — how a validator
    subprocess writes into the one carve-out it shares with the harness."""
    path = state_dir / "validator_harness" / "last_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


class TestTimeoutReclassified:
    """#934: a run killed at ``_PER_SCRIPT_TIMEOUT`` has ``exit_code None``,
    so it used to produce NOTHING anywhere — no demand, no operator signal —
    while still burning a quarter of ``_TOTAL_BUDGET_SECONDS`` every
    rotation (measured live: ``check_style.py`` AST-parses 456 files on
    i386/2GB and times out on every single run).

    It is reclassified into visible, fixable demand rather than excluded
    from selection. The exclusion design is the dangerous one: its only
    possible evidence is ``last_runs.jsonl``, which EVERY validator
    subprocess can append to, so a handful of forged rows would drop an
    arbitrary allowlisted validator from selection permanently — a script
    that never runs never produces a real row to break its own streak.
    ``test_forged_timeout_rows_cannot_exclude_a_script`` is the regression
    test for exactly that."""

    def test_timeout_is_reclassified(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_slow.py",
            "import time\ntime.sleep(30)\n",
            days_ago=2,
        )
        record = validator_harness._run_one(
            repo / "scripts" / "check_slow.py", repo, 1.0
        )
        assert record["exit_code"] is None
        assert record["harness_contract"] == "exceeds_time_budget"

    def test_ordinary_failure_is_not_marked_as_a_time_budget_problem(self, tmp_path):
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_fails.py",
            "import sys\nsys.stderr.write('real failure\\n')\nsys.exit(1)\n",
            days_ago=2,
        )
        record = validator_harness._run_one(
            repo / "scripts" / "check_fails.py", repo, 30.0
        )
        assert record["exit_code"] == 1
        assert "harness_contract" not in record

    def test_timeout_becomes_visible_demand(self, tmp_path):
        """Before #934 a timed-out run yielded no demand at all, because the
        presentation path only looked at integer exit codes."""
        state_dir = _state_dir(tmp_path)
        _seed_last_runs(
            state_dir,
            [
                {
                    "path": "scripts/check_slow.py",
                    "exit_code": None,
                    "stderr_tail": "timeout after 60s",
                    "harness_contract": "exceeds_time_budget",
                    "finished_at": "2026-08-23T01:24:10Z",
                }
            ],
        )
        items = demand._validator_defect_items(state_dir)
        assert len(items) == 1
        assert items[0]["affected_path"] == "scripts/check_slow.py"
        assert "cannot finish within the harness's per-script time budget" in (
            items[0]["summary"]
        )
        assert "timeout after 60s" in items[0]["evidence"]

    def test_a_plain_timeout_row_without_the_marker_still_yields_nothing(self, tmp_path):
        """The marker, not the absent exit code, is what makes a timeout
        visible — an unmarked row keeps the pre-#934 behaviour."""
        state_dir = _state_dir(tmp_path)
        _seed_last_runs(
            state_dir,
            [
                {
                    "path": "scripts/check_slow.py",
                    "exit_code": None,
                    "stderr_tail": "timeout after 60s",
                    "finished_at": "2026-08-23T01:24:10Z",
                }
            ],
        )
        assert demand._validator_defect_items(state_dir) == []

    def test_forged_timeout_rows_cannot_exclude_a_script(self, tmp_path):
        """#934, the reason exclusion was rejected: every validator
        subprocess can append to ``last_runs.jsonl``. If a streak of timeout
        rows dropped a script from selection, these three forged lines —
        naming a validator that is perfectly healthy — would silence it
        permanently. The script must still be selected and still run."""
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        script = _add_script(repo, "check_healthy.py", "print('fine')\n", days_ago=2)
        _seed_last_runs(
            state_dir,
            [
                {
                    "path": "scripts/check_healthy.py",
                    "exit_code": None,
                    "stderr_tail": "timeout after 60s",
                    "harness_contract": "exceeds_time_budget",
                    "source_mtime": script.stat().st_mtime,
                    "finished_at": f"2026-08-2{n}T01:00:00Z",
                }
                for n in (1, 2, 3)
            ],
        )

        result = validator_harness.run_validator_harness(state_dir, repo)

        assert result["ran"] == ["scripts/check_healthy.py"]
        rows = [r for r in _last_runs(state_dir) if r["path"] == "scripts/check_healthy.py"]
        assert rows[-1]["exit_code"] == 0
        assert "harness_contract" not in rows[-1]


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


# ─── #934: EROFS coverage, keyed on the TERMINAL stderr line only ────────


class TestSandboxDenialCoversErofsOnTerminalLine:
    """#934 Class A: the sandbox makes ``state/reports`` read-only, so a
    validator that writes its own report there crashes with ``OSError:
    [Errno 30] Read-only file system: ...`` -- same class as the EACCES
    case #928 fixed, but the old ``_SANDBOX_DENIAL_RE`` had no EROFS
    alternative at all.

    Narrowing to the TERMINAL non-empty stderr line (rather than searching
    the whole stream) is the other half: a validator can log a mid-run,
    non-fatal warning that happens to mention a read-only path and then go
    on to report a genuine failure on its last line (the
    ``analyze_repeat_failures.py`` shape below) -- whole-stderr matching
    would suppress that genuine finding, and it is not hypothetical."""

    def test_erofs_as_the_terminal_line_is_marked(self, tmp_path):
        """verify_and_proof.py-style: the validator's own report write
        fails with EROFS and that IS the last thing on stderr."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "verify_and_proof.py",
            "import sys\n"
            "sys.stderr.write('Traceback (most recent call last):\\n')\n"
            "sys.stderr.write(\"OSError: [Errno 30] Read-only file "
            "system: '/var/lib/eeepc-agent/self-evolving-agent/state/"
            "reports/proof-20260823T012648Z.json'\\n\")\n"
            "sys.exit(1)\n",
            days_ago=2,
        )
        record = validator_harness._run_one(
            repo / "scripts" / "verify_and_proof.py", repo, 30.0
        )
        assert record["exit_code"] == 1
        assert record["harness_env_error"] == "permission_denied"

    def test_erofs_midstream_with_genuine_terminal_failure_is_not_marked(
        self, tmp_path
    ):
        """analyze_repeat_failures.py-style: EROFS shows up mid-stream as
        non-fatal noise from a failed export, but the LAST line is a
        genuine, unrelated failure. That genuine failure must still reach
        the loop as demand -- it must NOT be suppressed just because an
        earlier line happened to mention a read-only path."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "analyze_repeat_failures.py",
            "import sys\n"
            "sys.stderr.write('Warning: Failed to export repeat failures "
            "to .../memory/repeat_failures.json: [Errno 30] Read-only "
            "file system: ...\\n')\n"
            "sys.stderr.write('Warning: Failed to export prevent repeats "
            "to .../memory/prevent_repeats.json: [Errno 30] Read-only "
            "file system: ...\\n')\n"
            "sys.stderr.write('ERROR: 1 unresolved failure signature(s) "
            "exceeded the retry budget of 2!\\n')\n"
            "sys.exit(1)\n",
            days_ago=2,
        )
        record = validator_harness._run_one(
            repo / "scripts" / "analyze_repeat_failures.py", repo, 30.0
        )
        assert record["exit_code"] == 1
        assert "harness_env_error" not in record

    def test_eacces_still_marked_when_it_is_the_terminal_line(self, tmp_path):
        """Regression pin: narrowing to the terminal line must not lose the
        #928 EACCES coverage that already existed."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_denies2.py",
            "import sys\n"
            "sys.stderr.write('some preamble\\n')\n"
            "sys.stderr.write('PermissionError: [Errno 13] Permission "
            "denied: x\\n')\n"
            "sys.exit(1)\n",
            days_ago=2,
        )
        record = validator_harness._run_one(
            repo / "scripts" / "check_denies2.py", repo, 30.0
        )
        assert record["exit_code"] == 1
        assert record["harness_env_error"] == "permission_denied"

    def test_eacces_midstream_with_genuine_terminal_failure_is_not_marked(
        self, tmp_path
    ):
        """Same silencing shape as the EROFS case above, for the EACCES
        alternatives that already existed pre-#934."""
        repo = _init_repo(tmp_path)
        _add_script(
            repo,
            "check_partial_denial.py",
            "import sys\n"
            "sys.stderr.write('PermissionError: [Errno 13] Permission "
            "denied: /some/inaccessible/path\\n')\n"
            "sys.stderr.write('ERROR: genuine validator failure\\n')\n"
            "sys.exit(1)\n",
            days_ago=2,
        )
        record = validator_harness._run_one(
            repo / "scripts" / "check_partial_denial.py", repo, 30.0
        )
        assert record["exit_code"] == 1
        assert "harness_env_error" not in record


# ─── #934 Class B: argparse usage errors are reclassified, not suppressed ─


class TestArgparseUsageContract:
    """#934: the harness invokes every selected script with NO arguments.
    A validator whose argparse requires a flag exits 2 on EVERY run,
    forever, and the false summary "validator X fails when run" sends the
    loop chasing a script that is not broken. This must be recorded
    distinguishably (``harness_contract``), never suppressed -- unlike
    ``harness_env_error``, the condition is genuinely fixable by the loop."""

    def test_argument_required_script_is_reclassified(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = (
            "import argparse, sys\n"
            "p = argparse.ArgumentParser(prog='validate_cycle_handoff.py')\n"
            "p.add_argument('--manifest')\n"
            "p.add_argument('--repo-root')\n"
            "p.add_argument('--json', action='store_true')\n"
            "p.add_argument('--test', action='store_true')\n"
            "args = p.parse_args()\n"
            "if not args.manifest and not args.test:\n"
            "    p.error('--manifest is required unless --test is used')\n"
        )
        _add_script(repo, "validate_cycle_handoff.py", script, days_ago=2)
        record = validator_harness._run_one(
            repo / "scripts" / "validate_cycle_handoff.py", repo, 30.0
        )
        assert record["exit_code"] == 2
        assert record["harness_contract"] == "requires_arguments"
        assert "harness_env_error" not in record

    def test_legitimate_exit_2_with_findings_is_not_reclassified(self, tmp_path):
        """A validator legitimately exiting 2 while printing findings text
        (not an argparse usage error) must not be caught."""
        repo = _init_repo(tmp_path)
        script = (
            "import sys\n"
            "print('2 findings detected in scripts/')\n"
            "sys.exit(2)\n"
        )
        _add_script(repo, "check_two.py", script, days_ago=2)
        record = validator_harness._run_one(
            repo / "scripts" / "check_two.py", repo, 30.0
        )
        assert record["exit_code"] == 2
        assert "harness_contract" not in record

    def test_usage_line_alone_without_error_line_is_not_reclassified(self, tmp_path):
        """A script that merely PRINTS something starting with 'usage:'
        (e.g. as part of its own help text) without argparse's paired
        '<prog>: error:' line must not be misclassified."""
        repo = _init_repo(tmp_path)
        script = (
            "import sys\n"
            "sys.stderr.write('usage: this is not an argparse error\\n')\n"
            "sys.exit(2)\n"
        )
        _add_script(repo, "check_usage_text.py", script, days_ago=2)
        record = validator_harness._run_one(
            repo / "scripts" / "check_usage_text.py", repo, 30.0
        )
        assert record["exit_code"] == 2
        assert "harness_contract" not in record

    def test_argparse_shape_at_a_different_exit_code_is_not_reclassified(self, tmp_path):
        """The detection is scoped to exit_code == 2 -- argparse's own exit
        code for a usage error -- not to the text shape alone."""
        repo = _init_repo(tmp_path)
        script = (
            "import sys\n"
            "sys.stderr.write('usage: check_weird.py [-h]\\n')\n"
            "sys.stderr.write('check_weird.py: error: something\\n')\n"
            "sys.exit(1)\n"
        )
        _add_script(repo, "check_weird.py", script, days_ago=2)
        record = validator_harness._run_one(
            repo / "scripts" / "check_weird.py", repo, 30.0
        )
        assert record["exit_code"] == 1
        assert "harness_contract" not in record


# ─── #928 round 2: _run_one must always return ──────────────────────────


class TestRunOneAlwaysReturns:
    """Round-2 review: an explicit ``proc.stdout.close()`` deadlocked
    ``_run_one`` — ``close()`` wants the same io lock a reader thread holds
    while blocked in ``read()``, which is exactly the state once its
    ``join(timeout=5)`` has expired. The close does eventually return, once
    the last process holding the pipe write end exits — measured at 20.2s
    against a 20s detached sleeper, versus 10.1s for the same run without
    it. Round 2 read that as "never returns" because the grandchild there
    outlived the observation window; the effect is a stall proportional to
    however long the escaped process runs, which is unbounded in principle
    and was 120s in the first version of this very test.

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
        # returns once the last pipe writer exits — measured at 20.2s against
        # a 20s detached sleeper, versus 10.1s for the same run without the
        # close. So with the 30s grandchild above, a reintroduced
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
    def test_does_not_consult_getpgid(self, monkeypatch):
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
            # monkeypatch rather than a hand-rolled save/restore: this patches
            # the real os module attribute process-wide, so pytest undoing it
            # is safer than a finally block that a failure could skip.
            monkeypatch.setattr(
                validator_harness.os, "getpgid", lambda _pid: os.getpgrp()
            )
            assert validator_harness._process_group_id(proc) == proc.pid
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

    def test_line_trim_cannot_evict_another_scripts_verdict(self, tmp_path):
        """#928 round-4 review: the byte budget got a per-path pass, but the
        LINE trim was still a raw tail slice applied BEFORE it — so the
        two-pass invariant never saw the rows the slice had already thrown
        away. That made eviction 100x cheaper than the megabyte flood: 500
        minimal rows is ~25 KB, nowhere near either byte bound, and the
        harness itself performs the deletion on its very next append. No
        attacker timing is required."""
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        sidecar = d / "last_runs.jsonl"

        validator_harness._append_last_run(
            tmp_path,
            {"path": "scripts/check_victim.py", "exit_code": 1,
             "stderr_tail": "genuine failure"},
        )
        assert [i["affected_path"] for i in demand._validator_defect_items(tmp_path)] == [
            "scripts/check_victim.py"
        ]

        # The hostile validator writes directly into the one writable
        # carve-out it shares with the harness.
        with sidecar.open("a", encoding="utf-8") as fh:
            for _ in range(validator_harness._MAX_LAST_RUNS_LINES):
                fh.write(json.dumps(
                    {"path": "scripts/check_evil.py", "exit_code": 0}
                ) + "\n")
        assert sidecar.stat().st_size < validator_harness._MAX_LAST_RUNS_KEEP_BYTES

        # ...and then the harness records that validator's own run.
        validator_harness._append_last_run(
            tmp_path, {"path": "scripts/check_evil.py", "exit_code": 0}
        )

        assert "scripts/check_victim.py" in self._paths_in(sidecar)
        assert [i["affected_path"] for i in demand._validator_defect_items(tmp_path)] == [
            "scripts/check_victim.py"
        ]

    def test_size_ladder_cannot_starve_the_newest_per_path_pass(self, tmp_path):
        """#928 round-5 review: pass 1 is bounded by the NUMBER of distinct
        candidate paths, but each row's SIZE is attacker-chosen up to the
        per-line cap — so with newest-path-first admission a validator could
        forge one padded row per other real candidate, sized as a DESCENDING
        LADDER, and drive the residual slack below the size of the victim's
        genuine row. Uniform padding does NOT reproduce it (greedy admission
        leaves a whole row's worth of slack, which the small genuine row then
        fits into); the ladder is what closes the gap, and it is why this
        test is built the awkward way it is.

        Admitting the newest-per-path set SMALLEST FIRST makes the class
        unreachable: the genuine 100-odd-byte row is taken before any padded
        forgery, so padding a forgery only makes it the first thing dropped.

        Measured against the parent commit: victim row deleted from disk,
        file at 1,048,504 B. With the fix: victim kept, file 1,032,198 B."""
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        sidecar = d / "last_runs.jsonl"

        def sized_row(rel: str, encoded_target: int) -> str:
            base = json.dumps({"path": rel, "exit_code": 0, "stderr_tail": ""})
            pad = encoded_target - 1 - len(base)
            return json.dumps(
                {"path": rel, "exit_code": 0, "stderr_tail": "z" * pad}
            )

        cap_line = validator_harness._MAX_LAST_RUNS_LINE_BYTES
        ladder = [cap_line] * 63 + [8192, 4096, 2048, 1024, 512, 256, 128]
        rels = {f"scripts/check_p{i:04d}.py" for i in range(len(ladder))}
        rels |= {"scripts/check_victim.py", "scripts/check_zzz_runner.py"}

        with sidecar.open("w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(
                {"path": "scripts/check_victim.py", "exit_code": 1,
                 "stderr_tail": "genuine"}
            ) + "\n")
            # Ascending in the file, so a newest-first pass sees it descending.
            for offset, size in enumerate(reversed(ladder)):
                index = len(ladder) - 1 - offset
                fh.write(sized_row(f"scripts/check_p{index:04d}.py", size) + "\n")

        # The harness records a run for a path that is NOT one of the forged
        # ones, so its append cannot free the attacker's own budget slot.
        validator_harness._append_last_run(
            tmp_path, {"path": "scripts/check_zzz_runner.py", "exit_code": 0}, rels
        )

        assert "scripts/check_victim.py" in self._paths_in(sidecar)
        assert [i["affected_path"] for i in demand._validator_defect_items(tmp_path)] == [
            "scripts/check_victim.py"
        ]

    def test_atomic_write_leaves_no_temp_file(self, tmp_path):
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "last_runs.jsonl"
        validator_harness._atomic_write(target, "x\n")
        assert target.read_text(encoding="utf-8") == "x\n"
        assert list(d.glob("*.tmp")) == []

    def test_atomic_write_leaves_no_temp_file_when_replace_fails(self, tmp_path, monkeypatch):
        """The success path alone does not test the cleanup — the code before
        the ``finally`` left no temp there either. The failure path is the
        one that used to orphan a uuid-named file in a directory nothing
        prunes and nothing bounds."""
        d = tmp_path / "validator_harness"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "last_runs.jsonl"
        target.write_text("original\n", encoding="utf-8")

        def boom(self, _target):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(Path, "replace", boom)
        with pytest.raises(OSError):
            validator_harness._atomic_write(target, "replacement\n")

        assert list(d.glob("*.tmp")) == []
        assert target.read_text(encoding="utf-8") == "original\n"


# ─── #934 review round 1 fixes ───────────────────────────────────────────


class TestSqueezedRunIsNeverBlamedOnTheScript:
    """#934 review RED: `_run_one` used to be handed
    ``min(_PER_SCRIPT_TIMEOUT, remaining)``, so the last script of a rotation
    got whatever the total budget had left. Once a timeout became demand,
    that turned a conformant validator into permanent false demand — and it
    was guaranteed on the live host, where one script eats 60s of the 240s
    every rotation."""

    def test_a_script_is_never_started_without_the_full_contract(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(validator_harness, "_PER_SCRIPT_TIMEOUT", 5.0)
        monkeypatch.setattr(validator_harness, "_TOTAL_BUDGET_SECONDS", 7.0)
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        # Sleeps past its own cap, consuming ~5s of the 7s budget.
        _add_script(repo, "check_a_slow.py", "import time\ntime.sleep(30)\n", days_ago=2)
        # Needs 2s, well inside the 5s contract — but only ~2s of budget is
        # left when its turn comes, so it must not be started at all.
        _add_script(repo, "check_b_healthy.py", "import time\ntime.sleep(2)\n", days_ago=2)

        result = validator_harness.run_validator_harness(state_dir, repo)

        assert result["ran"] == ["scripts/check_a_slow.py"]
        rows = _last_runs(state_dir)
        assert [r["path"] for r in rows] == ["scripts/check_a_slow.py"]
        # And the healthy script keeps its never-run rotation position, so it
        # is first in line next invocation rather than being penalised.
        assert "scripts/check_b_healthy.py" not in _rotation(state_dir)["served"]

    def test_only_the_genuinely_over_budget_script_becomes_demand(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(validator_harness, "_PER_SCRIPT_TIMEOUT", 5.0)
        monkeypatch.setattr(validator_harness, "_TOTAL_BUDGET_SECONDS", 7.0)
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_a_slow.py", "import time\ntime.sleep(30)\n", days_ago=2)
        _add_script(repo, "check_b_healthy.py", "import time\ntime.sleep(2)\n", days_ago=2)

        validator_harness.run_validator_harness(state_dir, repo)
        items = demand._validator_defect_items(state_dir)

        assert [i["affected_path"] for i in items] == ["scripts/check_a_slow.py"]


class TestTruncatedStderrIsNotClassified:
    """#934 review YELLOW: ``_drain_capped`` keeps the HEAD of the stream, so
    for any stderr past ``_MAX_OUTPUT_BYTES`` the last captured line is not
    the last line the script wrote — it is whatever sat at the cap boundary,
    at an offset the script itself chooses. A hostile validator could pad so
    that a read-only-path line lands there, collect ``harness_env_error``,
    and have demand skip the genuine failure it reported afterwards. Refusing
    to classify a truncated stream fails open toward a visible defect."""

    def test_denial_at_the_truncation_boundary_is_not_marked(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(validator_harness, "_MAX_OUTPUT_BYTES", 4096)
        repo = _init_repo(tmp_path)
        # Alignment is the whole attack: the denial line is exactly 64 chars
        # including its newline, and the cap is 4096, so the kept HEAD ends
        # flush at the end of the 64th denial line. That makes the last
        # CAPTURED line a denial even though the last WRITTEN line is the
        # genuine failure below — which is the shape the padding buys.
        script = (
            "import sys\n"
            "body = '[Errno 30] Read-only file system: '\n"
            "line = body + 'x' * (63 - len(body))\n"
            "assert len(line) + 1 == 64\n"
            "for _ in range(100):\n"
            "    sys.stderr.write(line + chr(10))\n"
            "sys.stderr.write('ERROR: 3 unresolved failure signatures!' + chr(10))\n"
            "sys.stderr.flush()\n"
            "sys.exit(1)\n"
        )
        _add_script(repo, "check_padded.py", script, days_ago=2)

        record = validator_harness._run_one(
            repo / "scripts" / "check_padded.py", repo, 30.0
        )

        assert record["exit_code"] == 1
        # The genuine terminal line was discarded by the cap, so the harness
        # must not pretend to know what the run's last word was.
        assert "harness_env_error" not in record

    def test_an_untruncated_denial_is_still_marked(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = (
            "import sys\n"
            "sys.stderr.write('OSError: [Errno 30] Read-only file system: /x' + chr(10))\n"
            "sys.exit(1)\n"
        )
        _add_script(repo, "check_erofs.py", script, days_ago=2)
        record = validator_harness._run_one(
            repo / "scripts" / "check_erofs.py", repo, 30.0
        )
        assert record["harness_env_error"] == "permission_denied"


class TestForgedRotationStampCannotExcludeAScript:
    """#934 review YELLOW: ``rotation.json`` sits in the same writable
    carve-out as the sidecar, and ``served[rel]`` is only overwritten for
    scripts that actually RAN. A forged far-future stamp therefore used to
    sort its target last forever — never selected, never run, never
    overwriting the forgery. That is durable, self-maintaining exclusion of
    any allowlisted validator from a single JSON write: precisely the
    capability #934 refused to add via a timeout streak, already reachable
    by another route. ``_rotation_key`` now treats a future stamp as
    never-run, so the forgery buys priority instead of silence."""

    def test_future_stamp_does_not_exclude(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validator_harness, "_DEFAULT_MAX_K", 1)
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(repo, "validate_no_eval_exec.py", "print('safety')\n", days_ago=2)
        _add_script(repo, "validate_zz_other.py", "print('other')\n", days_ago=2)
        rotation_path = state_dir / "validator_harness" / "rotation.json"
        rotation_path.parent.mkdir(parents=True, exist_ok=True)
        rotation_path.write_text(
            json.dumps(
                {
                    "schema_version": validator_harness._ROTATION_SCHEMA,
                    "served": {
                        "scripts/validate_no_eval_exec.py": "3000-01-01T00:00:00+00:00"
                    },
                }
            ),
            encoding="utf-8",
        )

        result = validator_harness.run_validator_harness(state_dir, repo)

        # The forged target is treated as never-run, so it sorts FIRST.
        assert result["ran"] == ["scripts/validate_no_eval_exec.py"]
        served = _rotation(state_dir)["served"]
        assert served["scripts/validate_no_eval_exec.py"] != "3000-01-01T00:00:00+00:00"

    def test_an_ordinary_past_stamp_still_orders_the_rotation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validator_harness, "_DEFAULT_MAX_K", 1)
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(repo, "validate_aa_recent.py", "print('a')\n", days_ago=2)
        _add_script(repo, "validate_zz_old.py", "print('z')\n", days_ago=2)
        rotation_path = state_dir / "validator_harness" / "rotation.json"
        rotation_path.parent.mkdir(parents=True, exist_ok=True)
        rotation_path.write_text(
            json.dumps(
                {
                    "schema_version": validator_harness._ROTATION_SCHEMA,
                    "served": {
                        "scripts/validate_aa_recent.py": _iso(
                            datetime.now(timezone.utc) - timedelta(hours=1)
                        ),
                        "scripts/validate_zz_old.py": _iso(
                            datetime.now(timezone.utc) - timedelta(days=9)
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )

        result = validator_harness.run_validator_harness(state_dir, repo)

        assert result["ran"] == ["scripts/validate_zz_old.py"]


class TestJsonFlagMustBeDeclaredNotMentioned:
    """#934 review YELLOW: ``_accepts_json_flag`` was a bare mention test —
    the same defect shape as the old ``_ARCHIVED_RE``, one function away. A
    decay/pattern auditor that merely NAMES ``--json`` got the flag appended,
    argparse rejected it with ``unrecognized arguments``, and the run failed
    for a reason the script had no part in — then got a demand summary
    asserting it "requires command-line arguments", the exact opposite of the
    truth. The 14 validators #934 returns to service are that class."""

    def test_a_mention_does_not_get_the_flag(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = (
            "import argparse\n"
            "PATTERNS = ['--json', '--verbose']  # strings this auditor looks FOR\n"
            "p = argparse.ArgumentParser(prog='validate_flag_coverage.py')\n"
            "args = p.parse_args()\n"
            "print('scanned', len(PATTERNS))\n"
        )
        path = _add_script(repo, "validate_flag_coverage.py", script, days_ago=2)
        assert validator_harness._accepts_json_flag(path) is False
        record = validator_harness._run_one(path, repo, 30.0)
        assert record["exit_code"] == 0
        assert "harness_contract" not in record

    def test_a_declaration_still_gets_the_flag(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = (
            "import argparse, json\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--json', action='store_true')\n"
            "a = p.parse_args()\n"
            "print(json.dumps({'findings': [1, 2]}) if a.json else 'plain')\n"
        )
        path = _add_script(repo, "validate_declares_json.py", script, days_ago=2)
        assert validator_harness._accepts_json_flag(path) is True
        record = validator_harness._run_one(path, repo, 30.0)
        assert record["findings_count"] == 2

    def test_short_alias_before_the_long_option_still_counts(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = (
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('-j', '--json', action='store_true')\n"
            "p.parse_args()\n"
            "print('{}')\n"
        )
        path = _add_script(repo, "validate_alias_json.py", script, days_ago=2)
        assert validator_harness._accepts_json_flag(path) is True

    def test_sys_argv_idiom_counts(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = "import sys\nprint('{}' if '--json' in sys.argv else 'plain')\n"
        path = _add_script(repo, "validate_argv_json.py", script, days_ago=2)
        assert validator_harness._accepts_json_flag(path) is True

    def test_over_supply_is_not_labelled_as_requiring_arguments(self, tmp_path):
        """argparse's ``unrecognized arguments`` is the OVER-supply shape.
        Labelling it "requires command-line arguments" would be false."""
        stderr = (
            "usage: validate_x.py [-h]\n"
            "validate_x.py: error: unrecognized arguments: --json\n"
        )
        assert validator_harness._is_argparse_usage_error(stderr) is False

    def test_under_supply_is_still_labelled(self, tmp_path):
        stderr = (
            "usage: validate_x.py [-h] [--manifest MANIFEST]\n"
            "validate_x.py: error: --manifest is required unless --test is used\n"
        )
        assert validator_harness._is_argparse_usage_error(stderr) is True


class TestDecayDeclarationSpanningLines:
    """#934 review round 2: the review asked for a same-line rule (phrase and
    own path on one line), because co-occurrence anywhere in the head is loose
    now that the roadmap doc publishes the phrase verbatim. Measured against
    the live instance repo, that rule excludes 11 of 43 where co-occurrence
    excludes 13 — and the two it loses are exactly the "scheduled for removal"
    pair this issue set out to START excluding. Their declarations are not
    single-line: the guard script carries the phrase in its module docstring
    and its own path four lines later, and its runtime WARNING string is split
    across two adjacent literals so the phrase itself spans a line break.

    These two tests pin the real shapes, so a future tightening cannot quietly
    regress Class C again."""

    def test_docstring_phrase_with_the_path_lines_later_is_excluded(self, tmp_path):
        repo = _init_repo(tmp_path)
        # The live verify_eeepc_self_evolving_service_guard.py shape.
        script = (
            '"""Guard check.\n'
            "\n"
            "This script is deprecated and scheduled for removal after 14+ days"
            " of disuse.\n"
            "\n"
            "Replacement:\n"
            "        scripts/verify_eeepc_self_evolving_service_guard.py\n"
            '"""\n'
            "import warnings\n"
            "warnings.warn(\n"
            '    "WARNING: scripts/verify_eeepc_self_evolving_service_guard.py '
            'is deprecated "\n'
            '    "and scheduled for removal after 14+ days of disuse.",\n'
            "    DeprecationWarning,\n"
            "    stacklevel=1,\n"
            ")\n"
            "raise SystemExit(2)\n"
        )
        _add_script(
            repo, "verify_eeepc_self_evolving_service_guard.py", script, days_ago=2
        )
        names = {p.name for p in validator_harness._candidate_scripts(repo)}
        assert "verify_eeepc_self_evolving_service_guard.py" not in names

    def test_a_real_single_line_declaration_is_still_excluded(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = (
            "import warnings\n"
            'msg = "WARNING: scripts/analyze_repo_size.py is deprecated and '
            'marked as archived (decay-36bd86468443) as unused."\n'
            "warnings.warn(msg, DeprecationWarning, stacklevel=1)\n"
            "raise SystemExit(1)\n"
        )
        _add_script(repo, "analyze_repo_size.py", script, days_ago=2)
        names = {p.name for p in validator_harness._candidate_scripts(repo)}
        assert "analyze_repo_size.py" not in names


class TestUsageErrorIsNeverSuppressedByTheEnvMarker:
    """#934 review GREEN-6: ``harness_env_error`` makes demand skip a row
    entirely, and it was reachable together with the argparse contract —
    ``argparse.FileType`` on a required flag reports EACCES as its terminal
    line. The claim that a contract mismatch is "never suppressed" has to be
    enforced, not asserted."""

    def test_argparse_error_mentioning_a_denial_still_becomes_demand(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = (
            "import sys\n"
            "sys.stderr.write('usage: validate_ft.py [-h] --manifest MANIFEST' + chr(10))\n"
            "sys.stderr.write(\"validate_ft.py: error: argument --manifest: \"\n"
            "                 \"can't open '/x': [Errno 13] Permission denied\" + chr(10))\n"
            "sys.exit(2)\n"
        )
        _add_script(repo, "validate_ft.py", script, days_ago=2)
        record = validator_harness._run_one(repo / "scripts" / "validate_ft.py", repo, 30.0)

        assert record["harness_contract"] == "requires_arguments"
        assert "harness_env_error" not in record

        state_dir = _state_dir(tmp_path)
        _seed_last_runs(state_dir, [record])
        items = demand._validator_defect_items(state_dir)
        assert len(items) == 1
        assert "requires command-line arguments" in items[0]["summary"]


# ─── #934 review round 2 fixes ───────────────────────────────────────────


class TestRotationKeyCannotKillTheHarness:
    """#934 review round 2 RED: ``_rotation_key`` formatted the stamp with
    ``_iso``, whose ``astimezone`` raises ``OverflowError`` outside
    ``datetime``'s range. One forged line reached it — the value parses, is
    not in the future, and then underflows — and the raise propagated out of
    ``eligible.sort(key=...)`` into the outer handler, aborting the whole
    invocation BEFORE either ``_write_rotation`` call. So the poison was
    never overwritten: the entire validator fleet was dead permanently while
    ``main()`` still reported success to systemd. Total silencing from one
    line, strictly worse than the per-script class #934 closes."""

    POISON = "0001-01-01T00:00:00+14:00"

    def _seed_poison(self, state_dir: Path, rel: str) -> Path:
        path = state_dir / "validator_harness" / "rotation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": validator_harness._ROTATION_SCHEMA,
                    "served": {rel: self.POISON},
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_key_does_not_raise_and_sorts_the_script_first(self, tmp_path):
        repo = _init_repo(tmp_path)
        script = _add_script(repo, "check_ok.py", "print('fine')\n", days_ago=2)
        other = _add_script(repo, "check_recent.py", "print('fine')\n", days_ago=2)
        served = {
            "scripts/check_ok.py": self.POISON,
            "scripts/check_recent.py": _iso(datetime.now(timezone.utc)),
        }

        # The point is that this returns at all: `_iso` raised OverflowError
        # here. A year-1 stamp is a legitimately ancient one, so it sorts
        # ahead of a just-run script rather than being clamped away — the
        # same practical outcome as never-run, and it gets overwritten by the
        # real stamp as soon as the script runs.
        poisoned = validator_harness._rotation_key(script, served)
        recent = validator_harness._rotation_key(other, served)
        assert poisoned < recent

    def test_a_poisoned_stamp_does_not_kill_the_invocation(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_ok.py", "print('fine')\n", days_ago=2)
        self._seed_poison(state_dir, "scripts/check_ok.py")

        result = validator_harness.run_validator_harness(state_dir, repo)

        assert result["errors"] == []
        assert result["ran"] == ["scripts/check_ok.py"]
        # And the poison is gone, so it cannot be permanent.
        assert _rotation(state_dir)["served"]["scripts/check_ok.py"] != self.POISON

    def test_it_does_not_survive_repeated_invocations(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_ok.py", "print('fine')\n", days_ago=2)
        self._seed_poison(state_dir, "scripts/check_ok.py")

        runs = [
            validator_harness.run_validator_harness(state_dir, repo)
            for _ in range(3)
        ]

        assert all(r["ran"] == ["scripts/check_ok.py"] for r in runs)
        assert all(r["errors"] == [] for r in runs)


class TestFutureStampsBuyDelayNotSilence:
    """#934 review round 2: the single-entry future-stamp test cannot detect
    the reverse channel — forging future stamps for MANY candidates to starve
    them out of the K-sized selection. The clamp's claimed property is that
    priority buys at most delay, never exclusion, so pin it: every candidate
    must still run within ceil(N/K) invocations."""

    def test_every_candidate_still_runs_within_a_full_sweep(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validator_harness, "_DEFAULT_MAX_K", 2)
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        names = [f"check_s{i}.py" for i in range(6)]
        for name in names:
            _add_script(repo, name, "print('ok')\n", days_ago=2)
        # Every candidate forged into the future, not just one.
        path = state_dir / "validator_harness" / "rotation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": validator_harness._ROTATION_SCHEMA,
                    "served": {f"scripts/{n}": "3000-01-01T00:00:00+00:00" for n in names},
                }
            ),
            encoding="utf-8",
        )

        ran: set[str] = set()
        for _ in range(3):  # ceil(6 / 2)
            ran.update(validator_harness.run_validator_harness(state_dir, repo)["ran"])

        assert ran == {f"scripts/{n}" for n in names}


class TestJsonFlagDeclarationForms:
    """#934 review round 2: the declaration test must not miss real
    non-argparse declarations. A miss is not a lost parse — for a validator
    that reports findings while exiting 0, no ``--json`` means no JSON on
    stdout, ``findings_count`` None, exit 0, and therefore no demand item at
    all: its findings stop reaching the loop entirely."""

    @pytest.mark.parametrize(
        "source",
        [
            'p.add_argument("--json", action="store_true")\n',
            "p.add_argument('-j', '--json')\n",
            'p.add_argument(\n    "--json",\n    action="store_true",\n)\n',
            '@click.option("--json", is_flag=True)\n',
            'parser.add_option("--json", dest="as_json")\n',
            'if "--json" in sys.argv:\n',
            'argv = sys.argv[1:]\nif "--json" in argv:\n',
        ],
    )
    def test_declaration_forms_are_matched(self, source):
        assert validator_harness._JSON_FLAG_DECL_RE.search(source)

    @pytest.mark.parametrize(
        "source",
        [
            'PATTERNS = ["--json", "--verbose"]\n',
            '"""Usage: python scripts/x.py --json"""\n',
            'p.add_argument("--json-output")\n',
            'p.add_argument("--no-json")\n',
        ],
    )
    def test_mentions_and_near_misses_are_not_matched(self, source):
        assert not validator_harness._JSON_FLAG_DECL_RE.search(source)


class TestForwardProgressFloor:
    """#934 review round 2: without a floor, a configuration where the
    per-script timeout meets or exceeds the total budget would select
    scripts, run none, report no errors, and exit 0 — a silently dead unit
    that systemd calls successful, the same signature as the RED above."""

    def test_the_first_script_always_runs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validator_harness, "_PER_SCRIPT_TIMEOUT", 10.0)
        monkeypatch.setattr(validator_harness, "_TOTAL_BUDGET_SECONDS", 1.0)
        state_dir = _state_dir(tmp_path)
        repo = _init_repo(tmp_path)
        _add_script(repo, "check_one.py", "print('ok')\n", days_ago=2)
        _add_script(repo, "check_two.py", "print('ok')\n", days_ago=2)

        result = validator_harness.run_validator_harness(state_dir, repo)

        assert result["ran"] == ["scripts/check_one.py"]
        assert result["errors"] == []
