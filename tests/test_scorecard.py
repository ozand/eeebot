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

from nanobot.runtime import runtime_deny, scorecard

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

    def test_decay_successes_split_from_integrations(self, tmp_path):
        """#800 churn split: a success whose proposed row served a decay
        demand is an archival (bookkeeping churn) — it counts as
        decay_integrations, never as integrations (the fitness numerator);
        integrations_total reports all work and feeds the cost denominator."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c-decay", "demand_id": "decay-608215ed8a44", "ts": _iso(40)},
                {"phase": "outcome", "cycle_id": "c-decay", "outcome": "success", "ts": _iso(39)},
                {"phase": "proposed", "cycle_id": "c-goal", "demand_id": "priority-b7942f7bf37b", "ts": _iso(20)},
                {"phase": "outcome", "cycle_id": "c-goal", "outcome": "success", "ts": _iso(19)},
            ],
        )
        _write_telemetry(
            state_dir,
            NOW.strftime("%Y-%m-%d"),
            [{"ts": _iso(5), "prompt_tokens": 800, "completion_tokens": 200}],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 1
        assert loop["decay_integrations"] == 1
        assert loop["integrations_total"] == 2
        # Cost per integration reflects ALL work: 1000 tokens / 2 total.
        assert snap["cost"]["tokens_per_integration"] == 500.0

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


class TestConfirmedIntegrationSplit:
    """#814: confirmed_integrations vs unconfirmed_integrations join
    success-outcome cycles against demand/completed.json's harness-confirmed
    set — the aggregate lever for "stop rewarding unused-artifact churn"
    (confirmation is post-hoc, so this cannot be a per-cycle gate).

    confirmed_integration_ratio is scoped to `confirmable_integrations`
    (non-decay successes whose files_changed touched a scripts/ path — the
    only kind confirm_serves can ever confirm), NOT integrations_total: a
    review pass on the first cut of this feature found that using
    integrations_total let decay archivals dilute the ratio and let
    permanently-unconfirmable runtime/docs/config integrations pin it below
    target forever (2 HIGH findings, fixed here)."""

    def _ledger_with_two_successes(self, state_dir: Path) -> None:
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c1", "demand_id": "priority-a", "ts": _iso(40)},
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
                 "files_changed": ["scripts/foo.py"], "ts": _iso(39)},
                {"phase": "proposed", "cycle_id": "c2", "demand_id": "priority-b", "ts": _iso(20)},
                {"phase": "outcome", "cycle_id": "c2", "outcome": "success",
                 "files_changed": ["scripts/bar.py"], "ts": _iso(19)},
            ],
        )

    def _write_completed(self, state_dir: Path, entries: dict) -> None:
        (state_dir / "demand").mkdir(parents=True, exist_ok=True)
        (state_dir / "demand" / "completed.json").write_text(
            json.dumps({"schema_version": "demand-completed-v1", "entries": entries}),
            encoding="utf-8",
        )

    def test_confirmed_vs_unconfirmed_split(self, tmp_path):
        state_dir = tmp_path / "state"
        self._ledger_with_two_successes(state_dir)
        self._write_completed(
            state_dir,
            {
                "a": {"cycle_id": "c1", "ts": _iso(38), "confirmed": True, "signal": "pycache"},
                "b": {"cycle_id": "c2", "ts": _iso(18)},
            },
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 2
        assert loop["confirmed_integrations"] == 1
        assert loop["unconfirmed_integrations"] == 1
        assert loop["confirmable_integrations"] == 2  # both touched scripts/
        assert loop["confirmed_integration_ratio"] == round(1 / 2, 4)

    def test_foreign_signal_does_not_count_as_confirmed_integration(self, tmp_path):
        """Tamper defense (#789 pattern): a `confirmed` entry whose signal
        is not harness-authored must not move confirmed_integration_ratio,
        mirroring test_foreign_signal_confirmed_entry_never_counts above."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c1", "demand_id": "priority-a", "ts": _iso(20)},
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
                 "files_changed": ["scripts/foo.py"], "ts": _iso(19)},
            ],
        )
        self._write_completed(
            state_dir,
            {"a": {"cycle_id": "c1", "ts": _iso(18), "confirmed": True, "signal": "operator-confirmed"}},
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 1
        assert loop["confirmed_integrations"] == 0
        assert loop["unconfirmed_integrations"] == 1
        assert loop["confirmable_integrations"] == 1
        assert loop["confirmed_integration_ratio"] == 0.0

    def test_decay_successes_never_join_confirmed_split(self, tmp_path):
        """Decay archivals are churn (#800), never counted toward either
        side of the confirmed/unconfirmed split, nor toward
        confirmable_integrations — even if their cycle_id happens to be
        marked confirmed and touched a scripts/ path."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c-decay", "demand_id": "decay-abc123", "ts": _iso(40)},
                {"phase": "outcome", "cycle_id": "c-decay", "outcome": "success",
                 "files_changed": ["scripts/foo.py"], "ts": _iso(39)},
            ],
        )
        self._write_completed(
            state_dir,
            {"decay-abc123": {"cycle_id": "c-decay", "ts": _iso(38), "confirmed": True, "signal": "pycache"}},
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 0
        assert loop["decay_integrations"] == 1
        assert loop["confirmed_integrations"] == 0
        assert loop["unconfirmed_integrations"] == 0
        assert loop["confirmable_integrations"] == 0
        assert loop["confirmed_integration_ratio"] is None  # 0-denominator, never fabricated

    def test_non_script_integration_excluded_from_denominator(self, tmp_path):
        """#814 review fix (HIGH #2): an integration that only touches
        nanobot/runtime (or docs/config) can NEVER be confirmed by
        confirm_serves (scripts/-only), so it must not sit in the
        denominator dragging the ratio down — it is simply excluded, not
        counted as a permanent miss."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c1", "demand_id": "priority-a", "ts": _iso(20)},
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
                 "files_changed": ["nanobot/runtime/scorecard.py"], "ts": _iso(19)},
            ],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 1
        assert loop["unconfirmed_integrations"] == 1  # reporting: never confirmed
        assert loop["confirmable_integrations"] == 0  # but excluded from the ratio entirely
        assert loop["confirmed_integration_ratio"] is None

    def test_decay_heavy_window_does_not_dilute_ratio(self, tmp_path):
        """#814 review fix (HIGH #1): 3 confirmed non-decay integrations
        alongside 5 decay archivals must read as a full 1.0 ratio, not
        3/8=0.375 — decay is out of BOTH numerator and denominator
        (the #801/#802 principle applied to this metric too)."""
        state_dir = tmp_path / "state"
        rows = []
        for i in range(3):
            rows += [
                {"phase": "proposed", "cycle_id": f"g{i}", "demand_id": f"priority-{i}", "ts": _iso(50 - i)},
                {"phase": "outcome", "cycle_id": f"g{i}", "outcome": "success",
                 "files_changed": [f"scripts/s{i}.py"], "ts": _iso(49 - i)},
            ]
        for i in range(5):
            rows += [
                {"phase": "proposed", "cycle_id": f"d{i}", "demand_id": f"decay-{i}", "ts": _iso(30 - i)},
                {"phase": "outcome", "cycle_id": f"d{i}", "outcome": "success",
                 "files_changed": [f"scripts/s{i}.py"], "ts": _iso(29 - i)},
            ]
        _write_ledger(state_dir, rows)
        self._write_completed(
            state_dir,
            {
                f"id{i}": {"cycle_id": f"g{i}", "ts": _iso(48), "confirmed": True, "signal": "pycache"}
                for i in range(3)
            },
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 3
        assert loop["decay_integrations"] == 5
        assert loop["confirmable_integrations"] == 3  # decay excluded, not 8
        assert loop["confirmed_integration_ratio"] == 1.0
        assert all(g["metric"] != "confirmed_integration_ratio" for g in snap["gaps"])

    def test_all_decay_window_no_spurious_zero_ratio(self, tmp_path):
        """0 non-decay integrations + 3 decay archivals must read as
        `confirmable_integrations=0` / ratio ``None`` — never a fabricated
        0/3=0.0 that would have fired a false gap despite there being no
        confirmable work in the window at all."""
        state_dir = tmp_path / "state"
        rows = []
        for i in range(3):
            rows += [
                {"phase": "proposed", "cycle_id": f"d{i}", "demand_id": f"decay-{i}", "ts": _iso(30 - i)},
                {"phase": "outcome", "cycle_id": f"d{i}", "outcome": "success",
                 "files_changed": [f"scripts/s{i}.py"], "ts": _iso(29 - i)},
            ]
        _write_ledger(state_dir, rows)
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 0
        assert loop["decay_integrations"] == 3
        assert loop["confirmable_integrations"] == 0
        assert loop["confirmed_integration_ratio"] is None
        assert all(g["metric"] != "confirmed_integration_ratio" for g in snap["gaps"])


class TestGoalLinkedConfirmableDenominator:
    """#836: confirmable_integrations additionally requires the cycle's
    `proposed` row to have carried a non-empty demand_id (goal-linked). An
    off-goal success (no demand_id on its proposed row, or no proposed row
    at all) is never folded into demand/completed.json, so confirm_serves
    can never reach it — it must not sit in the denominator forever,
    symmetric with the existing decay exclusion. #814 semantics are
    preserved: a goal-linked success that IS folded but not yet confirmed
    still counts as confirmable."""

    def _write_completed(self, state_dir: Path, entries: dict) -> None:
        (state_dir / "demand").mkdir(parents=True, exist_ok=True)
        (state_dir / "demand" / "completed.json").write_text(
            json.dumps({"schema_version": "demand-completed-v1", "entries": entries}),
            encoding="utf-8",
        )

    def test_goal_linked_success_still_counts_as_confirmable(self, tmp_path):
        """Unchanged behavior: a non-decay success touching scripts/ whose
        proposed row carried a demand_id is confirmable."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c1", "demand_id": "priority-a", "ts": _iso(20)},
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
                 "files_changed": ["scripts/foo.py"], "ts": _iso(19)},
            ],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 1
        assert loop["confirmable_integrations"] == 1

    def test_off_goal_success_excluded_from_denominator(self, tmp_path):
        """An otherwise-identical success whose proposed row has NO
        demand_id is never foldable into completed.json — it must not
        count as confirmable, and confirmed_integration_ratio must not be
        fabricated from a 0-sized confirmable universe."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c1", "ts": _iso(20)},
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
                 "files_changed": ["scripts/foo.py"], "ts": _iso(19)},
            ],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 1
        assert loop["unconfirmed_integrations"] == 1
        assert loop["confirmable_integrations"] == 0
        assert loop["confirmed_integration_ratio"] is None

    def test_off_goal_success_no_proposed_row_excluded(self, tmp_path):
        """Same exclusion when the cycle has no `proposed` row at all
        (demand_id is unknowable, so it cannot be treated as goal-linked)."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
                 "files_changed": ["scripts/foo.py"], "ts": _iso(19)},
            ],
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["integrations"] == 1
        assert loop["confirmable_integrations"] == 0
        assert loop["confirmed_integration_ratio"] is None

    def test_confirmed_goal_linked_success_counts_in_numerator(self, tmp_path):
        """A confirmed goal-linked success (completed.json confirmed=true,
        harness signal pycache) counts in the confirmed_integration_ratio
        numerator too — #814 semantics preserved by the #836 tightening."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": "c1", "demand_id": "priority-a", "ts": _iso(20)},
                {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
                 "files_changed": ["scripts/foo.py"], "ts": _iso(19)},
            ],
        )
        self._write_completed(
            state_dir,
            {"a": {"cycle_id": "c1", "ts": _iso(18), "confirmed": True, "signal": "pycache"}},
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        loop = snap["loop"]
        assert loop["confirmable_integrations"] == 1
        assert loop["confirmed_integration_ratio"] == 1.0


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

    def test_confirmed_ratio_gap_carries_lever_hint(self, tmp_path):
        """#808: confirmed_ratio's gap dict carries the scorecard's
        lever_hint so the proposer sees what actually moves the metric,
        instead of freely targeting an irrelevant reporting script."""
        state_dir = tmp_path / "state"
        (state_dir / "demand").mkdir(parents=True)
        (state_dir / "demand" / "completed.json").write_text(
            json.dumps(
                {
                    "schema_version": "demand-completed-v1",
                    "entries": {f"id{i}": {"cycle_id": f"c{i}", "ts": _iso(50)} for i in range(3)},
                }
            ),
            encoding="utf-8",
        )
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        gaps = {g["metric"]: g for g in snap["gaps"]}
        assert "confirmed_ratio" in gaps
        assert gaps["confirmed_ratio"]["lever_hint"] == scorecard._TARGETS["confirmed_ratio"]["lever_hint"]

    def test_gap_without_lever_hint_has_no_field(self, tmp_path):
        """A metric whose target has no lever_hint (e.g. repeat_failure_rate)
        must not crash and must not gain a fabricated lever_hint key."""
        state_dir = tmp_path / "state"
        _minimal_repeat_failure_ledger(state_dir)
        assert "lever_hint" not in scorecard._TARGETS["repeat_failure_rate"]
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        gaps = {g["metric"]: g for g in snap["gaps"]}
        assert "repeat_failure_rate" in gaps
        assert "lever_hint" not in gaps["repeat_failure_rate"]

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

    def test_confirmed_integration_ratio_needs_three_confirmable_integrations(self, tmp_path):
        """min_denominator gates on `confirmable_integrations` specifically
        (not all integrations) — 2 scripts/ successes is still too thin to
        judge, even though both are confirmation-eligible."""
        state_dir = tmp_path / "state"
        _write_ledger(
            state_dir,
            [
                {"phase": "proposed", "cycle_id": f"c{i}", "demand_id": f"priority-{i}", "ts": _iso(20 - i)}
                for i in range(2)
            ] + [
                {"phase": "outcome", "cycle_id": f"c{i}", "outcome": "success",
                 "files_changed": [f"scripts/s{i}.py"], "ts": _iso(19 - i)}
                for i in range(2)
            ],
        )
        # 0 confirmed of 2 confirmable — below min_denominator, no gap yet.
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["loop"]["confirmable_integrations"] == 2
        assert all(g["metric"] != "confirmed_integration_ratio" for g in snap["gaps"])

    def test_confirmed_window_scores_higher_than_unconfirmed_window(self, tmp_path):
        """#814 acceptance: an (otherwise identical) window whose
        integrations are later confirmed-used shows no
        confirmed_integration_ratio gap, while the same window with no
        confirmation shows one — churn alone must not read as fitness.
        Both cycles touch scripts/ so they are confirmation-eligible."""

        def _ledger(state_dir: Path) -> None:
            rows = [
                {"phase": "proposed", "cycle_id": f"c{i}", "demand_id": f"priority-{i}", "ts": _iso(40 - i)}
                for i in range(3)
            ] + [
                {"phase": "outcome", "cycle_id": f"c{i}", "outcome": "success",
                 "files_changed": [f"scripts/s{i}.py"], "ts": _iso(39 - i)}
                for i in range(3)
            ]
            _write_ledger(state_dir, rows)

        confirmed_dir = tmp_path / "confirmed"
        _ledger(confirmed_dir)
        (confirmed_dir / "demand").mkdir(parents=True)
        (confirmed_dir / "demand" / "completed.json").write_text(
            json.dumps(
                {
                    "schema_version": "demand-completed-v1",
                    "entries": {
                        f"id{i}": {"cycle_id": f"c{i}", "ts": _iso(38), "confirmed": True, "signal": "pycache"}
                        for i in range(3)
                    },
                }
            ),
            encoding="utf-8",
        )
        confirmed_snap = scorecard.compute_scorecard(confirmed_dir, None, force=True)

        unconfirmed_dir = tmp_path / "unconfirmed"
        _ledger(unconfirmed_dir)
        unconfirmed_snap = scorecard.compute_scorecard(unconfirmed_dir, None, force=True)

        assert confirmed_snap["loop"]["confirmed_integration_ratio"] == 1.0
        assert unconfirmed_snap["loop"]["confirmed_integration_ratio"] == 0.0

        confirmed_gaps = {g["metric"]: g for g in confirmed_snap["gaps"]}
        unconfirmed_gaps = {g["metric"]: g for g in unconfirmed_snap["gaps"]}
        assert "confirmed_integration_ratio" not in confirmed_gaps
        assert "confirmed_integration_ratio" in unconfirmed_gaps
        assert unconfirmed_gaps["confirmed_integration_ratio"]["vector"] == "V2"
        assert (
            unconfirmed_gaps["confirmed_integration_ratio"]["lever_hint"]
            == scorecard._TARGETS["confirmed_integration_ratio"]["lever_hint"]
        )

    def test_future_section_maps_to_nothing(self, tmp_path):
        """The goal's FUTURE section (deferred creative work) has no metric
        and can never generate a gap — regression pin on the targets table."""
        assert all(spec["vector"] in ("V1", "V2") for spec in scorecard._TARGETS.values())
        state_dir = tmp_path / "state"
        _minimal_repeat_failure_ledger(state_dir)
        for gap in scorecard.goal_gaps(state_dir, None):
            assert gap["vector"] in ("V1", "V2")


# ─── #819 CRITICAL-1: history.jsonl must be a protected fitness sidecar ─────


class TestHistoryIsFitnessProtected:
    def test_history_jsonl_is_in_fitness_sidecars(self):
        """benchmark_evidence.verify_benchmark (#819) treats
        scorecard/history.jsonl as its trust root. Without it in
        FITNESS_SIDECARS, the #789 spawn-boundary hash check (bridge.py,
        which iterates this exact tuple) would never notice an instance
        appending fabricated improving snapshots at runtime — a full bypass
        of the #819 non-forgeability guarantee. Regression pin."""
        assert "scorecard/history.jsonl" in scorecard.FITNESS_SIDECARS

    def test_latest_and_history_are_both_hashed_and_change_together(self, tmp_path):
        """Both sidecars are written by the SAME compute_scorecard call
        (latest.json overwritten, history.jsonl appended immediately after)
        — so a recompute changes both hashes together, confirming there is
        no timing gap where history.jsonl could be considered "already
        covered" by some other window than the one latest.json already
        gets checked in."""
        state_dir = tmp_path / "state"
        _minimal_repeat_failure_ledger(state_dir)
        before = scorecard.fitness_sidecar_hashes(state_dir)
        assert before["scorecard/latest.json"] == "absent"
        assert before["scorecard/history.jsonl"] == "absent"

        scorecard.compute_scorecard(state_dir, None, force=True)

        after = scorecard.fitness_sidecar_hashes(state_dir)
        assert after["scorecard/latest.json"] != "absent"
        assert after["scorecard/history.jsonl"] != "absent"


# ─── #865: control-plane visibility ─────────────────────────────────────────


class TestControlPlaneSnapshot:
    def test_section_present_with_all_allowlisted_keys(self, tmp_path, monkeypatch):
        for key in scorecard._CONTROL_PLANE_KEYS:
            monkeypatch.delenv(key, raising=False)
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        control_plane = snap["control_plane"]
        # #875/#876/#899: runtime_promotions + runtime_trust_ladder + models
        # are fixed extra keys (not env-driven), so they are asserted
        # separately rather than folded into the allowlist.
        assert set(control_plane.keys()) == set(scorecard._CONTROL_PLANE_KEYS) | {
            "runtime_promotions", "runtime_trust_ladder", "evolution_tree", "hypothesis_loop",
            "tech_tree", "models",
        }
        assert all(control_plane[key] is None for key in scorecard._CONTROL_PLANE_KEYS)
        assert control_plane["runtime_promotions"] == {"active": 0, "soaking": 0, "rejected": 0}
        assert control_plane["runtime_trust_ladder"] == {
            "level": 0,
            "unlocked": [],
            "ladder": list(runtime_deny.RUNTIME_TRUST_LADDER),
        }
        assert control_plane["evolution_tree"] == {
            "nodes": 0, "current_sha": None, "switches": 0,
        }
        assert control_plane["hypothesis_loop"] == {
            "active": 0, "answered": 0, "supported": 0, "refuted": 0, "inconclusive": 0,
        }
        # #879: a fresh recompute seeds all 5 tech-tree nodes and always
        # picks SOME current direction from among them (epsilon-greedy is
        # real/unseeded here, so which one is non-deterministic — assert
        # shape, not the exact pick).
        from nanobot.runtime import tech_tree as _tech_tree

        tt_snap = control_plane["tech_tree"]
        assert set(tt_snap["nodes"].keys()) == {spec["name"] for spec in _tech_tree.SEED_NODES}
        assert tt_snap["current"] in tt_snap["nodes"]
        assert tt_snap["switches"] == 0
        assert all(
            node == {"status": "active", "mean_gain": 0.0, "attempts": 0, "lever_metric": node["lever_metric"]}
            for node in tt_snap["nodes"].values()
        )

    def test_set_values_are_captured_others_stay_none(self, tmp_path, monkeypatch):
        for key in scorecard._CONTROL_PLANE_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SELFEVO_GOAL_REVIEW_ENABLED", "1")
        monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "openai/an/gemini-3-flash")
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        control_plane = snap["control_plane"]
        assert control_plane["SELFEVO_GOAL_REVIEW_ENABLED"] == "1"
        assert control_plane["SUBAGENT_BRIDGE_MODEL"] == "openai/an/gemini-3-flash"
        untouched = set(scorecard._CONTROL_PLANE_KEYS) - {
            "SELFEVO_GOAL_REVIEW_ENABLED",
            "SUBAGENT_BRIDGE_MODEL",
        }
        assert all(control_plane[key] is None for key in untouched)

    def test_preset_keys_are_captured_others_stay_none(self, tmp_path, monkeypatch):
        """#906: operator preset visibility — SELFEVO_PRESET, SELFEVO_CYCLE_PAUSE,
        and SELFEVO_MAX_TOOL_ITERATIONS are plain allowlisted passthrough keys,
        same shape as the pre-existing SUBAGENT_BRIDGE_MODEL/SELFEVO_PROPOSER_MODEL
        entries above."""
        for key in scorecard._CONTROL_PLANE_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SELFEVO_PRESET", "local_qwen")
        monkeypatch.setenv("SELFEVO_CYCLE_PAUSE", "3m")
        monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "80")
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        control_plane = snap["control_plane"]
        assert control_plane["SELFEVO_PRESET"] == "local_qwen"
        assert control_plane["SELFEVO_CYCLE_PAUSE"] == "3m"
        assert control_plane["SELFEVO_MAX_TOOL_ITERATIONS"] == "80"
        untouched = set(scorecard._CONTROL_PLANE_KEYS) - {
            "SELFEVO_PRESET",
            "SELFEVO_CYCLE_PAUSE",
            "SELFEVO_MAX_TOOL_ITERATIONS",
        }
        assert all(control_plane[key] is None for key in untouched)

    def test_no_key_outside_the_allowlist_ever_appears(self, tmp_path, monkeypatch):
        """Setting an unrelated SELFEVO_/env var must never leak into the
        section — only the explicit allowlist tuple is ever read."""
        monkeypatch.setenv("SELFEVO_TOTALLY_MADE_UP_FLAG", "1")
        monkeypatch.setenv("SOME_OTHER_ENV_VAR", "1")
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        control_plane = snap["control_plane"]
        assert set(control_plane.keys()) == set(scorecard._CONTROL_PLANE_KEYS) | {
            "runtime_promotions", "runtime_trust_ladder", "evolution_tree", "hypothesis_loop",
            "tech_tree", "models",
        }

    def test_no_secret_ever_appears_in_serialized_scorecard(self, tmp_path, monkeypatch):
        """Paranoia test (#865): a real secret-shaped env var, and a
        SELFEVO_-prefixed one that merely LOOKS like a secret, must never
        surface anywhere in the scorecard's serialized JSON — not just
        absent from control_plane, absent from the whole payload."""
        monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
        monkeypatch.setenv("SELFEVO_FAKE_SECRET_TOKEN", "x")
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        serialized = json.dumps(snap)
        assert "sk-test" not in serialized
        assert "SELFEVO_FAKE_SECRET_TOKEN" not in serialized
        assert "LITELLM_API_KEY" not in serialized

    def test_runtime_promotions_counts_active_soaking_rejected(self, tmp_path, monkeypatch):
        """#875: control_plane.runtime_promotions reflects PROMOTED_TREE's
        manifest (active) + verifier_state (soaking/rejected) — a cheap,
        read-only peek at the root-verification pipeline."""
        promoted_tree = tmp_path / "promoted"
        promoted_tree.mkdir()
        (promoted_tree / "manifest.json").write_text(json.dumps({
            "_schema_version": "promoted-manifest-v1",
            "nanobot/runtime/existence_index.py": {"status": "active"},
            "nanobot/runtime/probes.py": {"status": "active"},
        }))
        (promoted_tree / "verifier_state.json").write_text(json.dumps({
            "candidates": {
                "promotion-runtime-a": {"status": "soaking"},
                "promotion-runtime-b": {"status": "rejected"},
                "promotion-runtime-c": {"status": "rejected"},
                "promotion-runtime-d": {"status": "promoted"},
            }
        }))
        monkeypatch.setenv("PROMOTED_TREE", str(promoted_tree))
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["control_plane"]["runtime_promotions"] == {
            "active": 2,
            "soaking": 1,
            "rejected": 2,
        }

    def test_runtime_promotions_fail_open_when_tree_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROMOTED_TREE", str(tmp_path / "does-not-exist"))
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["control_plane"]["runtime_promotions"] == {
            "active": 0,
            "soaking": 0,
            "rejected": 0,
        }

    def test_runtime_trust_ladder_reflects_active_promotions(self, tmp_path, monkeypatch):
        """#876: control_plane.runtime_trust_ladder derives level/unlocked
        from the SAME PROMOTED_TREE manifest runtime_promotions reads —
        rung 0 + rung 1 genuinely active (consecutive) unlocks rung 2 too.
        A manifest entry must be genuinely valid (real file + matching
        sha256) to count, exactly like the loader itself requires."""
        import hashlib

        promoted_tree = tmp_path / "promoted"
        promoted_tree.mkdir()

        def _write(module_path: str, source: str) -> str:
            data = source.encode("utf-8")
            (promoted_tree / module_path.replace("/", "__")).write_bytes(data)
            return hashlib.sha256(data).hexdigest()

        sha_ei = _write("nanobot/runtime/existence_index.py", "X = 1\n")
        sha_demand = _write("nanobot/runtime/demand.py", "Y = 2\n")
        (promoted_tree / "manifest.json").write_text(json.dumps({
            "nanobot/runtime/existence_index.py": {"sha256": sha_ei, "status": "active"},
            "nanobot/runtime/demand.py": {"sha256": sha_demand, "status": "active"},
        }))
        monkeypatch.setenv("PROMOTED_TREE", str(promoted_tree))
        monkeypatch.setattr(
            "nanobot.runtime.promoted_overlay._boundary_ok", lambda *_: True,
        )
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        ladder = snap["control_plane"]["runtime_trust_ladder"]
        assert ladder["level"] == 2
        # earned_ladder_slice returns every rung whose PRECEDING rung is
        # active: rung1 (demand.py, unlocked by rung0) and rung2
        # (llm_proposer.py, unlocked by rung1) — rung 0 itself is never
        # included (it comes only from the env allow-list, not the ladder).
        assert ladder["unlocked"] == [
            "nanobot/runtime/demand.py",
            "nanobot/runtime/llm_proposer.py",
        ]
        assert ladder["ladder"] == list(runtime_deny.RUNTIME_TRUST_LADDER)

    def test_runtime_trust_ladder_fail_open_when_tree_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROMOTED_TREE", str(tmp_path / "does-not-exist"))
        state_dir = tmp_path / "state"
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["control_plane"]["runtime_trust_ladder"] == {
            "level": 0,
            "unlocked": [],
            "ladder": list(runtime_deny.RUNTIME_TRUST_LADDER),
        }

    def test_visibility_only_not_fed_into_targets_or_gaps(self):
        """Regression pin: control_plane keys must never appear as a target
        metric name — this section is snapshot-only, never fitness input."""
        assert not any(
            key in scorecard._TARGETS for key in scorecard._CONTROL_PLANE_KEYS
        )

    def test_hypothesis_loop_counts_from_lifecycle(self, tmp_path):
        """#878: control_plane.hypothesis_loop reflects
        hypotheses/lifecycle.json's status/verdict fields."""
        state_dir = tmp_path / "state"
        lifecycle_dir = state_dir / "hypotheses"
        lifecycle_dir.mkdir(parents=True)
        (lifecycle_dir / "lifecycle.json").write_text(json.dumps({
            "schema_version": "hypothesis-lifecycle-v1",
            "entries": {
                "hypothesis-h1": {"status": "active"},
                "hypothesis-h2": {"status": "answered", "verdict": "supported"},
                "hypothesis-h3": {"status": "answered", "verdict": "refuted"},
                "hypothesis-h4": {"status": "answered", "verdict": "inconclusive"},
            },
        }))
        snap = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snap["control_plane"]["hypothesis_loop"] == {
            "active": 1,
            "answered": 3,
            "supported": 1,
            "refuted": 1,
            "inconclusive": 1,
        }
