"""Tests for #878: the harness-computed hypothesis verdict.

Covers the two trusted measured sources (microbench, confirmed-usage), the
inconclusive fallback when neither has a signal, and the trust boundary
itself — a number the INSTANCE merely claims (in acceptance/evidence text)
must never influence the verdict; only a measured sidecar can.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import hypothesis_verdict


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    return state_dir


def _write_microbench(state_dir: Path, cycle_id: str, *, improvement_pct: float) -> None:
    d = state_dir / "heldout"
    d.mkdir(parents=True, exist_ok=True)
    (d / "microbench.json").write_text(
        json.dumps({
            "schema_version": "heldout-microbench-v1",
            "entries": {
                cycle_id: {
                    "module": "nanobot/runtime/existence_index.py",
                    "metric": "wall_ms_best_of_5",
                    "baseline_ms": 100.0,
                    "candidate_ms": 100.0 * (1 - improvement_pct / 100.0),
                    "improvement_pct": improvement_pct,
                    "direction": "lower",
                    "schema": "heldout-microbench-entry-v1",
                }
            },
        }),
        encoding="utf-8",
    )


def _write_completed(state_dir: Path, entry_id: str, entry: dict) -> None:
    d = state_dir / "demand"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "completed.json"
    data = {"schema_version": "demand-completed-v1", "entries": {}}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("entries", {})[entry_id] = entry
    path.write_text(json.dumps(data), encoding="utf-8")


class TestMicrobenchSource:
    def test_supported_above_threshold(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_microbench(state_dir, "cycle-1", improvement_pct=12.5)
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-1")
        assert verdict == "supported"
        assert evidence["source"] == "microbench"
        assert evidence["value"] == 12.5
        assert evidence["threshold"] == hypothesis_verdict.MICROBENCH_SUPPORTED_THRESHOLD_PCT

    def test_refuted_below_threshold(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_microbench(state_dir, "cycle-1", improvement_pct=1.0)
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-1")
        assert verdict == "refuted"
        assert evidence["source"] == "microbench"

    def test_refuted_on_negative_improvement(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_microbench(state_dir, "cycle-1", improvement_pct=-3.0)
        verdict, _ = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-1")
        assert verdict == "refuted"

    def test_microbench_takes_precedence_over_confirmed_usage(self, tmp_path):
        """Both sources present for the same cycle: microbench wins (first
        match), even though the confirmed-usage source alone would say
        something different."""
        state_dir = _state_dir(tmp_path)
        _write_microbench(state_dir, "cycle-1", improvement_pct=9.0)
        _write_completed(state_dir, "entry-1", {
            "cycle_id": "cycle-1",
            "files_changed": ["scripts/foo.py"],
            "confirmed": False,
            "ts": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        })
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-1")
        assert verdict == "supported"
        assert evidence["source"] == "microbench"


class TestConfirmedUsageSource:
    def test_supported_when_confirmed(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_completed(state_dir, "entry-1", {
            "cycle_id": "cycle-2",
            "files_changed": ["scripts/foo.py"],
            "confirmed": True,
            "signal": "pycache",
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-2")
        assert verdict == "supported"
        assert evidence["source"] == "confirmed_usage"
        assert evidence["artifact"] == "scripts/foo.py"

    def test_refuted_when_unused_past_window(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=hypothesis_verdict.CONFIRM_WINDOW_DAYS + 1))
        _write_completed(state_dir, "entry-1", {
            "cycle_id": "cycle-3",
            "files_changed": ["scripts/foo.py"],
            "confirmed": False,
            "ts": old_ts.isoformat().replace("+00:00", "Z"),
        })
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-3")
        assert verdict == "refuted"
        assert evidence["source"] == "confirmed_usage"

    def test_inconclusive_when_still_within_window(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        recent_ts = datetime.now(timezone.utc) - timedelta(days=1)
        _write_completed(state_dir, "entry-1", {
            "cycle_id": "cycle-4",
            "files_changed": ["scripts/foo.py"],
            "confirmed": False,
            "ts": recent_ts.isoformat().replace("+00:00", "Z"),
        })
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-4")
        assert verdict == "inconclusive"
        assert evidence["source"] == "none"

    def test_non_script_artifact_falls_through_to_inconclusive(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_completed(state_dir, "entry-1", {
            "cycle_id": "cycle-5",
            "files_changed": ["nanobot/runtime/demand.py"],
            "confirmed": False,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-5")
        assert verdict == "inconclusive"
        assert evidence["source"] == "none"


class TestNoSignal:
    def test_no_signal_at_all_is_inconclusive(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-none")
        assert verdict == "inconclusive"
        assert evidence == {"source": "none", "cycle_id": "cycle-none"}

    def test_empty_cycle_id_is_inconclusive(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "")
        assert verdict == "inconclusive"
        assert evidence["source"] == "none"

    def test_corrupt_microbench_file_is_fail_open(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        d = state_dir / "heldout"
        d.mkdir(parents=True)
        (d / "microbench.json").write_text("not json {{{", encoding="utf-8")
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-1")
        assert verdict == "inconclusive"
        assert evidence["source"] == "none"


class TestTrustBoundary:
    def test_forged_instance_claim_in_acceptance_text_is_ignored(self, tmp_path):
        """A hypothesis's own acceptance/evidence text claiming a huge win
        must NEVER move the verdict — only a measured sidecar can. With no
        microbench/completed entry at all, the verdict must be inconclusive
        no matter what the acceptance text says."""
        state_dir = _state_dir(tmp_path)
        forged_acceptance = "improvement_pct: 99.9 -- confirmed massive speedup, trust me"
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(
            state_dir, "cycle-forged", forged_acceptance
        )
        assert verdict == "inconclusive"
        assert evidence["source"] == "none"
        assert "99.9" not in json.dumps(evidence)

    def test_forged_completed_json_confirmed_field_still_requires_scripts_path(self, tmp_path):
        """A completed entry with confirmed=True but no scripts/ artifact at
        all must not be read as usage evidence for this cycle (a hypothesis
        that touched no script cannot be "used")."""
        state_dir = _state_dir(tmp_path)
        _write_completed(state_dir, "entry-1", {
            "cycle_id": "cycle-6",
            "files_changed": ["nanobot/runtime/demand.py"],
            "confirmed": True,
            "signal": "pycache",
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        verdict, evidence = hypothesis_verdict.classify_hypothesis_verdict(state_dir, "cycle-6")
        assert verdict == "inconclusive"
        assert evidence["source"] == "none"
