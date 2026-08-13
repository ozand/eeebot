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

from nanobot.runtime import demand, usage_evidence


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


def _set_mtime(path: Path, days_ago: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
    os.utime(path, (ts, ts))


def _usage_sidecar(state_dir: Path) -> dict:
    return json.loads(
        (state_dir / "usage" / "last_used.json").read_text(encoding="utf-8")
    )


def _write_usage_sidecar(state_dir: Path, entries: dict, **top) -> None:
    (state_dir / "usage").mkdir(parents=True, exist_ok=True)
    data = {"schema_version": "usage-evidence-v1", "entries": entries}
    data.update(top)
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
        used_candidates (and confirm_serves copies into entries)."""
        assert usage_evidence.HARNESS_SIGNALS == frozenset({"pycache", "output"})

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
