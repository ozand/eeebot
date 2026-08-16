"""Tests for #780: the held-out verification pack.

Covers: runner dispatch against a fixture "instance repo" (correct
archive_old_reports passes, a dry-run-that-deletes version fails; dashboard
degrade-on-empty passes, a crashing dashboard fails), missing artifacts not
checked, timeout → skip, content-hash reuse on the second run, the
results.json shape, sandbox isolation (fixture repo untouched; scripts
cannot write outside the tmpdir), the scorecard `heldout` section +
`heldout_gap` target breach → goal-gap, fail-open zeros on missing results,
defect-demand emission with checker evidence, and the invisibility
invariant (nothing instance-facing references the pack; no fixtures on
disk in the package).
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from nanobot.runtime import demand, heldout, scorecard
from nanobot.runtime.heldout import checkers

NOW = datetime.now(timezone.utc)

# ─── fixture instance-repo scripts ──────────────────────────────────────────

GOOD_ARCHIVE = '''\
"""Archive old reports: state/reports/*.json older than 30d into monthly tar.gz."""
import argparse, tarfile, time
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    reports = Path("state/reports")
    cutoff = time.time() - 30 * 86400
    by_month = {}
    for p in sorted(reports.glob("*.json")):
        if p.stat().st_mtime < cutoff:
            by_month.setdefault(time.strftime("%Y-%m", time.gmtime(p.stat().st_mtime)), []).append(p)
    if not args.apply:
        for month, files in by_month.items():
            for p in files:
                print("would archive", p, "->", month)
        return
    arch = reports / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    for month, files in by_month.items():
        with tarfile.open(arch / f"{month}.tar.gz", "w:gz") as tar:
            for p in files:
                tar.add(p, arcname=p.name)
        for p in files:
            p.unlink()

main()
'''

# Reward-hacked/buggy variant: "dry-run" actually deletes the old files.
BAD_ARCHIVE = '''\
"""Archive old reports (buggy: dry-run deletes)."""
import argparse, time
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.parse_args()
reports = Path("state/reports")
cutoff = time.time() - 30 * 86400
for p in sorted(reports.glob("*.json")):
    if p.stat().st_mtime < cutoff:
        p.unlink()  # mutates even without --apply
        print("archived", p)
'''

GOOD_DASHBOARD = '''\
"""Dashboard: loop-health from state/ledger/cycles.jsonl, degrades on empty."""
import json
from pathlib import Path
ledger = Path("state/ledger/cycles.jsonl")
if not ledger.is_file():
    print("Loop health: no ledger yet")
else:
    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    outcomes = sum(1 for r in rows if r.get("phase") == "outcome")
    print(f"Loop health: {outcomes} outcomes; Demand status: ok")
'''

CRASHING_DASHBOARD = '''\
"""Dashboard that crashes when the ledger is missing."""
rows = open("state/ledger/cycles.jsonl").read().splitlines()
print("Loop health:", len(rows))
'''

GOOD_SYSTEM_MAP = '''\
"""Regenerate docs/SYSTEM_MAP.md — one line per script in scripts/."""
import ast
from pathlib import Path
lines = ["# SYSTEM MAP", ""]
for p in sorted(Path("scripts").glob("*.py")):
    try:
        doc = (ast.get_docstring(ast.parse(p.read_text())) or "").splitlines()
        purpose = doc[0] if doc else "(no docstring)"
    except Exception:
        purpose = "(unparseable)"
    lines.append(f"- {p.name}: {purpose}")
Path("docs").mkdir(exist_ok=True)
Path("docs/SYSTEM_MAP.md").write_text("\\n".join(lines) + "\\n")
'''

SMOKE_OK = '''\
"""Loop health report from state/ledger/cycles.jsonl."""
print("loop health: ok")
'''

SLEEPER = '''\
import time
time.sleep(30)
'''

ESCAPER = '''\
"""Tries to write outside its cwd (into the parent tree)."""
from pathlib import Path
Path("pwned.txt").write_text("cwd write")
print("Loop health: fine")
'''


def _make_repo(tmp_path: Path, scripts: dict[str, str]) -> Path:
    repo = tmp_path / "instance-repo"
    (repo / "scripts").mkdir(parents=True)
    for name, source in scripts.items():
        (repo / "scripts" / name).write_text(source, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "scripts"],
        check=True,
        capture_output=True,
    )
    return repo


def _repo_snapshot(repo: Path) -> set[tuple[str, bytes]]:
    return {
        (str(p.relative_to(repo)), p.read_bytes())
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def _write_results(state_dir: Path, results: dict, *, regressions: list | None = None) -> None:
    payload = {
        "schema_version": heldout.HELDOUT_SCHEMA,
        "git_head": "abc",
        "checked_at_utc": NOW.isoformat().replace("+00:00", "Z"),
        "results": results,
    }
    if regressions is not None:
        payload["regressions"] = regressions
    path = state_dir / "heldout" / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ─── runner ─────────────────────────────────────────────────────────────────


class TestRunner:
    def test_good_scripts_pass_and_results_shape(self, tmp_path):
        repo = _make_repo(
            tmp_path,
            {
                "archive_old_reports.py": GOOD_ARCHIVE,
                "eeebot_dashboard.py": GOOD_DASHBOARD,
                "generate_system_map.py": GOOD_SYSTEM_MAP,
                "loop_health_report.py": SMOKE_OK,
            },
        )
        state_dir = tmp_path / "state"
        data = heldout.run_heldout(state_dir, repo, force=True)
        assert data["schema_version"] == heldout.HELDOUT_SCHEMA
        assert data["git_head"]
        assert data["checked_at_utc"]
        results = data["results"]
        # prune_failed_backlog.py is registered but absent — not checked.
        assert set(results) == {
            "scripts/archive_old_reports.py",
            "scripts/eeebot_dashboard.py",
            "scripts/generate_system_map.py",
            "scripts/loop_health_report.py",
        }
        for artifact, entry in results.items():
            assert entry["status"] == "pass", (artifact, entry)
            assert entry["evidence"]
            assert entry["content_hash"]
            assert entry["ts"]
        # Persisted to the sidecar.
        on_disk = json.loads((state_dir / "heldout" / "results.json").read_text())
        assert on_disk["results"] == results

    def test_dry_run_that_mutates_fails(self, tmp_path):
        repo = _make_repo(tmp_path, {"archive_old_reports.py": BAD_ARCHIVE})
        data = heldout.run_heldout(tmp_path / "state", repo, force=True)
        entry = data["results"]["scripts/archive_old_reports.py"]
        assert entry["status"] == "fail"
        assert "dry-run" in entry["evidence"]

    def test_crashing_dashboard_fails(self, tmp_path):
        repo = _make_repo(tmp_path, {"eeebot_dashboard.py": CRASHING_DASHBOARD})
        data = heldout.run_heldout(tmp_path / "state", repo, force=True)
        entry = data["results"]["scripts/eeebot_dashboard.py"]
        assert entry["status"] == "fail"
        assert "empty state" in entry["evidence"]

    def test_timeout_becomes_skip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(heldout, "_CHECK_TIMEOUT_SECONDS", 1.0)
        repo = _make_repo(tmp_path, {"loop_health_report.py": SLEEPER})
        data = heldout.run_heldout(tmp_path / "state", repo, force=True)
        entry = data["results"]["scripts/loop_health_report.py"]
        assert entry["status"] == "skip"
        assert "timed out" in entry["evidence"]

    def test_checker_exception_becomes_skip(self, tmp_path, monkeypatch):
        def _boom(ctx):
            raise RuntimeError("checker bug")

        monkeypatch.setitem(checkers.CHECKERS, "scripts/loop_health_report.py", _boom)
        repo = _make_repo(tmp_path, {"loop_health_report.py": SMOKE_OK})
        data = heldout.run_heldout(tmp_path / "state", repo, force=True)
        entry = data["results"]["scripts/loop_health_report.py"]
        assert entry["status"] == "skip"
        assert "checker bug" in entry["evidence"]

    def test_content_hash_skips_recheck(self, tmp_path, monkeypatch):
        # A changed non-skip verdict is confirmed with _FLAKY_CONFIRM_RUNS
        # checker calls (#842) before being trusted stable, not just 1.
        calls = {"n": 0}

        def _counting(ctx):
            calls["n"] += 1
            return "pass", "counted"

        monkeypatch.setitem(checkers.CHECKERS, "scripts/loop_health_report.py", _counting)
        repo = _make_repo(tmp_path, {"loop_health_report.py": SMOKE_OK})
        state_dir = tmp_path / "state"
        heldout.run_heldout(state_dir, repo, force=True)
        assert calls["n"] == heldout._FLAKY_CONFIRM_RUNS
        # Second forced run: content unchanged → verdict reused, no re-exec.
        heldout.run_heldout(state_dir, repo, force=True)
        assert calls["n"] == heldout._FLAKY_CONFIRM_RUNS
        # Content change → re-checked (confirmed again).
        (repo / "scripts" / "loop_health_report.py").write_text(
            SMOKE_OK + "# changed\n", encoding="utf-8"
        )
        heldout.run_heldout(state_dir, repo, force=True)
        assert calls["n"] == heldout._FLAKY_CONFIRM_RUNS * 2

    def test_head_time_watermark_no_op(self, tmp_path, monkeypatch):
        calls = {"n": 0}

        def _counting(ctx):
            calls["n"] += 1
            return "pass", "counted"

        monkeypatch.setitem(checkers.CHECKERS, "scripts/loop_health_report.py", _counting)
        repo = _make_repo(tmp_path, {"loop_health_report.py": SMOKE_OK})
        state_dir = tmp_path / "state"
        first = heldout.run_heldout(state_dir, repo)
        assert calls["n"] == heldout._FLAKY_CONFIRM_RUNS
        # Same HEAD, fresh timestamp → whole run is a watermark no-op.
        second = heldout.run_heldout(state_dir, repo)
        assert second == first
        assert calls["n"] == heldout._FLAKY_CONFIRM_RUNS

    def test_no_repo_and_missing_repo_fail_open(self, tmp_path):
        assert heldout.run_heldout(tmp_path / "state", None)["results"] == {}
        assert (
            heldout.run_heldout(tmp_path / "state", tmp_path / "nope")["results"] == {}
        )

    # ─── regressions (#841: pass -> fail flips) ─────────────────────────────

    def test_regression_pass_to_fail_recorded(self, tmp_path):
        repo = _make_repo(tmp_path, {"archive_old_reports.py": GOOD_ARCHIVE})
        state_dir = tmp_path / "state"
        first = heldout.run_heldout(state_dir, repo, force=True)
        assert first["results"]["scripts/archive_old_reports.py"]["status"] == "pass"
        assert first["regressions"] == []
        # Swap the passing script for the reward-hacked variant — same
        # artifact path, new content → rechecked, now fails.
        (repo / "scripts" / "archive_old_reports.py").write_text(
            BAD_ARCHIVE, encoding="utf-8"
        )
        second = heldout.run_heldout(state_dir, repo, force=True)
        assert second["results"]["scripts/archive_old_reports.py"]["status"] == "fail"
        assert second["regressions"] == ["scripts/archive_old_reports.py"]
        on_disk = json.loads((state_dir / "heldout" / "results.json").read_text())
        assert on_disk["regressions"] == ["scripts/archive_old_reports.py"]

    def test_regression_still_failing_not_counted(self, tmp_path):
        repo = _make_repo(tmp_path, {"archive_old_reports.py": BAD_ARCHIVE})
        state_dir = tmp_path / "state"
        first = heldout.run_heldout(state_dir, repo, force=True)
        assert first["results"]["scripts/archive_old_reports.py"]["status"] == "fail"
        assert first["regressions"] == []
        # Still buggy, but content changed → rechecked, still fails. Was
        # already failing last run, so this is NOT a regression.
        (repo / "scripts" / "archive_old_reports.py").write_text(
            BAD_ARCHIVE + "\n# still broken\n", encoding="utf-8"
        )
        second = heldout.run_heldout(state_dir, repo, force=True)
        assert second["results"]["scripts/archive_old_reports.py"]["status"] == "fail"
        assert second["regressions"] == []

    def test_regression_new_only_failure_not_counted(self, tmp_path):
        """An artifact with no prior result (first time checked) that fails
        is not a regression — there was nothing to regress from."""
        repo = _make_repo(tmp_path, {"archive_old_reports.py": BAD_ARCHIVE})
        state_dir = tmp_path / "state"
        data = heldout.run_heldout(state_dir, repo, force=True)
        assert data["results"]["scripts/archive_old_reports.py"]["status"] == "fail"
        assert data["regressions"] == []

    def test_regression_pass_to_pass_empty(self, tmp_path):
        repo = _make_repo(tmp_path, {"archive_old_reports.py": GOOD_ARCHIVE})
        state_dir = tmp_path / "state"
        heldout.run_heldout(state_dir, repo, force=True)
        # Content changed but still a correct implementation → still pass.
        (repo / "scripts" / "archive_old_reports.py").write_text(
            GOOD_ARCHIVE + "\n# still fine\n", encoding="utf-8"
        )
        second = heldout.run_heldout(state_dir, repo, force=True)
        assert second["results"]["scripts/archive_old_reports.py"]["status"] == "pass"
        assert second["regressions"] == []


# ─── flaky detection (#842: non-deterministic verdict exclusion) ───────────


class TestFlakyDetection:
    def test_stable_pass_no_flaky_key(self):
        def _always_pass(ctx):
            return "pass", "ok"

        result = heldout._check_stable("scripts/x.py", "src", _always_pass, NOW)
        assert result["status"] == "pass"
        assert "flaky" not in result

    def test_flip_fail_then_pass_is_flaky(self):
        calls = {"n": 0}

        def _flip(ctx):
            calls["n"] += 1
            return ("fail", "broke") if calls["n"] == 1 else ("pass", "ok")

        result = heldout._check_stable("scripts/x.py", "src", _flip, NOW)
        assert result["status"] == "skip"
        assert result["flaky"] is True
        assert "flaky" in result["evidence"]
        assert calls["n"] == 2

    def test_first_skip_no_extra_runs(self):
        """A first-run skip is returned as-is — skip is already excluded
        from the gate, so no re-runs are spent confirming it."""
        calls = {"n": 0}

        def _skip_once(ctx):
            calls["n"] += 1
            return "skip", "timed out"

        result = heldout._check_stable("scripts/x.py", "src", _skip_once, NOW)
        assert result["status"] == "skip"
        assert "flaky" not in result
        assert calls["n"] == 1

    def test_end_to_end_flaky_artifact_recorded(self, tmp_path, monkeypatch):
        calls = {"n": 0}

        def _flip(ctx):
            calls["n"] += 1
            return ("fail", "broke") if calls["n"] == 1 else ("pass", "ok")

        monkeypatch.setitem(checkers.CHECKERS, "scripts/loop_health_report.py", _flip)
        repo = _make_repo(tmp_path, {"loop_health_report.py": SMOKE_OK})
        data = heldout.run_heldout(tmp_path / "state", repo, force=True)
        assert data["flaky"] == ["scripts/loop_health_report.py"]
        entry = data["results"]["scripts/loop_health_report.py"]
        assert entry["status"] == "skip"
        assert entry["flaky"] is True


# ─── sandbox ────────────────────────────────────────────────────────────────


class TestSandbox:
    def test_fixture_repo_untouched(self, tmp_path):
        """The checked scripts run on tmpdir COPIES: even a destructive
        script (BAD_ARCHIVE deletes; ESCAPER writes into its cwd) must leave
        the instance repo byte-identical."""
        repo = _make_repo(
            tmp_path,
            {"archive_old_reports.py": BAD_ARCHIVE, "eeebot_dashboard.py": ESCAPER},
        )
        before = _repo_snapshot(repo)
        heldout.run_heldout(tmp_path / "state", repo, force=True)
        assert _repo_snapshot(repo) == before
        assert not (repo / "pwned.txt").exists()
        assert not (tmp_path / "pwned.txt").exists()

    def test_sandbox_env_is_minimal(self, tmp_path, monkeypatch):
        """The subprocess env is a fixed minimal allowlist pinned to the
        tmpdir — no state_dir path, no ambient secrets passed through."""
        monkeypatch.setenv("HELDOUT_TEST_SECRET", "leakme")
        tmp = tmp_path / "sb"
        ctx = checkers.CheckContext(tmp_dir=tmp, script=tmp / "x.py")
        env = checkers._sandbox_env(ctx)
        assert set(env) == {
            "PATH", "PYTHONPATH", "HOME", "TMPDIR", "LANG", "PYTHONDONTWRITEBYTECODE",
        }
        assert "leakme" not in json.dumps(env)
        assert env["PYTHONPATH"] == str(tmp)
        assert env["HOME"] == str(tmp)

    def test_timeout_enforced_in_run_helper(self, tmp_path):
        tmp = tmp_path / "sb"
        tmp.mkdir()
        script = tmp / "sleep.py"
        script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
        ctx = checkers.CheckContext(tmp_dir=tmp, script=script, timeout=1.0)
        import pytest

        with pytest.raises(subprocess.TimeoutExpired):
            checkers._run(ctx)


# ─── scorecard integration ──────────────────────────────────────────────────


class TestScorecardHeldout:
    def test_section_and_gap_breach(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_results(
            state_dir,
            {
                "scripts/a.py": {"status": "pass", "evidence": "ok"},
                "scripts/b.py": {"status": "fail", "evidence": "dry-run mutated tree"},
                "scripts/c.py": {"status": "skip", "evidence": "timed out"},
            },
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        section = snap["heldout"]
        assert section == {
            "checked": 3,
            "passed": 1,
            "failed": 1,
            "skipped": 1,
            "heldout_gap": 0.5,  # skips excluded from the denominator
            "heldout_regressions": 0,
        }
        gap_metrics = [g["metric"] for g in snap["gaps"]]
        assert "heldout_gap" in gap_metrics
        gap = next(g for g in snap["gaps"] if g["metric"] == "heldout_gap")
        assert gap["vector"] == "V1"
        assert gap["target"] == 0.2

    def test_all_pass_no_gap(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_results(state_dir, {"scripts/a.py": {"status": "pass", "evidence": "ok"}})
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["heldout"]["heldout_gap"] == 0.0
        assert "heldout_gap" not in [g["metric"] for g in snap["gaps"]]

    def test_missing_results_zeros_and_none(self, tmp_path):
        snap = scorecard.compute_scorecard(tmp_path / "state", None, force=True)
        assert snap["heldout"] == {
            "checked": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "heldout_gap": None,
            "heldout_regressions": 0,
        }
        assert "heldout_gap" not in [g["metric"] for g in snap["gaps"]]

    def test_regressions_count_exposed(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_results(
            state_dir,
            {
                "scripts/a.py": {"status": "pass", "evidence": "ok"},
                "scripts/x.py": {"status": "fail", "evidence": "broken"},
            },
            regressions=["scripts/x.py"],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["heldout"]["heldout_regressions"] == 1

    def test_regressions_missing_key_defaults_zero(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_results(state_dir, {"scripts/a.py": {"status": "pass", "evidence": "ok"}})
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["heldout"]["heldout_regressions"] == 0

    def test_regressions_empty_list_zero(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_results(
            state_dir, {"scripts/a.py": {"status": "pass", "evidence": "ok"}}, regressions=[]
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["heldout"]["heldout_regressions"] == 0

    def test_corrupt_results_no_crash(self, tmp_path):
        state_dir = tmp_path / "state"
        path = state_dir / "heldout" / "results.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["heldout"]["checked"] == 0

    def test_recompute_path_invokes_runner_fail_open(self, tmp_path, monkeypatch):
        """compute_scorecard calls run_heldout on the recompute path; a
        heldout crash must never break the scorecard."""
        called = {"n": 0}

        def _boom(state_dir, repo, **kwargs):
            called["n"] += 1
            raise RuntimeError("heldout bug")

        monkeypatch.setattr(heldout, "run_heldout", _boom)
        snap = scorecard.compute_scorecard(tmp_path / "state", None, force=True)
        assert called["n"] == 1
        assert snap["schema_version"] == scorecard.SCORECARD_SCHEMA


# ─── defect demand ──────────────────────────────────────────────────────────


class TestHeldoutDemand:
    def test_failed_check_becomes_defect_with_evidence(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_results(
            state_dir,
            {
                "scripts/archive_old_reports.py": {
                    "status": "fail",
                    "evidence": "dry-run modified the reports tree",
                },
                "scripts/eeebot_dashboard.py": {"status": "pass", "evidence": "ok"},
                "scripts/loop_health_report.py": {"status": "skip", "evidence": "timed out"},
            },
        )
        items = demand._heldout_defect_items(state_dir)
        assert len(items) == 1
        item = items[0]
        assert item["kind"] == "defect"
        assert item["summary"] == "held-out check failed: scripts/archive_old_reports.py"
        assert item["evidence"] == "dry-run modified the reports tree"
        assert item["affected_path"] == "scripts/archive_old_reports.py"
        # And it flows through collect_demand.
        collected = demand.collect_demand(state_dir, None)
        assert any(i["summary"].startswith("held-out check failed:") for i in collected)

    def test_passed_and_skipped_yield_nothing(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_results(
            state_dir,
            {
                "scripts/a.py": {"status": "pass", "evidence": "ok"},
                "scripts/b.py": {"status": "skip", "evidence": "checker error"},
            },
        )
        assert demand._heldout_defect_items(state_dir) == []

    def test_bounded_and_deduped(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_results(
            state_dir,
            {
                f"scripts/f{i}.py": {"status": "fail", "evidence": f"broken {i}"}
                for i in range(10)
            },
        )
        items = demand._heldout_defect_items(state_dir)
        assert len(items) == demand._MAX_HELDOUT_DEFECTS
        assert len({i["id"] for i in items}) == len(items)

    def test_missing_results_no_items(self, tmp_path):
        assert demand._heldout_defect_items(tmp_path / "state") == []


# ─── invisibility (#603 invariant) ──────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every module that builds instance-facing prompts or ships content into the
# instance workspace. None of them may reference the held-out pack — the
# instance must not learn the checks exist, let alone their content.
_INSTANCE_FACING_SOURCES = (
    "nanobot/runtime/llm_proposer.py",
    "nanobot/runtime/cycle_planning.py",
    "nanobot/runtime/bridge.py",
    "nanobot/runtime/subagent_materializer.py",
    "nanobot/runtime/bounded_subagent_executor.py",
    "host/eeepc/etc/goal_text.json",
)


class TestInvisibility:
    def test_no_instance_facing_reference(self):
        for rel in _INSTANCE_FACING_SOURCES:
            path = _REPO_ROOT / rel
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8").lower()
            # #822: bridge.py legitimately calls heldout/microbench.py's
            # measure_cycle — a harness-run causal wall-time measurement,
            # not a hidden behavioral check. Its security does not depend on
            # secrecy: the harness re-measures BOTH baseline and candidate
            # itself, in isolated git-worktree subprocesses, regardless of
            # what the instance can read in bridge.py's own source — unlike
            # checkers.py/run_heldout's grading fixtures, which still must
            # stay invisible (an instance that saw the fixtures/assertions
            # could tune a solution to pass them without being correct).
            # Strip only the microbench references before the blanket check
            # so this invariant keeps guarding the actual secret surface.
            sanitized = (
                content.replace("heldout.microbench", "")
                .replace("heldout/microbench", "")
                .replace("heldout\\microbench", "")
            )
            assert "heldout" not in sanitized, (
                f"{rel} references the held-out pack — it must stay invisible "
                "to the instance (#603 invariant)"
            )

    def test_deploy_never_ships_heldout_into_workspace(self):
        """deploy_release.sh copies goal_text/libexec into host paths; it
        must never copy the heldout pack or fixtures anywhere."""
        script = _REPO_ROOT / "host" / "eeepc" / "scripts" / "deploy_release.sh"
        assert "heldout" not in script.read_text(encoding="utf-8").lower()

    def test_no_fixture_files_on_disk(self):
        """Fixtures are generated at runtime inside tmpdirs ONLY — the
        package ships nothing but Python source (no fixture trees an
        instance copy could ever expose)."""
        pkg = _REPO_ROOT / "nanobot" / "runtime" / "heldout"
        files = [p for p in pkg.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        assert files, "heldout package missing"
        assert all(p.suffix == ".py" for p in files), files

    def test_demand_evidence_reveals_what_not_how(self, tmp_path):
        """The defect item carries only the checker's evidence string — no
        fixture paths, no checker module reference."""
        state_dir = tmp_path / "state"
        _write_results(
            state_dir,
            {"scripts/x.py": {"status": "fail", "evidence": "crashed on empty state"}},
        )
        (item,) = demand._heldout_defect_items(state_dir)
        assert "checkers" not in json.dumps(item)
        assert "nanobot" not in json.dumps(item)
