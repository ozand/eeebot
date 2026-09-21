"""ADR-031 rule 7 (#1852): the planning-session overhead ratio counter.

Same unconditional-marker-row-plus-rolling-census shape ``diary_fitness``
established -- see that module's own test file for the pattern this mirrors.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import planning_fitness


def test_record_cycle_overhead_computes_the_ratio(tmp_path: Path):
    row = planning_fitness.record_cycle_overhead(
        tmp_path / "state", cycle_id="c1", planning_ran=True,
        planning_iterations=20, execution_iterations=9,
    )
    assert row["planning_ran"] is True
    assert row["planning_iterations"] == 20
    assert row["execution_iterations"] == 9
    assert row["ratio"] == 20 / 29


def test_record_cycle_overhead_is_unconditional_marker_when_planning_did_not_run(tmp_path: Path):
    """A cycle where the planning session failed before producing ticks
    still leaves a row -- distinguishable from one where this was never
    called at all."""
    row = planning_fitness.record_cycle_overhead(
        tmp_path / "state", cycle_id="c1", planning_ran=False,
        planning_iterations=None, execution_iterations=9,
    )
    assert row["planning_ran"] is False
    assert row["planning_iterations"] is None
    assert row["ratio"] is None

    rows = planning_fitness._read_cycle_scans(tmp_path / "state")
    assert rows is not None and len(rows) == 1


def test_record_cycle_overhead_ratio_none_when_execution_count_unknown(tmp_path: Path):
    """A missing half of the pair must not fabricate a ratio -- never a
    denominator of just the known half."""
    row = planning_fitness.record_cycle_overhead(
        tmp_path / "state", cycle_id="c1", planning_ran=True,
        planning_iterations=20, execution_iterations=None,
    )
    assert row["ratio"] is None


def test_planning_overhead_rate_reports_no_data_when_sidecar_absent(tmp_path: Path):
    result = planning_fitness.planning_overhead_rate(tmp_path / "state")
    assert result["ok"] is False
    assert result["reason"] == "scans_unavailable"
    assert result["mean_ratio"] is None


def test_planning_overhead_rate_averages_over_the_window(tmp_path: Path):
    state = tmp_path / "state"
    planning_fitness.record_cycle_overhead(
        state, cycle_id="c1", planning_ran=True, planning_iterations=20, execution_iterations=20,
    )  # ratio 0.5
    planning_fitness.record_cycle_overhead(
        state, cycle_id="c2", planning_ran=True, planning_iterations=10, execution_iterations=90,
    )  # ratio 0.1
    result = planning_fitness.planning_overhead_rate(state, now=datetime.now(timezone.utc))
    assert result["ok"] is True
    assert result["cycles_in_window"] == 2
    assert result["planning_ran_in_window"] == 2
    assert abs(result["mean_ratio"] - 0.3) < 1e-9


def test_planning_overhead_rate_excludes_rows_outside_the_window(tmp_path: Path):
    state = tmp_path / "state"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    sidecar_dir = state / "planning_fitness"
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "cycle_scans.jsonl").write_text(
        '{"cycle_id": "old", "ts": "%s", "planning_ran": true, '
        '"planning_iterations": 20, "execution_iterations": 5, "ratio": 0.8}\n' % old_ts,
        encoding="utf-8",
    )
    planning_fitness.record_cycle_overhead(
        state, cycle_id="fresh", planning_ran=True, planning_iterations=20, execution_iterations=20,
    )
    result = planning_fitness.planning_overhead_rate(state)
    assert result["cycles_in_window"] == 1
    assert result["mean_ratio"] == 0.5


def test_write_planning_overhead_rate_persists_the_census(tmp_path: Path):
    state = tmp_path / "state"
    planning_fitness.record_cycle_overhead(
        state, cycle_id="c1", planning_ran=True, planning_iterations=20, execution_iterations=20,
    )
    result = planning_fitness.write_planning_overhead_rate(state)
    assert result["ok"] is True
    assert result["written"] is True
    payload_path = state / planning_fitness.CENSUS_REL
    assert payload_path.is_file()

    import json
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["schema"] == planning_fitness.CENSUS_SCHEMA
    assert payload["mean_ratio"] == 0.5


def test_record_cycle_overhead_fails_open_on_bad_state_dir(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    row = planning_fitness.record_cycle_overhead(
        blocker, cycle_id="c1", planning_ran=True, planning_iterations=20, execution_iterations=9,
    )
    assert row == {}
