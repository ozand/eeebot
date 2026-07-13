"""Tests for scripts/loop_metrics_report.py (issue #710).

Builds a fixture cycle ledger (matching ``nanobot.runtime.cycle_ledger``'s
row shapes) with a known mix of cycles and asserts each computed metric,
the liveness states (healthy/degraded/dead), gzip-rotated-file window
inclusion, and that an empty ledger renders a clean 'dead/no data' report
instead of crashing.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _load_module():
    """Import scripts/loop_metrics_report.py as a module without running __main__."""
    script_path = Path(__file__).parent.parent / "scripts" / "loop_metrics_report.py"
    spec = importlib.util.spec_from_file_location("loop_metrics_report", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return _load_module()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _ts(now: datetime, minutes_ago: int) -> str:
    return (now - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _make_ledger(tmp_path: Path, now: datetime) -> Path:
    """A mixed fixture: 1 success (gated+passed), 1 gate-fail, 1 precheck-duplicate,
    1 no_commit-style failure with no gate row, and one more success in a rotated
    gzip file — used across most of the tests below.
    """
    state_dir = tmp_path
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True)

    rows = [
        {"phase": "started", "cycle_id": "c1", "ts": _ts(now, 50)},
        {"phase": "dedup", "cycle_id": "c1", "decision": "proceeded", "matched_against": None, "ts": _ts(now, 49)},
        {"phase": "gate", "cycle_id": "c1", "allowed": True, "reason": None, "ts": _ts(now, 48)},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "reason": None, "ts": _ts(now, 47)},

        {"phase": "started", "cycle_id": "c2", "ts": _ts(now, 40)},
        {"phase": "dedup", "cycle_id": "c2", "decision": "proceeded", "matched_against": None, "ts": _ts(now, 39)},
        {"phase": "gate", "cycle_id": "c2", "allowed": False, "reason": "gate_failed", "ts": _ts(now, 38)},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "failed", "reason": "gate_failed", "ts": _ts(now, 37)},

        {"phase": "started", "cycle_id": "c3", "ts": _ts(now, 30)},
        {"phase": "dedup", "cycle_id": "c3", "decision": "skipped_duplicate", "matched_against": "done:title-x", "ts": _ts(now, 29)},
        {"phase": "outcome", "cycle_id": "c3", "outcome": "skipped-duplicate", "reason": None, "ts": _ts(now, 29)},

        {"phase": "started", "cycle_id": "c4", "ts": _ts(now, 20)},
        {"phase": "dedup", "cycle_id": "c4", "decision": "proceeded", "matched_against": None, "ts": _ts(now, 19)},
        {"phase": "outcome", "cycle_id": "c4", "outcome": "failed", "reason": "no_commit", "ts": _ts(now, 18)},
    ]
    _write_jsonl(ledger_dir / "cycles.jsonl", rows)

    gz_rows = [
        {"phase": "started", "cycle_id": "c5", "ts": _ts(now, 10)},
        {"phase": "dedup", "cycle_id": "c5", "decision": "proceeded", "matched_against": None, "ts": _ts(now, 9)},
        {"phase": "gate", "cycle_id": "c5", "allowed": True, "reason": None, "ts": _ts(now, 8)},
        {"phase": "outcome", "cycle_id": "c5", "outcome": "success", "reason": None, "ts": _ts(now, 7)},
    ]
    gz_path = ledger_dir / f"cycles-{now.date().isoformat()}.jsonl.gz"
    with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
        for row in gz_rows:
            fh.write(json.dumps(row) + "\n")

    return state_dir


# ─── load / group ──────────────────────────────────────────────────────────


def test_load_ledger_rows_missing_dir_returns_empty(tmp_path, mod):
    rows = mod.load_ledger_rows(tmp_path / "does-not-exist", days=7)
    assert rows == []


def test_load_ledger_rows_includes_gzip_files_in_window(tmp_path, mod):
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)
    rows = mod.load_ledger_rows(state_dir, days=7)
    cycle_ids = {r.get("cycle_id") for r in rows}
    assert "c5" in cycle_ids  # from the gzip file
    assert "c1" in cycle_ids  # from the active file


def test_load_ledger_rows_excludes_out_of_window_gzip(tmp_path, mod):
    now = datetime.now(timezone.utc)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    old_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    gz_path = ledger_dir / "cycles-2026-01-01.jsonl.gz"
    with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"phase": "started", "cycle_id": "old1", "ts": old_ts}) + "\n")

    rows = mod.load_ledger_rows(tmp_path, days=7)
    assert rows == []


def test_load_ledger_rows_skips_malformed_lines(tmp_path, mod):
    now = datetime.now(timezone.utc)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    path = ledger_dir / "cycles.jsonl"
    good = json.dumps({"phase": "started", "cycle_id": "c1", "ts": _ts(now, 1)})
    path.write_text(good + "\nnot-json\n", encoding="utf-8")

    rows = mod.load_ledger_rows(tmp_path, days=7)
    assert len(rows) == 1


def test_group_by_cycle_keeps_last_of_singleton_phases(mod):
    rows = [
        {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "ts": "2026-01-01T00:00:00Z"},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-01-01T00:01:00Z"},
    ]
    cycles = mod.group_by_cycle(rows)
    assert cycles["c1"]["outcome"]["outcome"] == "success"


def test_group_by_cycle_accumulates_gate_rows(mod):
    rows = [
        {"phase": "gate", "cycle_id": "c1", "allowed": False, "reason": "r1"},
        {"phase": "gate", "cycle_id": "c1", "allowed": True, "reason": None},
    ]
    cycles = mod.group_by_cycle(rows)
    assert len(cycles["c1"]["gate"]) == 2


def test_group_by_cycle_ignores_rows_without_cycle_id(mod):
    rows = [{"phase": "started", "cycle_id": ""}, {"phase": "started"}]
    cycles = mod.group_by_cycle(rows)
    assert cycles == {}


# ─── metrics ───────────────────────────────────────────────────────────────


def test_full_report_metrics(tmp_path, mod):
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)
    report = mod.build_report(state_dir, days=7)

    assert report["window"]["n_cycles"] == 5
    assert report["outcome_counts"]["success"] == 2
    assert report["outcome_counts"]["failed"] == 2
    assert report["outcome_counts"]["skipped-duplicate"] == 1

    m = report["metrics"]
    assert m["duplicate_rate"]["value"] == pytest.approx(1 / 5)
    assert m["duplicate_rate"]["numerator"] == 1
    assert m["duplicate_rate"]["denominator"] == 5

    assert m["genuinely_new_proposal_rate"]["value"] == pytest.approx(4 / 5)

    # spawned = 4 (all but c3, the precheck-duplicate skip); gated = 2 (c1, c5 passed; c2 gate ran)
    assert m["productive_spawn_rate"]["denominator"] == 4
    assert m["productive_spawn_rate"]["numerator"] == 3  # c1, c2, c5 all reached a gate row
    assert m["gate_pass_rate"]["denominator"] == 3
    assert m["gate_pass_rate"]["numerator"] == 2  # c1, c5 passed; c2 failed
    assert m["integration_rate"]["of_spawned"]["numerator"] == 2
    assert m["integration_rate"]["of_spawned"]["denominator"] == 4
    assert m["integration_rate"]["of_gated"]["numerator"] == 2
    assert m["integration_rate"]["of_gated"]["denominator"] == 3


def test_metrics_with_unavailable_inputs_are_null_with_note(tmp_path, mod):
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)
    report = mod.build_report(state_dir, days=7)
    m = report["metrics"]
    for name in (
        "protected_surface_rejections",
        "cost_per_integrated_change",
        "harvestable_upstream_ratio",
        "human_intervention_needed",
    ):
        assert m[name]["value"] is None
        assert "note" in m[name] and m[name]["note"]


def test_gate_fail_breakdown_attributes_gate_and_outcome_stages(tmp_path, mod):
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)
    report = mod.build_report(state_dir, days=7)
    breakdown = {(r["stage"], r["reason"]): r["count"] for r in report["gate_fail_breakdown"]}
    assert breakdown[("gate", "gate_failed")] == 1
    assert breakdown[("outcome", "no_commit")] == 1


def test_dedup_breakdown_counts_decisions_and_top_matched_against(tmp_path, mod):
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)
    report = mod.build_report(state_dir, days=7)
    dedup = report["dedup_breakdown"]
    assert dedup["by_decision"]["proceeded"] == 4
    assert dedup["by_decision"]["skipped_duplicate"] == 1
    assert dedup["top_matched_against"][0] == {"matched_against": "done:title-x", "count": 1}


# ─── liveness ──────────────────────────────────────────────────────────────


def test_liveness_healthy_when_success_and_low_duplicate_rate(tmp_path, mod):
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)
    report = mod.build_report(state_dir, days=7)
    assert report["liveness"]["state"] == "healthy"
    assert report["liveness"]["last_productive_ts"] is not None


def test_liveness_degraded_when_activity_but_no_success(tmp_path, mod):
    now = datetime.now(timezone.utc)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = [
        {"phase": "started", "cycle_id": "c1", "ts": _ts(now, 10)},
        {"phase": "dedup", "cycle_id": "c1", "decision": "proceeded", "ts": _ts(now, 9)},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "reason": "no_commit", "ts": _ts(now, 8)},
    ]
    _write_jsonl(ledger_dir / "cycles.jsonl", rows)

    report = mod.build_report(tmp_path, days=7)
    assert report["liveness"]["state"] == "degraded"
    assert report["liveness"]["last_productive_ts"] is None


def test_liveness_degraded_when_duplicate_rate_saturated(tmp_path, mod):
    now = datetime.now(timezone.utc)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = []
    # 4 duplicates, 1 success -> duplicate_rate = 0.8, at the saturation threshold (>=)
    for i in range(4):
        cid = f"dup{i}"
        rows.append({"phase": "dedup", "cycle_id": cid, "decision": "skipped_duplicate", "ts": _ts(now, 10 + i)})
        rows.append({"phase": "outcome", "cycle_id": cid, "outcome": "skipped-duplicate", "ts": _ts(now, 10 + i)})
    rows.append({"phase": "dedup", "cycle_id": "ok1", "decision": "proceeded", "ts": _ts(now, 5)})
    rows.append({"phase": "gate", "cycle_id": "ok1", "allowed": True, "ts": _ts(now, 4)})
    rows.append({"phase": "outcome", "cycle_id": "ok1", "outcome": "success", "ts": _ts(now, 3)})
    _write_jsonl(ledger_dir / "cycles.jsonl", rows)

    report = mod.build_report(tmp_path, days=7)
    assert report["metrics"]["duplicate_rate"]["value"] == pytest.approx(0.8)
    assert report["liveness"]["state"] == "degraded"


def _add_hourly_proposed_rows(rows: list[dict], now: datetime, count: int = 5, start_minutes_ago: int = 300) -> None:
    """Append ``count`` 'proposed'-phase rows on an hourly cadence, oldest first."""
    for i in range(count):
        minutes_ago = start_minutes_ago - i * 60
        rows.append(
            {
                "phase": "proposed",
                "cycle_id": f"proposed{i}",
                "task_title": f"task {i}",
                "target_path": "scripts/foo.py",
                "ts": _ts(now, minutes_ago),
            }
        )


def test_liveness_healthy_with_cadence_aware_threshold(tmp_path, mod):
    """Hourly proposal cadence + last success 70 min ago -> healthy (within 2x median gap)."""
    now = datetime.now(timezone.utc)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = [
        {"phase": "started", "cycle_id": "c1", "ts": _ts(now, 71)},
        {"phase": "dedup", "cycle_id": "c1", "decision": "proceeded", "ts": _ts(now, 70)},
        {"phase": "gate", "cycle_id": "c1", "allowed": True, "ts": _ts(now, 70)},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _ts(now, 70)},
    ]
    _add_hourly_proposed_rows(rows, now)
    _write_jsonl(ledger_dir / "cycles.jsonl", rows)

    report = mod.build_report(tmp_path, days=7)
    liveness = report["liveness"]
    assert liveness["state"] == "healthy"
    assert liveness["threshold_source"] == "proposal_cadence"
    assert liveness["median_proposal_gap_seconds"] == pytest.approx(3600, abs=1)
    assert liveness["effective_threshold_seconds"] == pytest.approx(7200, abs=1)


def test_liveness_degraded_with_cadence_aware_threshold_when_stale(tmp_path, mod):
    """Same hourly cadence, but last success 3h ago -> degraded (beyond 2x median gap)."""
    now = datetime.now(timezone.utc)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = [
        {"phase": "started", "cycle_id": "c1", "ts": _ts(now, 181)},
        {"phase": "dedup", "cycle_id": "c1", "decision": "proceeded", "ts": _ts(now, 180)},
        {"phase": "gate", "cycle_id": "c1", "allowed": True, "ts": _ts(now, 180)},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _ts(now, 180)},
    ]
    _add_hourly_proposed_rows(rows, now)
    _write_jsonl(ledger_dir / "cycles.jsonl", rows)

    report = mod.build_report(tmp_path, days=7)
    liveness = report["liveness"]
    assert liveness["state"] == "degraded"
    assert liveness["threshold_source"] == "proposal_cadence"
    assert liveness["effective_threshold_seconds"] == pytest.approx(7200, abs=1)


def test_liveness_uses_legacy_threshold_with_zero_proposed_rows(tmp_path, mod):
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)  # no 'proposed' rows in this fixture
    report = mod.build_report(state_dir, days=7)
    liveness = report["liveness"]
    assert liveness["threshold_source"] == "legacy_default"
    assert liveness["median_proposal_gap_seconds"] is None
    assert liveness["effective_threshold_seconds"] == mod.LIVENESS_STALE_SECONDS_DEFAULT
    assert liveness["state"] == "healthy"  # unchanged legacy behavior


def test_liveness_uses_legacy_threshold_with_one_proposed_row(tmp_path, mod):
    now = datetime.now(timezone.utc)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    rows = [
        {"phase": "proposed", "cycle_id": "p1", "task_title": "x", "target_path": "y.py", "ts": _ts(now, 100)},
        {"phase": "started", "cycle_id": "c1", "ts": _ts(now, 200)},
        {"phase": "dedup", "cycle_id": "c1", "decision": "proceeded", "ts": _ts(now, 199)},
        {"phase": "gate", "cycle_id": "c1", "allowed": True, "ts": _ts(now, 198)},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _ts(now, 197)},
    ]
    _write_jsonl(ledger_dir / "cycles.jsonl", rows)

    report = mod.build_report(tmp_path, days=7)
    liveness = report["liveness"]
    assert liveness["threshold_source"] == "legacy_default"
    assert liveness["median_proposal_gap_seconds"] is None
    assert liveness["effective_threshold_seconds"] == mod.LIVENESS_STALE_SECONDS_DEFAULT
    # Even at 197 min stale, legacy fallback applies no age-based check.
    assert liveness["state"] == "healthy"


def test_liveness_json_contract_backward_compatible(tmp_path, mod):
    """All pre-existing liveness keys remain present alongside the new #740 fields."""
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)
    report = mod.build_report(state_dir, days=7)
    liveness = report["liveness"]
    for key in ("state", "last_productive_ts", "last_cycle_ts"):
        assert key in liveness
    for key in ("effective_threshold_seconds", "threshold_source", "median_proposal_gap_seconds"):
        assert key in liveness
    json.dumps(report)  # must still be JSON-serializable


def test_liveness_dead_when_empty_ledger(tmp_path, mod):
    report = mod.build_report(tmp_path, days=7)
    assert report["liveness"]["state"] == "dead"
    assert report["window"]["n_cycles"] == 0
    assert report["liveness"]["last_cycle_ts"] is None


def test_empty_ledger_renders_clean_table_not_a_crash(tmp_path, mod):
    report = mod.build_report(tmp_path, days=7)
    table = mod.render_table(report)
    assert "DEAD" in table
    assert "no cycles in window" in table
    assert "n/a" in table


# ─── CLI / self-test ───────────────────────────────────────────────────────


def test_self_test_runs_without_error(mod, capsys):
    mod._self_test()
    captured = capsys.readouterr()
    assert "PASS" in captured.out


def test_main_json_output(tmp_path, mod, capsys, monkeypatch):
    now = datetime.now(timezone.utc)
    state_dir = _make_ledger(tmp_path, now)
    exit_code = mod.main(["--state-dir", str(state_dir), "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["window"]["n_cycles"] == 5
