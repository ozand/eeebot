"""Tests for #761: post-integration value verification.

Covers the usage-evidence collector (pycache signal, bounded output-artifact
signal, no-signal skip, HEAD+time watermark no-op, sidecar max-merge,
fail-open on unreadable repo/state), the confirmed-serves tie-back
(harness-observed evidence ONLY — an explicit test pins that no text/claim
field can ever confirm an entry, the AIDE² anti-reward-hacking constraint),
and the decay demand kind (>14d unused+untouched, ordered last, max 5,
git-fallback, never outside scripts/).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import benchmark_evidence, demand, usage_evidence


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso(days_ago: float = 0) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(days=days_ago))


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "used_tool.py").write_text('"""A used tool."""\n', encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def _commit_all(repo: Path, message: str = "more") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def _repo_with_files(
    tmp_path: Path,
    files: dict,
    name: str = "refrepo",
    commit_iso: str | None = None,
) -> Path:
    """Fresh git repo seeded with ``files`` (repo-relative path -> content),
    committed once (#838 reference-signal tests need custom script/ops-file
    trees, not the single-script fixture ``_git_repo`` provides)."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    env = None
    if commit_iso is not None:
        env = dict(os.environ, GIT_COMMITTER_DATE=commit_iso, GIT_AUTHOR_DATE=commit_iso)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True, env=env)
    return repo


def _set_mtime(path: Path, days_ago: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
    os.utime(path, (ts, ts))


def _give_pycache(script: Path) -> None:
    """Create a ``__pycache__/<stem>.cpython-*.pyc`` next to ``script`` —
    real execution evidence (#854), matching how ``_pycache_signal`` detects
    that the interpreter actually imported/ran a script (as opposed to a
    committed-but-never-executed companion)."""
    cache = script.parent / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / f"{script.stem}.cpython-311.pyc").write_bytes(b"\x00")


def _usage_sidecar(state_dir: Path) -> dict:
    return json.loads(
        (state_dir / "usage" / "last_used.json").read_text(encoding="utf-8")
    )


def _write_usage_sidecar(state_dir: Path, entries: dict, **top) -> None:
    (state_dir / "usage").mkdir(parents=True, exist_ok=True)
    data = {"schema_version": "usage-evidence-v1", "entries": entries}
    data.update(top)
    # Existing fixtures model a successfully completed reader pass unless a
    # test explicitly exercises missing/unknown legacy metadata.
    data.setdefault("touched_results_status", "complete")
    # Ordinary decay fixtures represent a known empty/completed artifact
    # horizon; tests for missing input remove these directories explicitly.
    for name in ("results", "archive"):
        (state_dir / "subagents" / name).mkdir(parents=True, exist_ok=True)
    (state_dir / "usage" / "last_used.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def _write_completed(state_dir: Path, entries: dict) -> None:
    (state_dir / "demand").mkdir(parents=True, exist_ok=True)
    (state_dir / "demand" / "completed.json").write_text(
        json.dumps({"schema_version": "demand-completed-v1", "entries": entries}),
        encoding="utf-8",
    )


def _read_completed(state_dir: Path) -> dict:
    return json.loads(
        (state_dir / "demand" / "completed.json").read_text(encoding="utf-8")
    )


# ─── refresh_usage: signals ─────────────────────────────────────────────────


class TestRefreshUsageSignals:
    def test_pycache_mtime_is_a_used_signal(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        cache = repo / "scripts" / "__pycache__"
        cache.mkdir()
        pyc = cache / "used_tool.cpython-311.pyc"
        pyc.write_bytes(b"\x00")
        _set_mtime(pyc, 1)

        data = usage_evidence.refresh_usage(state_dir, repo)
        entry = data["entries"]["scripts/used_tool.py"]
        assert entry["last_used"] is not None
        assert entry["signal"] == "pycache"
        assert entry["last_used"] == _usage_sidecar(state_dir)["entries"][
            "scripts/used_tool.py"
        ]["last_used"]

    def test_output_artifact_signal_bounded_to_header(self, tmp_path):
        """A state/... output path named in the first 50 lines counts (the
        output file's mtime is the evidence); the same string past line 50
        does NOT — the extraction is deliberately bounded."""
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        (repo / "scripts" / "reporter.py").write_text(
            '"""Writes state/reports/summary.json every run."""\n',
            encoding="utf-8",
        )
        (repo / "scripts" / "late_mention.py").write_text(
            '"""No output named here."""\n'
            + "\n" * 60
            + "# writes state/reports/summary.json\n",
            encoding="utf-8",
        )
        _commit_all(repo)
        out = state_dir / "reports" / "summary.json"
        out.parent.mkdir(parents=True)
        out.write_text("{}", encoding="utf-8")

        data = usage_evidence.refresh_usage(state_dir, repo)
        entries = data["entries"]
        assert entries["scripts/reporter.py"]["signal"] == "output"
        assert entries["scripts/reporter.py"]["last_used"] is not None
        assert "scripts/late_mention.py" not in entries

    def test_output_signal_rejects_preexisting_churned_artifact(self, tmp_path):
        """#929 freshness gate: a runtime-churned state file that already
        existed BEFORE the script was committed must NOT grant output evidence
        to a script that merely names it in its header.

        Scenario: ``state/goals/goal_text.txt`` is rewritten every cycle.
        A new script ``scripts/goal_reader.py`` adds ``state/goals/goal_text.txt``
        to its header.  Because the file predates the script, the script has
        not actually produced any output --- the freshness gate must block it.
        """
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        # The state file already exists (written frequently by the runtime).
        churned = state_dir / "goals" / "goal_text.txt"
        churned.parent.mkdir(parents=True, exist_ok=True)
        churned.write_text("some runtime goal", encoding="utf-8")
        # Back-date the mtime to 30 days ago (pre-existing before the script).
        _set_mtime(churned, 30)

        # Commit the script AFTER the churned file already exists.
        goal_reader_text = (
            '"""Reads state/goals/goal_text.txt on every cycle.\n'
            "Writes state/goals/goal_text.txt (the goal file).\n\"\"\""
        )
        (repo / "scripts" / "goal_reader.py").write_text(
            goal_reader_text,
            encoding="utf-8",
        )
        _commit_all(repo)

        data = usage_evidence.refresh_usage(state_dir, repo)
        # The script named a churned file but produced no new output after
        # it was committed --- it must NOT appear as ``signal: output``.
        assert "scripts/goal_reader.py" not in data["entries"], (
            "freshness gate failed: churned pre-existing artifact granted output evidence"
        )

    def test_output_signal_rejects_newer_runtime_churned_file(self, tmp_path):
        """#929 causal binding gate: runtime-churned bookkeeping targets
        (ledger/cycles, scorecard/latest, validator_harness/last_runs, usage, etc.)
        must NOT qualify as output artifacts even when modified AFTER the script
        was committed, preventing mtime freshness from earning unearned output signal.
        """
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)

        # 1. Commit script first
        script_text = (
            '"""Maintains runtime bookkeeping.\n'
            "Writes state/ledger/cycles.jsonl and state/scorecard/latest.json.\n"
            "Also touches state/validator_harness/last_runs.jsonl and state/usage/last_used.json.\n\"\"\""
        )
        (repo / "scripts" / "churn_namer.py").write_text(script_text, encoding="utf-8")
        _commit_all(repo)

        # 2. Touch runtime churned files AFTER script creation (fresh mtime)
        for rel in [
            "ledger/cycles.jsonl",
            "scorecard/latest.json",
            "validator_harness/last_runs.jsonl",
            "usage/last_used.json",
        ]:
            target = state_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("churned content", encoding="utf-8")
            _set_mtime(target, 0)  # current / fresh timestamp

        # 3. refresh_usage must reject these churned files despite fresh mtimes
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/churn_namer.py" not in data["entries"], (
            "causal binding gate failed: newer runtime churned file granted output evidence"
        )

    def test_output_signal_rejects_absolute_and_traversal_paths(self, tmp_path):
        """#929: header paths cannot escape the trusted state/repo roots."""
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        script = repo / "scripts" / "path_escape.py"
        script.write_text(
            '"""Names state/../state/ledger/cycles.jsonl and '
            'state//var/log/syslog."""',
            encoding="utf-8",
        )
        _commit_all(repo)
        outside = Path("/var/log/syslog")
        if outside.exists():
            pytest.skip("host has the optional syslog fixture path")
        assert usage_evidence._output_signal(script, state_dir, repo) is None

    def test_output_signal_accepts_genuine_produced_output(self, tmp_path):
        """#929 positive test: a genuinely produced output artifact (e.g. docs or
        state data file not in the churned bookkeeping list) with mtime >= script creation
        qualifies for output evidence.
        """
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)

        script_text = (
            '"""Generates system map documentation.\n'
            "Outputs docs/system_map.md and state/metrics/report.json.\n\"\"\""
        )
        (repo / "scripts" / "map_generator.py").write_text(script_text, encoding="utf-8")
        _commit_all(repo)

        # Genuinely produced output files created after commit
        doc_out = repo / "docs" / "system_map.md"
        doc_out.parent.mkdir(parents=True, exist_ok=True)
        doc_out.write_text("# System Map\nGenerated content", encoding="utf-8")
        _set_mtime(doc_out, 0)

        state_out = state_dir / "metrics" / "report.json"
        state_out.parent.mkdir(parents=True, exist_ok=True)
        state_out.write_text('{"status": "ok"}', encoding="utf-8")
        _set_mtime(state_out, 0)

        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/map_generator.py" in data["entries"]
        entry = data["entries"]["scripts/map_generator.py"]
        assert entry["signal"] == "output"
        assert entry["last_used"] is not None

    def test_result_files_changed_counts_as_touched_not_used(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        results = state_dir / "subagents" / "results"
        results.mkdir(parents=True)
        (results / "r1.json").write_text(
            json.dumps({"status": "completed", "files_changed": ["scripts/used_tool.py"]}),
            encoding="utf-8",
        )

        data = usage_evidence.refresh_usage(state_dir, repo)
        entry = data["entries"]["scripts/used_tool.py"]
        assert entry["last_touched"] is not None
        assert entry["last_used"] is None  # modified is NOT used

    def test_no_signal_artifact_is_not_recorded(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/used_tool.py" not in data["entries"]


# ─── #1272: archive-aware touched evidence ───────────────────────────────────


def _write_result_artifact(state_dir: Path, directory: str, name: str, payload: dict, days_ago: float = 0) -> Path:
    path = state_dir / "subagents" / directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    _set_mtime(path, days_ago)
    return path


class TestArchiveAwareTouchedEvidence:
    def test_archived_result_contributes_touched_evidence(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        archived = _write_result_artifact(
            state_dir,
            "archive",
            "result-archived.json",
            {"files_changed": ["scripts/used_tool.py"]},
            days_ago=3,
        )

        touched, status = usage_evidence._touched_from_results_with_status(state_dir)

        assert touched == {"scripts/used_tool.py": usage_evidence._mtime_iso(archived)}
        assert status == "partial"

    def test_touched_reader_uses_newest_deterministic_bounded_union(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        for i in range(50):
            _write_result_artifact(
                state_dir,
                "archive" if i % 2 else "results",
                f"result-{i:02d}.json",
                {"files_changed": [f"scripts/tool_{i:02d}.py"]},
                days_ago=i,
            )
        oldest = _write_result_artifact(
            state_dir,
            "archive",
            "result-oldest.json",
            {"files_changed": ["scripts/oldest.py"]},
            days_ago=60,
        )

        touched, status = usage_evidence._touched_from_results_with_status(state_dir)

        assert status == "partial", "an unbounded touch refresh remains partial when its rank cap truncates evidence"
        assert len(touched) == 50
        assert "scripts/tool_00.py" in touched
        assert "scripts/tool_49.py" in touched
        assert "scripts/oldest.py" not in touched
        assert usage_evidence._mtime_iso(oldest) not in touched.values()

    def test_missing_result_dirs_are_explicit_but_preserve_empty_behavior(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        touched, status = usage_evidence._touched_from_results_with_status(state_dir)
        assert touched == {}
        assert status == "missing"

    def test_both_present_empty_result_dirs_are_valid_empty(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        for name in ("results", "archive"):
            (state_dir / "subagents" / name).mkdir(parents=True)
        touched, status = usage_evidence._touched_from_results_with_status(state_dir)
        assert touched == {}
        assert status == "valid-empty"

    def test_equal_mtime_boundary_uses_deterministic_order(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        # Exactly 51 artifacts share one mtime. The first 50 in descending
        # (mtime, directory, name) order are selected; the boundary item is not.
        for i in range(51):
            _write_result_artifact(
                state_dir,
                "archive" if i % 2 else "results",
                f"result-{i:02d}.json",
                {"files_changed": [f"scripts/tool_{i:02d}.py"]},
                days_ago=2,
            )
        paths = list((state_dir / "subagents" / "results").glob("*.json")) + list(
            (state_dir / "subagents" / "archive").glob("*.json")
        )
        mtime = paths[0].stat().st_mtime
        for path in paths:
            os.utime(path, (mtime, mtime))

        touched, status = usage_evidence._touched_from_results_with_status(state_dir)

        assert status == "partial", "an unbounded touch refresh remains partial when its rank cap truncates evidence"
        assert len(touched) == 50
        # `results` sorts ahead of `archive` in reverse tuple ordering; names
        # descend within each directory, so archive/result-01 is the boundary
        # item excluded while all results entries remain selected.
        assert "scripts/tool_00.py" in touched
        assert "scripts/tool_01.py" not in touched

    def test_one_missing_result_dir_is_partial(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        (state_dir / "subagents" / "results").mkdir(parents=True)
        _write_result_artifact(
            state_dir, "results", "result-live.json",
            {"files_changed": ["scripts/used_tool.py"]}, days_ago=1,
        )
        touched, status = usage_evidence._touched_from_results_with_status(state_dir)
        assert touched["scripts/used_tool.py"]
        assert status == "partial"

    def test_decay_horizon_reads_past_rank_cap_and_proceeds(self, tmp_path):
        """#1290: decay passes its 14-day horizon instead of trusting rank 50."""
        state_dir = _state_dir(tmp_path)
        (state_dir / "subagents" / "results").mkdir(parents=True)
        _write_result_artifact(
            state_dir, "results", "result-live.json",
            {"files_changed": ["scripts/used_tool.py"]}, days_ago=1,
        )
        for i in range(60):  # all older than the touch window, well past the cap
            _write_result_artifact(
                state_dir, "archive", f"result-{i:02d}.json",
                {"files_changed": [f"scripts/tool_{i:02d}.py"]}, days_ago=20 + i,
            )
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        touched, status = usage_evidence._touched_from_results_with_status(state_dir, since=cutoff)
        assert status == "complete"
        assert touched == {"scripts/used_tool.py": touched["scripts/used_tool.py"]}

        _write_usage_sidecar(state_dir, {}, touched_results_status=status)
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        candidates = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert candidates and "scripts/old_tool.py" in json.dumps(candidates)

    def test_recent_touch_beyond_rank_cap_blocks_decay_candidate(self, tmp_path):
        """A rank-50 prefix cannot answer a 14-day time-window question.

        All 51 artifacts are inside the decay horizon and the target's touch is
        only in the 51st-newest. The target must not become destructive decay
        demand merely because 50 newer unrelated results exist.
        """
        state_dir = _state_dir(tmp_path)
        for name in ("results", "archive"):
            (state_dir / "subagents" / name).mkdir(parents=True, exist_ok=True)
        for i in range(50):
            _write_result_artifact(
                state_dir, "archive", f"result-newer-{i:02d}.json",
                {"files_changed": [f"scripts/other_{i:02d}.py"]}, days_ago=1 + i / 100,
            )
        _write_result_artifact(
            state_dir, "archive", "result-target.json",
            {"files_changed": ["scripts/old_tool.py"]}, days_ago=2,
        )
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        _write_usage_sidecar(
            state_dir,
            {"scripts/old_tool.py": {"last_used": _now_iso(days_ago=30), "last_touched": _now_iso(days_ago=30), "signal": "pycache"}},
            touched_results_status="complete",
        )

        candidates = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)

        assert not any(item["path"] == "scripts/old_tool.py" for item in candidates)

    def test_cap_does_not_launder_a_missing_dir(self, tmp_path):
        """The cap is the only thing that stopped meaning ``partial``; a missing directory still does."""
        state_dir = _state_dir(tmp_path)
        for i in range(60):
            _write_result_artifact(
                state_dir, "archive", f"result-{i:02d}.json",
                {"files_changed": [f"scripts/tool_{i:02d}.py"]}, days_ago=20 + i,
            )
        touched, status = usage_evidence._touched_from_results_with_status(state_dir)
        assert status == "partial" and len(touched) == 50
        _write_usage_sidecar(state_dir, {}, touched_results_status=status)
        (state_dir / "subagents" / "results").rmdir()
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_cap_does_not_launder_a_corrupt_file_inside_the_window(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        (state_dir / "subagents" / "results").mkdir(parents=True)
        for i in range(60):
            _write_result_artifact(
                state_dir, "archive", f"result-{i:02d}.json",
                {"files_changed": [f"scripts/tool_{i:02d}.py"]}, days_ago=20 + i,
            )
        bad = state_dir / "subagents" / "results" / "result-bad.json"
        bad.write_text("{", encoding="utf-8")
        _set_mtime(bad, 1)
        _, status = usage_evidence._touched_from_results_with_status(state_dir)
        assert status == "corrupt"
        _write_usage_sidecar(state_dir, {}, touched_results_status=status)
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_refresh_usage_persists_touch_reader_status(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        _write_result_artifact(
            state_dir, "archive", "result-archived.json",
            {"files_changed": ["scripts/used_tool.py"]}, days_ago=3,
        )

        data = usage_evidence.refresh_usage(state_dir, repo)
        persisted = _usage_sidecar(state_dir)

        assert data["touched_results_status"] == "partial"
        assert persisted["touched_results_status"] == "partial"
        assert persisted["schema_version"] == "usage-evidence-v1"
        assert "scripts/used_tool.py" in persisted["entries"]

    def test_decay_refreshes_status_within_watermark(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        for name in ("results", "archive"):
            (state_dir / "subagents" / name).mkdir(parents=True, exist_ok=True)
        _write_usage_sidecar(
            state_dir,
            {"scripts/old_tool.py": {"last_used": _now_iso(days_ago=30), "last_touched": _now_iso(days_ago=20), "signal": "pycache"}},
            touched_results_status="complete",
            git_head="pinned", scanned_at_utc=_now_iso(),
        )
        bad = state_dir / "subagents" / "archive" / "result-new.json"
        bad.write_text("{", encoding="utf-8")
        _set_mtime(bad, 1)

        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_missing_result_evidence_blocks_decay(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        _write_usage_sidecar(state_dir, {}, touched_results_status="missing")
        import shutil
        shutil.rmtree(state_dir / "subagents")
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_unknown_sidecar_status_blocks_decay(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        _write_usage_sidecar(state_dir, {}, touched_results_status="unknown")
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_malformed_result_is_explicit_and_blocks_decay(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_result_artifact(
            state_dir, "archive", "result-good.json",
            {"files_changed": ["scripts/used_tool.py"]}, days_ago=3,
        )
        bad = state_dir / "subagents" / "archive" / "result-bad.json"
        bad.write_text("{", encoding="utf-8")
        _set_mtime(bad, 2)

        touched, status = usage_evidence._touched_from_results_with_status(state_dir)
        assert touched["scripts/used_tool.py"]
        assert status == "corrupt"

        _write_usage_sidecar(
            state_dir, {}, touched_results_status=status,
        )
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []


# ─── refresh_usage: watermark + merge ───────────────────────────────────────


class TestRefreshUsageWatermark:
    def test_noop_on_unchanged_head_within_window(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        usage_evidence.refresh_usage(state_dir, repo)
        # New evidence appears on disk after the first scan...
        cache = repo / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "used_tool.cpython-311.pyc").write_bytes(b"\x00")
        # ...but the watermark (same HEAD, <6h) makes the second call a no-op.
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/used_tool.py" not in data["entries"]

    def test_head_move_triggers_rescan(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        usage_evidence.refresh_usage(state_dir, repo)
        cache = repo / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "used_tool.cpython-311.pyc").write_bytes(b"\x00")
        (repo / "scripts" / "another.py").write_text("x = 1\n", encoding="utf-8")
        _commit_all(repo)

        data = usage_evidence.refresh_usage(state_dir, repo)
        assert data["entries"]["scripts/used_tool.py"]["signal"] == "pycache"

    def test_stale_scan_time_triggers_rescan(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        usage_evidence.refresh_usage(state_dir, repo)
        # Backdate the watermark >6h; same HEAD.
        data = _usage_sidecar(state_dir)
        data["scanned_at_utc"] = _iso(datetime.now(timezone.utc) - timedelta(hours=7))
        _write_usage_sidecar(state_dir, data["entries"], git_head=data["git_head"],
                             scanned_at_utc=data["scanned_at_utc"])
        cache = repo / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "used_tool.cpython-311.pyc").write_bytes(b"\x00")

        result = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/used_tool.py" in result["entries"]

    def test_merge_never_regresses_newer_to_older(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        future = _iso(datetime.now(timezone.utc) + timedelta(days=1))
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": future, "last_touched": None, "signal": "output"}},
        )
        cache = repo / "scripts" / "__pycache__"
        cache.mkdir()
        pyc = cache / "used_tool.cpython-311.pyc"
        pyc.write_bytes(b"\x00")
        _set_mtime(pyc, 2)  # older than the recorded last_used

        data = usage_evidence.refresh_usage(state_dir, repo)
        entry = data["entries"]["scripts/used_tool.py"]
        assert entry["last_used"] == future
        assert entry["signal"] == "output"

    def test_fail_open_on_missing_repo_and_unreadable_state(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        # Missing repo: sidecar contents returned unchanged, nothing raises.
        assert usage_evidence.refresh_usage(state_dir, tmp_path / "nope")["entries"] == {}
        assert usage_evidence.refresh_usage(state_dir, None)["entries"] == {}
        # Corrupt sidecar: degrades to empty entries, never raises.
        (state_dir / "usage").mkdir(parents=True)
        (state_dir / "usage" / "last_used.json").write_text("{not json", encoding="utf-8")
        repo = _git_repo(tmp_path)
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert isinstance(data["entries"], dict)


# ─── #838: reference signal (consumed via import or ops wiring) ────────────


class TestReferenceSignal:
    def test_import_edge_confirms_referenced_script(self, tmp_path):
        """Another committed script importing this one's module stem is a
        reference — the consumed script gains signal:"reference" — PROVIDED
        the importing script itself has execution evidence (#854; a
        never-executed companion import does not count, see
        test_forged_noop_companion_import_gets_no_reference_credit below)."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(
            tmp_path,
            {
                "scripts/a.py": "x = 1\n",
                "scripts/b.py": "import scripts.a\n",
            },
            commit_iso=_now_iso(days_ago=10),
        )
        _give_pycache(repo / "scripts" / "b.py")
        _set_mtime(repo / "scripts" / "__pycache__" / "b.cpython-311.pyc", 1)
        data = usage_evidence.refresh_usage(state_dir, repo)
        entry = data["entries"]["scripts/a.py"]
        assert entry["signal"] == "reference"
        assert entry["last_used"] is not None

    def test_ops_file_reference_confirms_script(self, tmp_path):
        """A committed *.service naming scripts/a.py is a reference too."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(tmp_path, {
            "scripts/a.py": "x = 1\n",
            "foo.service": "[Service]\nExecStart=/usr/bin/python3 scripts/a.py\n",
        })
        data = usage_evidence.refresh_usage(state_dir, repo)
        entry = data["entries"]["scripts/a.py"]
        assert entry["signal"] == "reference"
        assert entry["last_used"] is not None

    def test_self_import_and_test_only_import_excluded(self, tmp_path):
        """A script importing itself, a test file living in scripts/
        importing another script, and a tests/ file importing a script must
        never register a reference — self-reference and test-only
        consumption are not consumption (#838)."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(tmp_path, {
            "scripts/a.py": "import scripts.a\nx = 1\n",  # self-import
            "scripts/test_b.py": "import scripts.a\n",  # test file in scripts/
            "tests/test_a.py": "import scripts.a\n",  # test dir outside scripts/
        })
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/a.py" not in data["entries"]

    def test_kill_switch_disables_reference_signal(self, tmp_path, monkeypatch):
        """SELFEVO_USAGE_REFERENCE_ENABLED=0 makes refresh_usage behave
        exactly as before #838 — no reference signal is computed at all."""
        monkeypatch.setenv("SELFEVO_USAGE_REFERENCE_ENABLED", "0")
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(tmp_path, {
            "scripts/a.py": "x = 1\n",
            "scripts/b.py": "import scripts.a\n",
        })
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/a.py" not in data["entries"]

    def test_reference_confirms_completed_entry(self, tmp_path):
        """End-to-end: a completed.json entry for scripts/a.py with a ts
        BEFORE the importer's commit (mtime) becomes confirmed with
        signal:"reference" after refresh_usage + confirm_serves. The
        importer (scripts/b.py) has real execution evidence (#854)."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(
            tmp_path,
            {
                "scripts/a.py": "x = 1\n",
                "scripts/b.py": "import scripts.a\n",
            },
            commit_iso=_now_iso(days_ago=10),
        )
        _give_pycache(repo / "scripts" / "b.py")
        _set_mtime(repo / "scripts" / "__pycache__" / "b.cpython-311.pyc", 1)
        usage_evidence.refresh_usage(state_dir, repo)
        _write_completed(
            state_dir,
            {"priority-ref": {"cycle_id": "c1", "ts": _now_iso(days_ago=2),
                              "files_changed": ["scripts/a.py"]}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["priority-ref"]
        assert entry["confirmed"] is True
        assert entry["signal"] == "reference"

    def test_reference_is_a_harness_signal(self):
        assert "reference" in usage_evidence.HARNESS_SIGNALS

    def test_forged_reference_confirmation_without_sidecar_is_stripped(self, tmp_path):
        """#838 forgery guard: a completed.json entry forged with
        confirmed=true/signal=reference but with NO backing in the harness
        usage sidecar is stripped as tamper in Pass 1 (not trusted
        context-free), then re-derived honestly (stays unconfirmed)."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(tmp_path, {"scripts/a.py": "x = 1\n"})  # no importer
        usage_evidence.refresh_usage(state_dir, repo)  # a.py records no reference
        _write_completed(
            state_dir,
            {"forged": {"cycle_id": "c1", "ts": _now_iso(days_ago=2),
                        "files_changed": ["scripts/a.py"],
                        "confirmed": True, "signal": "reference",
                        "confirmed_at": _now_iso(days_ago=1)}},
        )
        result = usage_evidence.confirm_serves(state_dir, None)
        entry = _read_completed(state_dir)["entries"]["forged"]
        assert entry.get("confirmed") is not True   # stripped, not trusted
        assert entry.get("tamper_signal") == "reference"
        assert result == 0  # a strip is never counted as a confirmation

    def test_ops_substring_does_not_falsely_reference(self, tmp_path):
        """#838 review word-boundary: an ops file naming 'xa.py' must NOT
        register a reference for scripts/a.py (substring false positive)."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(tmp_path, {
            "scripts/a.py": "x = 1\n",
            "foo.service": "[Service]\nExecStart=/usr/bin/python3 xa.py\n",
        })
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/a.py" not in data["entries"]

    def test_forged_noop_companion_import_gets_no_reference_credit(self, tmp_path):
        """#854: a committed no-op companion that only does `import
        scripts.a` (never actually run — no pycache, no output evidence)
        must NOT manufacture a "reference" credit for scripts/a.py from
        static text alone."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(tmp_path, {
            "scripts/a.py": "x = 1\n",
            "scripts/b.py": "import scripts.a\n",  # committed, never executed
        })
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/a.py" not in data["entries"]


# ─── confirm_serves ─────────────────────────────────────────────────────────


class TestConfirmServes:
    def test_newer_usage_confirms_completed_entry(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-abc": {"cycle_id": "c1", "ts": _now_iso(days_ago=2),
                              "files_changed": ["scripts/used_tool.py"]}},
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["priority-abc"]
        assert entry["confirmed"] is True
        assert entry["signal"] == "pycache"
        assert entry["confirmed_at"]
        # Never-remove: original fields intact.
        assert entry["cycle_id"] == "c1"
        assert entry["files_changed"] == ["scripts/used_tool.py"]

    def test_older_usage_does_not_confirm(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-abc": {"cycle_id": "c1", "ts": _now_iso(days_ago=1),
                              "files_changed": ["scripts/used_tool.py"]}},
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=2),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        assert "confirmed" not in _read_completed(state_dir)["entries"]["priority-abc"]

    def test_claims_never_confirm_without_harness_evidence(self, tmp_path):
        """AIDE² pin: an entry whose files_changed artifact has NO harness
        evidence stays unconfirmed no matter what any summary/claim text
        says — text fields are never a confirmation source."""
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-abc": {
                "cycle_id": "c1", "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "summary": "SUCCESS — script deployed and used in production, huge win",
                "subagent_report": "verified working, confirmed by me",
                "confirmed_claim": True,
            }},
        )
        _write_usage_sidecar(state_dir, {})  # no evidence at all
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["priority-abc"]
        assert entry.get("confirmed") is not True
        # Never-remove: even unconsulted claim fields survive untouched.
        assert entry["summary"].startswith("SUCCESS")

    def test_touched_alone_never_confirms(self, tmp_path):
        """Modification is not consumption: last_touched newer than the
        completion ts does not confirm."""
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-abc": {"cycle_id": "c1", "ts": _now_iso(days_ago=2),
                              "files_changed": ["scripts/used_tool.py"]}},
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": None,
                                      "last_touched": _now_iso(days_ago=1),
                                      "signal": None}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0

    def test_fail_open_on_missing_sidecars(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert usage_evidence.confirm_serves(state_dir, None) == 0


# ─── #813/#819: benchmark-evidence gate ─────────────────────────────────────


_GOOD_BENCHMARK = {
    "metric": "p95_latency_ms",
    "baseline": 420,
    "new_value": 180,
    "method": "wrk -t2 -c50 -d30s against /health, median of 3 runs",
    "direction": "lower_is_better",
}

# #819: an artifact naming a metric that IS in the harness-verifiable
# allowlist (benchmark_evidence._HARNESS_METRICS) — used by every test that
# needs the affirmative (or self-heal) path to actually land.
_VERIFIABLE_BENCHMARK = {
    "metric": "tokens_per_integration",
    "baseline": 1000,
    "new_value": 400,
    "method": "scorecard cost section, before/after the integration cycle",
    "direction": "lower_is_better",
}


def _write_benchmark(state_dir: Path, cycle_id: str, payload: dict) -> None:
    path = benchmark_evidence.benchmark_path(state_dir, cycle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_history(state_dir: Path, snapshots: list[dict]) -> None:
    """Write ``snapshots`` (each a full scorecard-history-line dict) to
    ``<state_dir>/scorecard/history.jsonl`` — the harness trust root
    ``benchmark_evidence.verify_benchmark`` reads (#819)."""
    path = Path(state_dir) / "scorecard" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for snap in snapshots:
            fh.write(json.dumps(snap) + "\n")


def _corroborating_history(
    state_dir: Path,
    *,
    section: str = "cost",
    metric: str = "tokens_per_integration",
    before_value: float = 1000,
    after_value: float = 400,
    before_days_ago: float = 3,
    after_days_ago: float = 0,
) -> None:
    """The harness's own scorecard history showing ``metric`` moved from
    ``before_value`` to ``after_value`` — independent of whatever the
    benchmark artifact itself claims (the #819 non-forgeability point)."""
    _write_history(
        state_dir,
        [
            {"computed_at_utc": _now_iso(before_days_ago), section: {metric: before_value}},
            {"computed_at_utc": _now_iso(after_days_ago), section: {metric: after_value}},
        ],
    )


def _completed_optimization_entry(
    state_dir: Path,
    entry_id: str,
    cycle_id: str,
    *,
    confirmed: bool | None = None,
    signal: str | None = None,
    ts_days_ago: float = 2,
) -> None:
    entry: dict = {
        "cycle_id": cycle_id,
        "ts": _now_iso(days_ago=ts_days_ago),
        "files_changed": ["scripts/used_tool.py"],
        "serves": "optimization latency",
    }
    if confirmed is not None:
        entry["confirmed"] = confirmed
    if signal is not None:
        entry["signal"] = signal
    _write_completed(state_dir, {entry_id: entry})


class TestBenchmarkEvidenceGateTrustOff:
    """SELFEVO_BENCHMARK_TRUST unset/off (the default, required posture
    pending #819): the affirmative path stays fully dormant."""

    def test_valid_benchmark_still_does_not_confirm(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt3", "cycle-opt-3")
        _write_benchmark(state_dir, "cycle-opt-3", dict(_GOOD_BENCHMARK))
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-opt3"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_untrusted"

    def test_no_benchmark_at_all_is_benchmark_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt", "cycle-opt-1")
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-opt"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_missing"

    def test_non_optimization_entry_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-normal": {
                "cycle_id": "cycle-normal",
                "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "serves": "priority 5",
            }},
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["priority-normal"]
        assert entry["confirmed"] is True
        assert "unconfirmed_reason" not in entry

    def test_entry_without_serves_field_unaffected(self, tmp_path, monkeypatch):
        """Entries folded before #813 (no ``serves`` key at all) behave
        exactly as before — is_optimization_claim(None) is False."""
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-legacy": {
                "cycle_id": "cycle-legacy",
                "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
            }},
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["priority-legacy"]
        assert entry["confirmed"] is True

    def test_invalid_benchmark_is_benchmark_untrusted_not_missing(self, tmp_path, monkeypatch):
        """A schema-invalid artifact IS present on disk — the reason must
        distinguish this from no-file-at-all."""
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt2", "cycle-opt-2")
        _write_benchmark(state_dir, "cycle-opt-2", {"metric": "latency"})  # incomplete
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-opt2"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_untrusted"

    def test_gated_entry_write_is_idempotent(self, tmp_path, monkeypatch):
        """A second pass over an already-gated entry changes nothing further
        and does not re-increment any counter."""
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt4", "cycle-opt-4")
        _write_usage_sidecar(state_dir, {})
        usage_evidence.confirm_serves(state_dir, None)
        first = _read_completed(state_dir)
        usage_evidence.confirm_serves(state_dir, None)
        assert _read_completed(state_dir) == first


class TestBenchmarkEvidenceGateTrustOn:
    """SELFEVO_BENCHMARK_TRUST=1 (explicit operator opt-in): the affirmative
    path is live."""

    def test_valid_corroborated_benchmark_confirms_via_benchmark_signal(self, tmp_path, monkeypatch):
        """#819: a schema-valid artifact naming an allowlisted metric AND
        corroborated by the harness's own scorecard history confirms the
        entry directly in Pass 2 — signal is ``"benchmark"``, not whatever
        usage-evidence signal happens to also be present."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt3", "cycle-opt-3", ts_days_ago=2)
        _write_benchmark(state_dir, "cycle-opt-3", dict(_VERIFIABLE_BENCHMARK))
        _corroborating_history(state_dir, before_value=1000, after_value=400)
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["defect-opt3"]
        assert entry["confirmed"] is True
        assert entry["signal"] == "benchmark"
        assert entry["confirmed_at"]
        assert "unconfirmed_reason" not in entry

    def test_no_benchmark_never_confirms(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt", "cycle-opt-1")
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-opt"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_missing"

    def test_invalid_benchmark_never_confirms(self, tmp_path, monkeypatch):
        """#819: a schema-invalid artifact IS present (file_exists True) and
        trust IS on — the reason is now ``benchmark_unverified`` (we looked
        and could not verify it), distinct from both ``benchmark_missing``
        and ``benchmark_untrusted``."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt2", "cycle-opt-2")
        _write_benchmark(state_dir, "cycle-opt-2", {"metric": "latency"})  # incomplete
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-opt2"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_unverified"

    def test_regression_benchmark_never_confirms(self, tmp_path, monkeypatch):
        """A benchmark file exists and is well-typed but proves a
        regression (fails validate_benchmark's improvement check) — gated as
        unverified (file present, trust on, but not verifiable), never
        confirms."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt5", "cycle-opt-5")
        _write_benchmark(
            state_dir, "cycle-opt-5",
            dict(_VERIFIABLE_BENCHMARK, baseline=400, new_value=1000),  # got SLOWER
        )
        _corroborating_history(state_dir, before_value=1000, after_value=400)
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-opt5"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_unverified"

    def test_corroborated_metric_but_unregistered_metric_name_never_confirms(self, tmp_path, monkeypatch):
        """A schema-valid, well-typed artifact naming a metric NOT in the
        harness allowlist can never confirm, even with trust on and even if
        a same-named history section happens to exist — the metric-name gate
        comes before history corroboration."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-opt6", "cycle-opt-6")
        _write_benchmark(state_dir, "cycle-opt-6", dict(_GOOD_BENCHMARK))  # p95_latency_ms
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-opt6"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_unverified"

    def test_non_optimization_entry_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-normal": {
                "cycle_id": "cycle-normal",
                "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "serves": "priority 5",
            }},
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["priority-normal"]
        assert entry["confirmed"] is True

    # ─── HIGH-1: revocation of a pre-confirmed optimization entry ─────────

    def test_preconfirmed_entry_with_no_benchmark_is_revoked(self, tmp_path, monkeypatch):
        """The exact bypass shape: confirmed=True with a legitimate-looking
        harness signal AND an optimization claim, but no valid benchmark.
        Must be REVOKED to confirmed=False, not left alone."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(
            state_dir, "defect-forged", "cycle-forged",
            confirmed=True, signal="pycache",
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-forged"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_missing"

    def test_preconfirmed_entry_revoked_even_when_trust_off(self, tmp_path, monkeypatch):
        """The bypass must be closed regardless of the trust switch state —
        with it off, a claim with a valid-looking benchmark is STILL
        revoked (untrusted), not merely a fresh one blocked from
        confirming."""
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(
            state_dir, "defect-forged2", "cycle-forged2",
            confirmed=True, signal="pycache",
        )
        _write_benchmark(state_dir, "cycle-forged2", dict(_GOOD_BENCHMARK))
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-forged2"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_untrusted"

    def test_preconfirmed_entry_with_valid_trusted_corroborated_benchmark_stays_confirmed(
        self, tmp_path, monkeypatch
    ):
        """Not every pre-confirmed optimization entry is revoked — one that
        already verifies against harness history is left confirmed (#819:
        via the ``benchmark`` signal Pass 2 itself owns, correcting a stale
        ``pycache`` signal that never should have been the confirmer for an
        optimization claim in the first place)."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(
            state_dir, "defect-legit", "cycle-legit",
            confirmed=True, signal="pycache", ts_days_ago=2,
        )
        _write_benchmark(state_dir, "cycle-legit", dict(_VERIFIABLE_BENCHMARK))
        _corroborating_history(state_dir, before_value=1000, after_value=400)
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["defect-legit"]
        assert entry["confirmed"] is True
        assert entry["signal"] == "benchmark"
        assert "unconfirmed_reason" not in entry

    def test_second_pass_over_already_benchmark_confirmed_entry_is_idempotent(self, tmp_path, monkeypatch):
        """A steady-state verified entry (already confirmed=True,
        signal="benchmark") must not be rewritten/re-counted on a second
        pass — confirm_serves is watermark-cheap for the common case."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(
            state_dir, "defect-steady", "cycle-steady", ts_days_ago=2,
        )
        _write_benchmark(state_dir, "cycle-steady", dict(_VERIFIABLE_BENCHMARK))
        _corroborating_history(state_dir, before_value=1000, after_value=400)
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        first = _read_completed(state_dir)
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        assert _read_completed(state_dir) == first

    def test_foreign_signal_optimization_entry_is_repaired_then_gated(self, tmp_path, monkeypatch):
        """A confirmed entry with BOTH a foreign (non-harness) signal and an
        optimization claim with no benchmark: the #789 tamper repair must
        still fire (foreign signal stripped, tamper markers recorded) AND
        the benchmark gate must still land on confirmed=False afterward."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(
            state_dir, "defect-both", "cycle-both",
            confirmed=True, signal="operator-confirmed",
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-both"]
        assert entry["tamper_repaired_at"]
        assert entry["tamper_signal"] == "operator-confirmed"
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_missing"


# ─── #819: harness-corroborated confirm / revoke / self-heal ────────────────


class TestBenchmarkEvidenceHarnessCorroboration:
    """The #819 acceptance shapes: an optimization claim confirms ONLY when
    the harness's own scorecard history corroborates the named metric
    improved; a forged pre-written confirmation is revoked when it doesn't;
    and a previously-revoked entry SELF-HEALS once the history catches up —
    all via the SAME re-derivation in Pass 2, never a separate restore path.
    """

    def test_harness_corroborated_optimization_confirms(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(state_dir, "defect-real", "cycle-real", ts_days_ago=2)
        _write_benchmark(state_dir, "cycle-real", dict(_VERIFIABLE_BENCHMARK))
        _corroborating_history(state_dir, before_value=1000, after_value=400)
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["defect-real"]
        assert entry["confirmed"] is True
        assert entry["signal"] == "benchmark"

    def test_forged_preconfirmed_entry_with_flat_history_is_revoked(self, tmp_path, monkeypatch):
        """The exact forge shape #819 closes: confirmed=true and
        signal="benchmark" are pre-written directly into completed.json (as
        if by instance-authored code), AND a schema-valid benchmark artifact
        with fabricated numbers exists — but the harness's own scorecard
        history shows the metric never actually moved. Must be REVOKED to
        confirmed=False / unconfirmed_reason=benchmark_unverified, not
        trusted merely because the stored fields already look legitimate."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(
            state_dir, "defect-forged-claim", "cycle-forged-claim",
            confirmed=True, signal="benchmark", ts_days_ago=2,
        )
        _write_benchmark(
            state_dir, "cycle-forged-claim",
            dict(_VERIFIABLE_BENCHMARK, baseline=10_000, new_value=1),  # fabricated huge win
        )
        _corroborating_history(state_dir, before_value=1000, after_value=1000)  # actually flat
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-forged-claim"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_unverified"
        assert entry.get("signal") != "benchmark"

    def test_previously_revoked_entry_self_heals_once_history_corroborates(self, tmp_path, monkeypatch):
        """A previously-revoked entry (confirmed=False,
        unconfirmed_reason=benchmark_unverified from an earlier pass, before
        the harness had observed the after-snapshot) becomes confirmed=True
        the moment the harness's own history shows the improvement — no
        separate restore branch, just the same re-derivation Pass 2 always
        runs."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"defect-heal": {
                "cycle_id": "cycle-heal",
                "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "serves": "optimization latency",
                "confirmed": False,
                "unconfirmed_reason": "benchmark_unverified",
            }},
        )
        _write_benchmark(state_dir, "cycle-heal", dict(_VERIFIABLE_BENCHMARK))
        # First pass: history not yet corroborating (flat) — stays revoked.
        _corroborating_history(state_dir, before_value=1000, after_value=1000)
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-heal"]
        assert entry["confirmed"] is False
        assert entry["unconfirmed_reason"] == "benchmark_unverified"

        # Second pass: the harness has since observed the real improvement —
        # self-heals to confirmed=True, signal="benchmark".
        _corroborating_history(state_dir, before_value=1000, after_value=400)
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["defect-heal"]
        assert entry["confirmed"] is True
        assert entry["signal"] == "benchmark"
        assert "unconfirmed_reason" not in entry

    def test_non_optimization_entry_unaffected_by_harness_corroboration(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-normal2": {
                "cycle_id": "cycle-normal2",
                "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "serves": "priority 5",
            }},
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["priority-normal2"]
        assert entry["confirmed"] is True
        assert entry["signal"] == "pycache"


# ─── #789: tamper repair (trust only harness signals) ───────────────────────


def _ledger_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestTamperRepair:
    def test_harness_signals_is_exactly_what_the_module_writes(self):
        """The whitelist IS the set of signal values refresh_usage puts into
        used_candidates (and confirm_serves copies into entries), plus
        "benchmark" (#819) — the signal confirm_serves' Pass 2 writes on a
        harness-history-verified optimization claim — and "reference" (#838)
        — the signal refresh_usage writes when a script is consumed via
        import or ops wiring."""
        assert usage_evidence.HARNESS_SIGNALS == frozenset(
            {"pycache", "output", "benchmark", "reference", "harness_run"}
        )

    def test_foreign_signal_entry_repaired_with_one_integrity_row(self, tmp_path):
        """The live 2026-07-17 shape: instance code wrote confirmed=True with
        signal 'operator-confirmed'. Repair strips the falsified fields,
        records the tamper on the entry, and appends ONE integrity ledger
        row; a second pass is a no-op (no row spam)."""
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"defect-abc": {
                "cycle_id": "c1", "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "confirmed": True,
                "confirmed_at": _now_iso(days_ago=1),
                "signal": "operator-confirmed",
            }},
        )
        _write_usage_sidecar(state_dir, {})  # no honest evidence either

        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-abc"]
        assert "confirmed" not in entry
        assert "confirmed_at" not in entry
        assert "signal" not in entry
        assert entry["tamper_repaired_at"]
        assert entry["tamper_signal"] == "operator-confirmed"
        rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "integrity"]
        assert len(rows) == 1
        assert rows[0]["reason"] == "sidecar_tamper"
        assert rows[0]["entry_id"] == "defect-abc"
        assert rows[0]["foreign_signal"] == "operator-confirmed"

        # Idempotent: the repaired entry is no longer "confirmed", so a
        # second pass changes nothing and appends no second row.
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "integrity"]
        assert len(rows) == 1

    def test_missing_signal_on_confirmed_entry_is_tamper(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"defect-nosig": {"cycle_id": "c1", "ts": _now_iso(days_ago=2),
                              "files_changed": [], "confirmed": True}},
        )
        usage_evidence.confirm_serves(state_dir, None)
        entry = _read_completed(state_dir)["entries"]["defect-nosig"]
        assert "confirmed" not in entry
        assert entry["tamper_signal"] == ""

    def test_harness_signal_entry_untouched(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        confirmed_at = _now_iso(days_ago=1)
        _write_completed(
            state_dir,
            {"defect-ok": {
                "cycle_id": "c1", "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "confirmed": True, "confirmed_at": confirmed_at, "signal": "pycache",
            }},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["defect-ok"]
        assert entry["confirmed"] is True
        assert entry["confirmed_at"] == confirmed_at
        assert entry["signal"] == "pycache"
        assert "tamper_repaired_at" not in entry
        assert _ledger_rows(state_dir) == []

    # ─── #819 MED: "benchmark" is trusted ONLY on an optimization claim ────

    def test_benchmark_signal_on_non_optimization_entry_is_tampered(self, tmp_path, monkeypatch):
        """The MED bypass: `signal: "benchmark"` is context-free-trusted by
        HARNESS_SIGNALS, but Pass 2 (its sole legitimate writer) only ever
        writes it on an optimization-claim entry. A bare
        `{"confirmed": true, "signal": "benchmark"}` with no (or a
        non-optimization) `serves` could never have been written honestly —
        it must be stripped by Pass 1 exactly like any other foreign signal,
        not waved through as "harness-authored"."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"priority-forged-sig": {
                "cycle_id": "cycle-forged-sig",
                "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "serves": "priority 5",  # NOT an optimization claim
                "confirmed": True,
                "signal": "benchmark",
            }},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 0
        entry = _read_completed(state_dir)["entries"]["priority-forged-sig"]
        assert "confirmed" not in entry
        assert "signal" not in entry
        assert entry["tamper_repaired_at"]
        assert entry["tamper_signal"] == "benchmark"
        rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "integrity"]
        assert len(rows) == 1
        assert rows[0]["foreign_signal"] == "benchmark"

    def test_benchmark_signal_on_optimization_entry_is_not_tampered_by_pass1(self, tmp_path, monkeypatch):
        """The legitimate counterpart: an optimization-claim entry carrying
        `signal: "benchmark"` is NOT stripped by Pass 1 — it is left for
        Pass 2 to re-verify from harness history (which will confirm or
        revoke it based on actual corroboration, not merely on the presence
        of the signal)."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        state_dir = _state_dir(tmp_path)
        _completed_optimization_entry(
            state_dir, "defect-legit-sig", "cycle-legit-sig",
            confirmed=True, signal="benchmark", ts_days_ago=2,
        )
        _write_benchmark(state_dir, "cycle-legit-sig", dict(_VERIFIABLE_BENCHMARK))
        _corroborating_history(state_dir, before_value=1000, after_value=400)
        usage_evidence.confirm_serves(state_dir, None)
        entry = _read_completed(state_dir)["entries"]["defect-legit-sig"]
        assert "tamper_repaired_at" not in entry  # Pass 1 left it alone
        assert entry["confirmed"] is True  # Pass 2 re-verified and confirmed it
        assert entry["signal"] == "benchmark"

    def test_repaired_entry_reevaluates_honestly_same_pass(self, tmp_path):
        """A tampered entry whose artifact DOES have newer harness usage
        evidence is stripped, then re-confirmed honestly in the same pass —
        with the harness signal, keeping the tamper record."""
        state_dir = _state_dir(tmp_path)
        _write_completed(
            state_dir,
            {"defect-real": {
                "cycle_id": "c1", "ts": _now_iso(days_ago=2),
                "files_changed": ["scripts/used_tool.py"],
                "confirmed": True, "signal": "operator-confirmed",
            }},
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/used_tool.py": {"last_used": _now_iso(days_ago=1),
                                      "last_touched": None, "signal": "pycache"}},
        )
        assert usage_evidence.confirm_serves(state_dir, None) == 1
        entry = _read_completed(state_dir)["entries"]["defect-real"]
        assert entry["confirmed"] is True
        assert entry["signal"] == "pycache"  # honest, harness-authored
        assert entry["tamper_signal"] == "operator-confirmed"
        assert entry["tamper_repaired_at"]
        rows = [r for r in _ledger_rows(state_dir) if r.get("phase") == "integrity"]
        assert len(rows) == 1


# ─── decay demand kind ──────────────────────────────────────────────────────


def _seed_repo_scripts_at(tmp_path: Path, names: list[str], commit_iso: str) -> Path:
    """Repo whose scripts were all created (and last touched) by one commit
    dated ``commit_iso`` (via GIT_COMMITTER_DATE/GIT_AUTHOR_DATE)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "scripts").mkdir()
    for name in names:
        (repo / "scripts" / name).write_text("x = 1\n", encoding="utf-8")
    env = dict(os.environ, GIT_COMMITTER_DATE=commit_iso, GIT_AUTHOR_DATE=commit_iso)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True, env=env)
    return repo


def _seed_old_repo_scripts(tmp_path: Path, names: list[str]) -> Path:
    """Repo whose scripts have an OLD git commit date so the git fallback
    reads them as stale. Pinned BEFORE usage_evidence._EVIDENCE_EPOCH (not a
    days-ago offset from the real clock, which would drift past the #800
    eligibility epoch as time advances) — pre-epoch scripts stay
    decay-eligible without ever having harness evidence."""
    old = _iso(usage_evidence._EVIDENCE_EPOCH - timedelta(days=45))
    return _seed_repo_scripts_at(tmp_path, names, old)


class TestDecayDemand:
    def test_stale_artifact_becomes_decay_demand_ordered_last(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        _write_usage_sidecar(
            state_dir,
            {"scripts/old_tool.py": {"last_used": _now_iso(days_ago=30),
                                     "last_touched": _now_iso(days_ago=20),
                                     "signal": "pycache"}},
            git_head="pinned", scanned_at_utc=_now_iso(),
        )
        # A recent ledger defect proves decay sorts after other kinds.
        ledger = state_dir / "ledger"
        ledger.mkdir(parents=True)
        (ledger / "cycles.jsonl").write_text(
            json.dumps({"phase": "outcome", "cycle_id": "c1", "outcome": "failed",
                        "reason": "gate_failed", "ts": _now_iso()}) + "\n",
            encoding="utf-8",
        )
        items = demand.collect_demand(state_dir, repo)
        kinds = [i["kind"] for i in items]
        assert "decay" in kinds
        assert kinds.index("defect") < kinds.index("decay")
        decay = [i for i in items if i["kind"] == "decay"][0]
        assert decay["summary"].startswith("Propose archiving scripts/old_tool.py")
        assert "unused since" in decay["summary"]
        assert decay["affected_path"] == "scripts/old_tool.py"

    def test_recently_used_artifact_is_not_flagged(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["fresh_tool.py"])
        _write_usage_sidecar(
            state_dir,
            {"scripts/fresh_tool.py": {"last_used": _now_iso(days_ago=1),
                                       "last_touched": _now_iso(days_ago=30),
                                       "signal": "pycache"}},
            git_head="pinned", scanned_at_utc=_now_iso(),
        )
        items = demand.collect_demand(state_dir, repo)
        assert [i for i in items if i["kind"] == "decay"] == []

    def test_recently_touched_artifact_is_not_flagged(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["edited_tool.py"])
        _write_usage_sidecar(
            state_dir,
            {"scripts/edited_tool.py": {"last_used": _now_iso(days_ago=30),
                                        "last_touched": _now_iso(days_ago=1),
                                        "signal": "pycache"}},
            git_head="pinned", scanned_at_utc=_now_iso(),
        )
        items = demand.collect_demand(state_dir, repo)
        assert [i for i in items if i["kind"] == "decay"] == []

    def test_git_fallback_for_never_observed_artifact(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["forgotten.py"])
        _write_usage_sidecar(state_dir, {}, git_head="pinned", scanned_at_utc=_now_iso())
        items = demand.collect_demand(state_dir, repo)
        decay = [i for i in items if i["kind"] == "decay"]
        assert len(decay) == 1
        assert decay[0]["affected_path"] == "scripts/forgotten.py"

    def test_bounded_to_five_oldest(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        names = [f"tool_{i}.py" for i in range(8)]
        repo = _seed_old_repo_scripts(tmp_path, names)
        entries = {
            f"scripts/tool_{i}.py": {
                "last_used": _now_iso(days_ago=20 + i),
                "last_touched": None,
                "signal": "pycache",
            }
            for i in range(8)
        }
        _write_usage_sidecar(state_dir, entries, git_head="pinned", scanned_at_utc=_now_iso())
        items = demand.collect_demand(state_dir, repo)
        decay = [i for i in items if i["kind"] == "decay"]
        assert len(decay) == 5
        # Oldest first: tool_7 (27 days ago) leads.
        assert decay[0]["affected_path"] == "scripts/tool_7.py"

    def test_never_flags_files_outside_scripts(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["only_one.py"])
        (repo / "surfaces").mkdir()
        (repo / "surfaces" / "panel.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "note.md").write_text("old\n", encoding="utf-8")
        old = _iso(datetime.now(timezone.utc) - timedelta(days=60))
        env = dict(os.environ, GIT_COMMITTER_DATE=old, GIT_AUTHOR_DATE=old)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "extras"], cwd=repo, check=True, env=env)
        _write_usage_sidecar(state_dir, {}, git_head="pinned", scanned_at_utc=_now_iso())
        items = demand.collect_demand(state_dir, repo)
        decay_paths = {i["affected_path"] for i in items if i["kind"] == "decay"}
        assert decay_paths == {"scripts/only_one.py"}

    def test_no_evidence_and_no_git_history_skips(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = tmp_path / "notagit"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "mystery.py").write_text("x = 1\n", encoding="utf-8")
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert stale == []

    def test_fail_open_missing_repo(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert usage_evidence.stale_artifacts(state_dir, None, older_than_days=14) == []
        assert usage_evidence.stale_artifacts(state_dir, tmp_path / "gone", older_than_days=14) == []


# ─── #800: decay-eligibility guard (create→archive farming) ─────────────────


class TestDecayEligibilityGuard:
    def test_never_used_created_after_epoch_is_not_stale(self, tmp_path):
        """The farming vector: a script the instance created AFTER the
        usage-evidence epoch that was NEVER harness-observed as used must
        not surface as decay — mass-create → wait 14d → archive-for-credit."""
        state_dir = _state_dir(tmp_path)
        post_epoch = _iso(usage_evidence._EVIDENCE_EPOCH + timedelta(days=1))
        repo = _seed_repo_scripts_at(tmp_path, ["own_artifact.py"], post_epoch)
        _write_usage_sidecar(state_dir, {})  # no evidence at all
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_never_used_created_before_epoch_is_eligible(self, tmp_path):
        """Pre-epoch scripts can be legitimately stale without ever having
        evidence — the evidence system did not exist to observe them."""
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["legacy.py"])
        _write_usage_sidecar(state_dir, {})
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert [item["path"] for item in stale] == ["scripts/legacy.py"]

    def test_used_after_birth_grace_then_stale_is_eligible(self, tmp_path):
        """A post-epoch script with harness-observed usage OUTSIDE the
        birth-grace window decays normally — the guard gates never-used and
        birth-only-used artifacts, not genuinely consumed ones."""
        state_dir = _state_dir(tmp_path)
        epoch = usage_evidence._EVIDENCE_EPOCH
        post_epoch = _iso(epoch + timedelta(days=1))
        repo = _seed_repo_scripts_at(tmp_path, ["was_used.py"], post_epoch)
        # Used 10 days after creation — well past _BIRTH_USE_GRACE — and
        # both timestamps are anchored to the fixed epoch, so they stay
        # stale as the real clock advances.
        _write_usage_sidecar(
            state_dir,
            {"scripts/was_used.py": {"last_used": _iso(epoch + timedelta(days=11)),
                                     "last_touched": _iso(epoch + timedelta(days=12)),
                                     "signal": "pycache"}},
        )
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert [item["path"] for item in stale] == ["scripts/was_used.py"]

    def test_birth_only_use_created_after_epoch_is_not_stale(self, tmp_path):
        """The tightened #800 vector: the creation cycle's own self-test
        (subagent runs the script right after writing it → __pycache__ →
        last_used == creation date) is not consumption. Live dry-run showed
        17 farmed scripts passing an ever-used check on exactly that birth
        signal — they must not surface as decay."""
        state_dir = _state_dir(tmp_path)
        epoch = usage_evidence._EVIDENCE_EPOCH
        post_epoch = _iso(epoch + timedelta(days=1))
        repo = _seed_repo_scripts_at(tmp_path, ["birth_tested.py"], post_epoch)
        _write_usage_sidecar(
            state_dir,
            {"scripts/birth_tested.py": {
                "last_used": _iso(epoch + timedelta(days=1, hours=2)),
                "last_touched": _iso(epoch + timedelta(days=1, hours=2)),
                "signal": "pycache"}},
        )
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_archived_stub_is_never_reproposed(self, tmp_path):
        """Double-dip vector: an already-archived stub (DEPRECATED/ARCHIVED
        marker in the first 5 lines) must never re-surface as decay — the
        second archival of the same file farmed a second credit."""
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["stub_a.py", "stub_b.py"])
        (repo / "scripts" / "stub_a.py").write_text(
            '"""DEPRECATED: archived by decay lane."""\nraise SystemExit(1)\n',
            encoding="utf-8",
        )
        (repo / "scripts" / "stub_b.py").write_text(
            "# ARCHIVED 2026-07-30\nx = 1\n", encoding="utf-8",
        )
        _write_usage_sidecar(
            state_dir,
            {
                "scripts/stub_a.py": {"last_used": _now_iso(days_ago=30),
                                      "last_touched": None, "signal": "pycache"},
                "scripts/stub_b.py": {"last_used": _now_iso(days_ago=30),
                                      "last_touched": None, "signal": "pycache"},
            },
        )
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_protected_path_never_flagged(self, tmp_path, monkeypatch):
        """#809: an operator-protected script (env var, not repo-controlled)
        is never a decay candidate even when otherwise stale-eligible — the
        decay lane cannot see systemd/cron execution, so a live service like
        scripts/eeebot_dashboard.py must be excludable from proposal."""
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["eeebot_dashboard.py"])
        _write_usage_sidecar(
            state_dir,
            {"scripts/eeebot_dashboard.py": {"last_used": _now_iso(days_ago=30),
                                             "last_touched": _now_iso(days_ago=20),
                                             "signal": "pycache"}},
        )
        monkeypatch.setenv("SELFEVO_DECAY_PROTECT", "scripts/eeebot_dashboard.py")
        assert usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14) == []

    def test_protection_is_specific_not_global(self, tmp_path, monkeypatch):
        """Protecting one path must not shield an unrelated stale script —
        protection is per-path, not a blanket decay disable."""
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(
            tmp_path, ["eeebot_dashboard.py", "old_tool.py"]
        )
        _write_usage_sidecar(
            state_dir,
            {
                "scripts/eeebot_dashboard.py": {"last_used": _now_iso(days_ago=30),
                                                "last_touched": _now_iso(days_ago=20),
                                                "signal": "pycache"},
                "scripts/old_tool.py": {"last_used": _now_iso(days_ago=30),
                                        "last_touched": _now_iso(days_ago=20),
                                        "signal": "pycache"},
            },
        )
        monkeypatch.setenv("SELFEVO_DECAY_PROTECT", "scripts/eeebot_dashboard.py")
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert [item["path"] for item in stale] == ["scripts/old_tool.py"]

    def test_heldout_contracted_script_never_flagged(self, tmp_path, monkeypatch):
        """#884: a script under a held-out behavioral contract (a key of
        heldout.checkers.CHECKERS) must never be a decay candidate even when
        otherwise stale-eligible and with NO operator protect env set — a
        decay-disabled contracted script keeps held-out RED and makes #875
        auto-promotion inert. Protection is derived from the live registry."""
        from nanobot.runtime.heldout.checkers import CHECKERS

        contracted = sorted(CHECKERS)[0]  # e.g. scripts/eeebot_dashboard.py
        name = contracted.split("/", 1)[1]
        monkeypatch.delenv("SELFEVO_DECAY_PROTECT", raising=False)
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, [name])
        _write_usage_sidecar(
            state_dir,
            {contracted: {"last_used": _now_iso(days_ago=30),
                          "last_touched": _now_iso(days_ago=20),
                          "signal": "pycache"}},
        )
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert contracted not in [item["path"] for item in stale]

    def test_heldout_contracted_paths_helper_fail_open(self, tmp_path):
        """The registry lookup is fail-open: it returns a frozenset of the
        contracted paths, and never raises."""
        paths = usage_evidence._heldout_contracted_paths()
        assert isinstance(paths, frozenset)
        assert "scripts/eeebot_dashboard.py" in paths

    def test_unset_protect_env_behavior_unchanged(self, tmp_path, monkeypatch):
        """No env var set at all: identical to pre-#809 behavior — a stale
        script surfaces normally."""
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        _write_usage_sidecar(
            state_dir,
            {"scripts/old_tool.py": {"last_used": _now_iso(days_ago=30),
                                     "last_touched": _now_iso(days_ago=20),
                                     "signal": "pycache"}},
        )
        monkeypatch.delenv("SELFEVO_DECAY_PROTECT", raising=False)
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert [item["path"] for item in stale] == ["scripts/old_tool.py"]

    def test_empty_protect_env_behavior_unchanged(self, tmp_path, monkeypatch):
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["old_tool.py"])
        _write_usage_sidecar(
            state_dir,
            {"scripts/old_tool.py": {"last_used": _now_iso(days_ago=30),
                                     "last_touched": _now_iso(days_ago=20),
                                     "signal": "pycache"}},
        )
        monkeypatch.setenv("SELFEVO_DECAY_PROTECT", "")
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert [item["path"] for item in stale] == ["scripts/old_tool.py"]

    def test_malformed_protect_env_parsed_robustly(self, tmp_path, monkeypatch):
        """Trailing commas, stray whitespace, and a backslash-separated
        (Windows-authored) path must all parse without raising, and the
        backslash form still matches the POSIX repo-relative path."""
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(
            tmp_path, ["eeebot_dashboard.py", "old_tool.py"]
        )
        _write_usage_sidecar(
            state_dir,
            {
                "scripts/eeebot_dashboard.py": {"last_used": _now_iso(days_ago=30),
                                                "last_touched": _now_iso(days_ago=20),
                                                "signal": "pycache"},
                "scripts/old_tool.py": {"last_used": _now_iso(days_ago=30),
                                        "last_touched": _now_iso(days_ago=20),
                                        "signal": "pycache"},
            },
        )
        monkeypatch.setenv(
            "SELFEVO_DECAY_PROTECT",
            " , scripts\\eeebot_dashboard.py ,, ,  \t,",
        )
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert [item["path"] for item in stale] == ["scripts/old_tool.py"]

    def test_marker_past_line_five_does_not_shield(self, tmp_path):
        """The stub check is bounded to the FIRST 5 lines — a DEPRECATED
        mention deeper in a real script does not exempt it from decay."""
        state_dir = _state_dir(tmp_path)
        repo = _seed_old_repo_scripts(tmp_path, ["deep_mention.py"])
        (repo / "scripts" / "deep_mention.py").write_text(
            "x = 1\n" * 6 + "# TODO: mark DEPRECATED someday\n", encoding="utf-8",
        )
        _write_usage_sidecar(
            state_dir,
            {"scripts/deep_mention.py": {"last_used": _now_iso(days_ago=30),
                                         "last_touched": None, "signal": "pycache"}},
        )
        stale = usage_evidence.stale_artifacts(state_dir, repo, older_than_days=14)
        assert [item["path"] for item in stale] == ["scripts/deep_mention.py"]


# ─── #1034: harness_run and birth grace tests ───────────────────────────────


class TestHarnessRunAndBirthGrace:
    def test_harness_run_signal_from_parent_log(self, tmp_path):
        """#1034: Parent-written validator_harness_parent/runs.jsonl confers harness_run signal."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(tmp_path, {"scripts/val.py": "print('val')\n"})
        parent_log = state_dir / "validator_harness_parent" / "runs.jsonl"
        parent_log.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "validator": "scripts/val.py",
            "finished_at": _now_iso(days_ago=1),
            "exit_code": 0,
            "findings_count": 0,
            "harness_contract": "finding_jsonl_v1",
        }
        parent_log.write_text(json.dumps(row) + "\n", encoding="utf-8")
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/val.py" in data["entries"]
        assert data["entries"]["scripts/val.py"]["signal"] == "harness_run"

    def test_consumer_exec_confers_reference(self, tmp_path):
        """#1034: Executed consumer outside birth-grace window confers reference signal."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(
            tmp_path,
            {
                "scripts/target.py": "x = 1\n",
                "scripts/consumer.py": "import scripts.target\n",
            },
            commit_iso=_now_iso(days_ago=10),
        )
        _give_pycache(repo / "scripts" / "consumer.py")
        _set_mtime(repo / "scripts" / "__pycache__" / "consumer.cpython-311.pyc", 1)
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/target.py" in data["entries"]
        assert data["entries"]["scripts/target.py"]["signal"] == "reference"

    def test_consumer_exec_inside_birth_grace_suppresses_reference(self, tmp_path):
        """#1034 negative test: Consumer execution inside the 1-day birth window does NOT confer reference."""
        state_dir = _state_dir(tmp_path)
        epoch = _now_iso(days_ago=2)
        repo = _repo_with_files(
            tmp_path,
            {
                "scripts/target.py": "x = 1\n",
                "scripts/consumer.py": "import scripts.target\n",
            },
            commit_iso=epoch,
        )
        _give_pycache(repo / "scripts" / "consumer.py")
        # Evidence mtime is 2 hours after creation (inside the 1-day grace window)
        exec_time = datetime.fromisoformat(epoch.replace("Z", "+00:00")) + timedelta(hours=2)
        ts = exec_time.timestamp()
        pyc = repo / "scripts" / "__pycache__" / "consumer.cpython-311.pyc"
        os.utime(pyc, (ts, ts))
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "scripts/target.py" not in data["entries"]


# ─── #1035: owner_live_ratio and surfaces/ usage evidence ───────────────────


class TestOwnerLiveRatioAndSurfaces:
    def test_surfaces_tracked_in_refresh_usage(self, tmp_path):
        """#1035: surfaces/ files participate in refresh_usage and confirm_serves."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(
            tmp_path,
            {
                "surfaces/web_dashboard.py": "print('hello')\n",
            },
        )
        _give_pycache(repo / "surfaces" / "web_dashboard.py")
        data = usage_evidence.refresh_usage(state_dir, repo)
        assert "surfaces/web_dashboard.py" in data["entries"]
        assert data["entries"]["surfaces/web_dashboard.py"]["signal"] == "pycache"

    def test_owner_live_ratio_breakdown(self, tmp_path, monkeypatch):
        """#1035: owner_live_ratio union includes surfaces/, SELFEVO_DECAY_PROTECT,
        systemd/Makefile mentions, and post-birth harness signals."""
        state_dir = _state_dir(tmp_path)
        epoch = _now_iso(days_ago=10)
        repo = _repo_with_files(
            tmp_path,
            {
                "scripts/archived.py": "# DEPRECATED\nx = 1\n",
                "scripts/protected.py": "x = 1\n",
                "scripts/serviced.py": "x = 1\n",
                "surfaces/used.py": "x = 1\n",
                "scripts/unused.py": "x = 1\n",
                "eeebot.service": "ExecStart=/bin/python scripts/serviced.py\n",
            },
            commit_iso=epoch,
        )
        monkeypatch.setenv("SELFEVO_DECAY_PROTECT", "scripts/protected.py")

        # Give surfaces/used.py post-birth usage
        _give_pycache(repo / "surfaces" / "used.py")
        exec_time = datetime.fromisoformat(epoch.replace("Z", "+00:00")) + timedelta(days=2)
        ts = exec_time.timestamp()
        pyc = repo / "surfaces" / "__pycache__" / "used.cpython-311.pyc"
        os.utime(pyc, (ts, ts))

        # refresh usage sidecar so sidecar contains surfaces/used.py
        usage_evidence.refresh_usage(state_dir, repo)

        res = usage_evidence.owner_live_ratio(state_dir, repo)
        # 5 candidate files minus 1 archived stub = 4 inventory
        # Live items: scripts/protected.py (decay protect), scripts/serviced.py (service mention), surfaces/used.py (post-birth harness signal) -> 3
        assert res["inventory"] == 4
        assert res["live"] == 3
        assert res["ratio"] == 0.75

    def test_git_creation_memoization_zero_subprocess_on_second_refresh(self, tmp_path, monkeypatch):
        """#1040: second refresh on unchanged repo uses memoized git creation dates and makes 0 git subprocess calls."""
        state_dir = _state_dir(tmp_path)
        repo = _repo_with_files(
            tmp_path,
            {
                "scripts/consumer.py": "import s1\nx = 1\n",
                "scripts/s1.py": "print(1)\n",
            },
        )
        # Give consumer.py a pycache execution signal so _reference_index inspects its creation
        _give_pycache(repo / "scripts" / "consumer.py")

        # First refresh populates sidecar with created_cache
        res1 = usage_evidence.refresh_usage(state_dir, repo)
        assert len(res1["entries"]) >= 0
        sidecar = json.loads((state_dir / "usage" / "last_used.json").read_text(encoding="utf-8"))
        assert "created_cache" in sidecar
        assert "scripts/consumer.py" in sidecar["created_cache"]

        # Track subprocess.run calls
        orig_run = subprocess.run
        git_log_calls = 0

        def counting_run(*args, **kwargs):
            nonlocal git_log_calls
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, (list, tuple)) and len(cmd) > 1 and cmd[0] == "git" and "log" in cmd:
                git_log_calls += 1
            return orig_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", counting_run)
        # Advance time to force refresh past watermark
        monkeypatch.setattr(usage_evidence, "_RESCAN_HOURS", 0)
        res2 = usage_evidence.refresh_usage(state_dir, repo)
        assert len(res2["entries"]) >= 0
        assert git_log_calls == 0

    def test_git_creation_backward_compatibility_with_legacy_head_keys(self, tmp_path):
        """#1040: legacy head:rel key in created_cache is respected when rel key is missing."""
        repo = _repo_with_files(
            tmp_path,
            {
                "scripts/consumer.py": "import s1\nx = 1\n",
                "scripts/s1.py": "print(1)\n",
            },
        )
        head = usage_evidence._git_head(repo)
        sidecar_data = {
            "created_cache": {
                f"{head}:scripts/consumer.py": "2026-08-01T00:00:00Z",
            },
        }
        val = usage_evidence._git_creation_iso(repo, "scripts/consumer.py", sidecar_data=sidecar_data)
        assert val == "2026-08-01T00:00:00Z"

    def test_refresh_usage_replaces_unwritable_target_file_in_writable_dir(self, tmp_path, monkeypatch):
        """#1083: atomic write via tempfile + os.replace succeeds when existing file is unwritable."""
        repo = _repo_with_files(
            tmp_path,
            {
                "scripts/task.py": "print('ok')\n",
            },
        )
        state_dir = tmp_path / "state"
        usage_dir = state_dir / "usage"
        usage_dir.mkdir(parents=True, exist_ok=True)
        target = usage_dir / "last_used.json"

        stale_data = {
            "schema": usage_evidence.USAGE_SCHEMA,
            "scanned_at_utc": "2026-08-01T00:00:00Z",
            "git_head": "old-head",
            "entries": {},
        }
        target.write_text(json.dumps(stale_data), encoding="utf-8")

        # Simulate root-owned / unwritable target file by making direct write_text fail with PermissionError.
        orig_write_text = Path.write_text

        def unwritable_target_write_text(self, *args, **kwargs):
            if self.resolve() == target.resolve():
                raise PermissionError(13, "Permission denied (simulated root-owned file)")
            return orig_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", unwritable_target_write_text)
        monkeypatch.setattr(usage_evidence, "_RESCAN_HOURS", 0)
        res = usage_evidence.refresh_usage(state_dir, repo)

        assert res["scanned_at_utc"] != "2026-08-01T00:00:00Z"
        persisted = json.loads(target.read_text(encoding="utf-8"))
        assert persisted["scanned_at_utc"] == res["scanned_at_utc"]
        assert persisted["git_head"] == usage_evidence._git_head(repo)
