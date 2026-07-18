"""Tests for #765: the deterministic instance scorecard + goal-gap analysis.

Covers: rotation-aware ledger counting (current cycles.jsonl + one .gz
archive), cost metrics from the #675 daily telemetry, zero-integration
None-safety, the 30-minute recompute watermark no-op, latest.json overwrite
+ history.jsonl append, fail-open on unreadable everything, the targets/
gap analysis (repeat_failure_rate, compile_clean_ratio, confirmed_ratio,
tokens_per_integration trend, and the idle_share/FUTURE non-targets), and
the report's tolerance for missing scorecard state.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import scorecard

NOW = datetime.now(timezone.utc)


def _iso(minutes_ago: int = 0, days_ago: int = 0) -> str:
    return (
        (NOW - timedelta(minutes=minutes_ago, days=days_ago))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_ledger(state_dir: Path, rows: list[dict], filename: str = "cycles.jsonl") -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / filename).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _write_gz_ledger(state_dir: Path, rows: list[dict], day: str) -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(ledger_dir / f"cycles-{day}.jsonl.gz", "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _write_telemetry(state_dir: Path, day: str, rows: list[dict]) -> None:
    calls_dir = state_dir / "llm_calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    (calls_dir / f"{day}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


# ─── compute: loop section (rotation-aware) ─────────────────────────────────


class TestLoopSection:
    def test_counts_across_rotation(self, tmp_path):
        """Integration/skip/idle counts must span the current ledger AND a
        rotated .gz archive — rotation blinds single-file readers (#773)."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(10)},
                {"phase": "outcome", "cycle_id": "c2", "outcome": "skipped-duplicate", "reason": "recent_duplicate_failure", "ts": _iso(20)},
                {"phase": "idle", "reason": "no_demand", "ts": _iso(30)},
                {"phase": "proposed", "cycle_id": "c1", "demand_id": "x", "ts": _iso(11)},
                {"phase": "proposer_reject", "reason": "self_dedup", "ts": _iso(12)},
            ],
        )
        yesterday = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
        _write_gz_ledger(
            state_dir,
            [
                {"phase": "outcome", "cycle_id": "c0", "outcome": "success", "ts": _iso(days_ago=1)},
                {"phase": "idle", "reason": "no_demand", "ts": _iso(days_ago=1, minutes_ago=5)},
                {"phase": "proposed", "cycle_id": "c0", "ts": _iso(days_ago=1, minutes_ago=10)},
                # Out-of-window row must be excluded.
                {"phase": "outcome", "cycle_id": "old", "outcome": "success", "ts": _iso(days_ago=30)},
            ],
            yesterday,
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 2  # one current + one archived
        assert loop["skips_by_class"] == {"skipped-duplicate": 1}
        assert loop["idle_rows"] == 2
        assert loop["cycleish_rows"] == 5  # 2 idle + 3 in-window outcomes
        assert loop["idle_share"] == round(2 / 5, 4)
        assert loop["proposals"] == 2
        # repeat failures = 1 recent_duplicate_failure skip + 1 self_dedup.
        assert loop["repeat_failures"] == 2
        assert loop["repeat_failure_rate"] == 1.0

    def test_bounded_archive_read(self, tmp_path):
        """Only the newest _MAX_GZ_FILES archives are read."""
        state_dir = tmp_path / "state"
        _write_ledger(state_dir, [])
        for i in range(10):
            day = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
            _write_gz_ledger(
                state_dir,
                [{"phase": "outcome", "cycle_id": f"g{i}", "outcome": "success", "ts": _iso(days_ago=1)}],
                f"{day}-{i:02d}",  # distinct names, all recent-day rows
            )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["loop"]["integrations"] == scorecard._MAX_GZ_FILES


# ─── compute: cost section (#675 telemetry) ─────────────────────────────────


class TestCostSection:
    def test_cost_metrics_from_daily_telemetry(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(10)}],
        )
        today = NOW.strftime("%Y-%m-%d")
        yesterday = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
        _write_telemetry(
            state_dir,
            today,
            [
                {"ts": _iso(5), "model": "m", "prompt_tokens": 100, "completion_tokens": 20, "component": "proposer"},
                {"ts": _iso(6), "model": "m", "prompt_tokens": 300, "completion_tokens": 80, "component": "bridge"},
            ],
        )
        _write_telemetry(
            state_dir,
            yesterday,
            [{"ts": _iso(days_ago=1), "model": "m", "prompt_tokens": 400, "completion_tokens": 100, "component": "bridge"}],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        cost = snap["cost"]
        assert cost["llm_calls"] == 3
        assert cost["total_tokens"] == 1000
        assert cost["calls_per_integration"] == 3.0
        assert cost["tokens_per_integration"] == 1000.0

    def test_zero_integrations_none_safe(self, tmp_path):
        """0 integrations → per-integration ratios are None, never a
        fabricated 0 (and never ZeroDivisionError)."""
        state_dir = tmp_path / "state"
        today = NOW.strftime("%Y-%m-%d")
        _write_telemetry(
            state_dir,
            today,
            [{"ts": _iso(5), "prompt_tokens": 10, "completion_tokens": 5}],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["loop"]["integrations"] == 0
        assert snap["cost"]["llm_calls"] == 1
        assert snap["cost"]["calls_per_integration"] is None
        assert snap["cost"]["tokens_per_integration"] is None


# ─── compute: quality + value sections ──────────────────────────────────────


class TestQualityAndValue:
    def test_quality_counts(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / "scripts" / "good.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "scripts" / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        (repo / "scripts" / "test_good.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "tests" / "test_more.py").write_text("x = 1\n", encoding="utf-8")
        snap = scorecard.compute_scorecard(state_dir, repo, force=True)
        quality = snap["quality"]
        assert quality["script_count"] == 3
        assert quality["compile_failing"] == 1
        assert quality["compile_clean"] == 2
        assert quality["compile_clean_ratio"] == round(2 / 3, 4)
        assert quality["test_file_count"] == 2

    def test_value_counts_from_761_sidecars(self, tmp_path):
        state_dir = tmp_path / "state"
        (state_dir / "demand").mkdir(parents=True)
        (state_dir / "demand" / "completed.json").write_text(
            json.dumps(
                {
                    "schema_version": "demand-completed-v1",
                    "entries": {
                        "a": {"cycle_id": "c1", "ts": _iso(100), "confirmed": True, "signal": "pycache"},
                        "b": {"cycle_id": "c2", "ts": _iso(90)},
                        "c": {"cycle_id": "c3", "ts": _iso(80)},
                    },
                }
            ),
            encoding="utf-8",
        )
        (state_dir / "usage").mkdir(parents=True)
        (state_dir / "usage" / "last_used.json").write_text(
            json.dumps(
                {
                    "schema_version": "usage-evidence-v1",
                    "entries": {
                        "scripts/a.py": {"last_used": _iso(5), "last_touched": None, "signal": "pycache"},
                        "scripts/old.py": {"last_used": _iso(days_ago=60), "last_touched": None, "signal": "pycache"},
                    },
                }
            ),
            encoding="utf-8",
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        value = snap["value"]
        assert value["completed_declared"] == 3
        assert value["completed_confirmed"] == 1
        assert value["confirmed_ratio"] == round(1 / 3, 4)
        assert value["usage_tracked"] == 2

    def test_foreign_signal_confirmed_entry_never_counts(self, tmp_path):
        """#789: a `confirmed` entry whose signal is not harness-authored
        (the 2026-07-17 live reward-hack wrote 'operator-confirmed') must
        not move confirmed_ratio — even before confirm_serves repairs it."""
        state_dir = tmp_path / "state"
        (state_dir / "demand").mkdir(parents=True)
        (state_dir / "demand" / "completed.json").write_text(
            json.dumps(
                {
                    "schema_version": "demand-completed-v1",
                    "entries": {
                        "a": {"cycle_id": "c1", "ts": _iso(100), "confirmed": True,
                              "signal": "operator-confirmed"},
                        "b": {"cycle_id": "c2", "ts": _iso(90), "confirmed": True},
                        "c": {"cycle_id": "c3", "ts": _iso(80), "confirmed": True,
                              "signal": "output"},
                    },
                }
            ),
            encoding="utf-8",
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        value = snap["value"]
        assert value["completed_declared"] == 3
        assert value["completed_confirmed"] == 1  # only the harness 'output' one
        assert value["confirmed_ratio"] == round(1 / 3, 4)


# ─── #789: integrity section ────────────────────────────────────────────────


class TestIntegritySection:
    def test_integrity_rows_counted_by_reason(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "integrity", "reason": "sidecar_tamper",
                 "entry_id": "defect-abc", "ts": _iso(60)},
                {"phase": "integrity", "reason": "sidecar_tamper",
                 "entry_id": "defect-def", "ts": _iso(50)},
                {"phase": "integrity", "reason": "sidecar_write_during_spawn",
                 "files": ["demand/completed.json"], "ts": _iso(40)},
                # out of 7d window — excluded
                {"phase": "integrity", "reason": "sidecar_tamper", "ts": _iso(days_ago=9)},
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(30)},
            ],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["integrity"]["incidents"] == 3
        assert snap["integrity"]["by_reason"] == {
            "sidecar_tamper": 2,
            "sidecar_write_during_spawn": 1,
        }

    def test_missing_data_reads_as_zero_incidents(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["integrity"] == {"incidents": 0, "by_reason": {}}


# ─── watermark + persistence ────────────────────────────────────────────────


class TestWatermarkAndPersistence:
    def test_recompute_watermark_noop_within_30_min(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(10)}],
        )
        # Explicit now= on every call: module-level NOW is captured at import
        # time, and on a slow CI runner more than a minute can pass before
        # this test body executes — a real-clock computed_at then defeats
        # the "31 minutes later" arithmetic below (fired on the 3.11 runner).
        first = scorecard.compute_scorecard(state_dir, None, now=NOW)
        assert first["loop"]["integrations"] == 1
        # New data lands, but the watermark holds — snapshot is returned as-is.
        _write_ledger(
            state_dir,
            [
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(10)},
                {"phase": "outcome", "cycle_id": "c2", "outcome": "success", "ts": _iso(5)},
            ],
        )
        second = scorecard.compute_scorecard(state_dir, None, now=NOW + timedelta(minutes=5))
        assert second["loop"]["integrations"] == 1
        assert second["computed_at_utc"] == first["computed_at_utc"]
        # 31 minutes later the recompute fires.
        later = NOW + timedelta(minutes=31)
        third = scorecard.compute_scorecard(state_dir, None, now=later)
        assert third["loop"]["integrations"] == 2

    def test_history_append_and_latest_overwrite(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_ledger(state_dir, [])
        scorecard.compute_scorecard(state_dir, None, force=True)
        scorecard.compute_scorecard(state_dir, None, force=True, now=NOW + timedelta(hours=1))
        history_lines = (
            (state_dir / "scorecard" / "history.jsonl").read_text(encoding="utf-8").splitlines()
        )
        assert len(history_lines) == 2
        latest = json.loads((state_dir / "scorecard" / "latest.json").read_text(encoding="utf-8"))
        assert latest["schema_version"] == "scorecard-v1"
        # latest.json holds exactly the newest snapshot (overwritten, not appended).
        assert latest == json.loads(history_lines[-1])

    def test_fail_open_on_unreadable_everything(self, tmp_path):
        """A file where the ledger dir should be, corrupt sidecars, a bogus
        repo path — all degrade to a zeros/None snapshot, never a raise."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "ledger").write_text("not a directory", encoding="utf-8")
        (state_dir / "demand").mkdir()
        (state_dir / "demand" / "completed.json").write_text("{corrupt", encoding="utf-8")
        snap = scorecard.compute_scorecard(state_dir, tmp_path / "no-such-repo", force=True)
        assert snap["schema_version"] == "scorecard-v1"
        assert snap["loop"]["integrations"] == 0
        assert snap["cost"]["tokens_per_integration"] is None
        assert snap["gaps"] == []
        assert scorecard.goal_gaps(state_dir, None) == []


# ─── gap analysis ───────────────────────────────────────────────────────────


def _minimal_repeat_failure_ledger(state_dir: Path) -> None:
    """2 proposals, 1 recent_duplicate_failure skip + 1 self_dedup reject
    → repeat_failure_rate = 1.0 > 0.3."""
    _write_ledger(
        state_dir,
        [
            {"phase": "proposed", "cycle_id": "c1", "ts": _iso(20)},
            {"phase": "proposed", "cycle_id": "c2", "ts": _iso(15)},
            {"phase": "outcome", "cycle_id": "c1", "outcome": "skipped-duplicate", "reason": "recent_duplicate_failure", "ts": _iso(19)},
            {"phase": "proposer_reject", "reason": "self_dedup", "ts": _iso(14)},
        ],
    )


class TestGoalGaps:
    def test_repeat_failure_rate_breach_gaps_with_vector_v1(self, tmp_path):
        state_dir = tmp_path / "state"
        _minimal_repeat_failure_ledger(state_dir)
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        gaps = {g["metric"]: g for g in snap["gaps"]}
        assert "repeat_failure_rate" in gaps
        gap = gaps["repeat_failure_rate"]
        assert gap["vector"] == "V1"
        assert gap["current"] == 1.0
        assert gap["target"] == 0.3
        assert "repeat_failure_rate" in gap["evidence"]

    def test_compile_clean_breach_gaps(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "good.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "scripts" / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        snap = scorecard.compute_scorecard(state_dir, repo, force=True)
        gaps = {g["metric"]: g for g in snap["gaps"]}
        assert "compile_clean_ratio" in gaps
        assert gaps["compile_clean_ratio"]["vector"] == "V1"
        assert gaps["compile_clean_ratio"]["current"] == 0.5

    def test_healthy_metrics_no_gaps(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "good.py").write_text("x = 1\n", encoding="utf-8")
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c1", "ts": _iso(20)},
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(19)},
            ],
        )
        snap = scorecard.compute_scorecard(state_dir, repo, force=True)
        assert snap["gaps"] == []

    def test_idle_share_never_gaps(self, tmp_path):
        """A 100% idle window is the honest no-op working — never a gap."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [{"phase": "idle", "reason": "no_demand", "ts": _iso(m)} for m in (10, 20, 30)],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["loop"]["idle_share"] == 1.0
        assert snap["gaps"] == []
        assert "idle_share" not in scorecard._TARGETS

    def test_no_history_no_trend_gap(self, tmp_path):
        """tokens_per_integration is trend-only: with no (or thin) history
        there is no trend to call, so no gap — even at a huge value."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(10)}],
        )
        _write_telemetry(
            state_dir,
            NOW.strftime("%Y-%m-%d"),
            [{"ts": _iso(5), "prompt_tokens": 900000, "completion_tokens": 100000}],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["cost"]["tokens_per_integration"] == 1000000.0
        assert all(g["metric"] != "tokens_per_integration" for g in snap["gaps"])

    def test_trend_gap_on_worsening_tokens_per_integration(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(10)}],
        )
        _write_telemetry(
            state_dir,
            NOW.strftime("%Y-%m-%d"),
            [{"ts": _iso(5), "prompt_tokens": 1500, "completion_tokens": 500}],
        )
        # Prior-window history: two entries, mean tokens_per_integration = 1000.
        history_dir = state_dir / "scorecard"
        history_dir.mkdir(parents=True)
        with open(history_dir / "history.jsonl", "w", encoding="utf-8") as fh:
            for days_ago, tpi in ((10, 900.0), (9, 1100.0)):
                fh.write(
                    json.dumps(
                        {
                            "schema_version": "scorecard-v1",
                            "computed_at_utc": _iso(days_ago=days_ago),
                            "cost": {"tokens_per_integration": tpi},
                        }
                    )
                    + "\n"
                )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        gaps = {g["metric"]: g for g in snap["gaps"]}
        assert "tokens_per_integration" in gaps
        gap = gaps["tokens_per_integration"]
        assert gap["vector"] == "V1"
        assert gap["current"] == 2000.0  # > 1.5 * mean(1000)
        assert gap["target"] == 1500.0

    def test_confirmed_ratio_needs_three_completed(self, tmp_path):
        state_dir = tmp_path / "state"
        (state_dir / "demand").mkdir(parents=True)

        def _completed(n: int) -> None:
            (state_dir / "demand" / "completed.json").write_text(
                json.dumps(
                    {
                        "schema_version": "demand-completed-v1",
                        "entries": {f"id{i}": {"cycle_id": f"c{i}", "ts": _iso(50)} for i in range(n)},
                    }
                ),
                encoding="utf-8",
            )

        _completed(2)  # 0 confirmed of 2 — below min_denominator, no gap
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert all(g["metric"] != "confirmed_ratio" for g in snap["gaps"])

        _completed(3)  # 0 confirmed of 3 — now judged, gaps (V2)
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        gaps = {g["metric"]: g for g in snap["gaps"]}
        assert "confirmed_ratio" in gaps
        assert gaps["confirmed_ratio"]["vector"] == "V2"

    def test_v1_gaps_ordered_before_v2(self, tmp_path):
        state_dir = tmp_path / "state"
        _minimal_repeat_failure_ledger(state_dir)
        (state_dir / "demand").mkdir(parents=True)
        (state_dir / "demand" / "completed.json").write_text(
            json.dumps(
                {
                    "schema_version": "demand-completed-v1",
                    "entries": {f"id{i}": {"cycle_id": f"c{i}", "ts": _iso(50)} for i in range(4)},
                }
            ),
            encoding="utf-8",
        )
        gaps = scorecard.goal_gaps(state_dir, None)
        vectors = [g["vector"] for g in gaps]
        assert "V1" in vectors and "V2" in vectors
        assert vectors == sorted(vectors)  # every V1 before every V2

    def test_future_section_maps_to_nothing(self, tmp_path):
        """The goal's FUTURE section (deferred creative work) has no metric
        and can never generate a gap — regression pin on the targets table."""
        assert all(spec["vector"] in ("V1", "V2") for spec in scorecard._TARGETS.values())
        state_dir = tmp_path / "state"
        _minimal_repeat_failure_ledger(state_dir)
        for gap in scorecard.goal_gaps(state_dir, None):
            assert gap["vector"] in ("V1", "V2")
