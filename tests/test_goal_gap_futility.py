from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import goal_gap_futility as futility


def _gap(gap_id="goal-gap-x", current=0.5, direction="max"):
    return {"id": gap_id, "metric": "repeat_failure_rate", "current": current, "target": 0.3, "direction": direction}


def _row(state, gap_id, cycle, ts):
    path = Path(state) / "ledger" / "cycles.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"phase": "proposed", "cycle_id": cycle, "demand_id": gap_id, "ts": ts}) + "\n")
        fh.write(json.dumps({"phase": "outcome", "cycle_id": cycle, "outcome": "success", "integrated": True, "ts": ts}) + "\n")


def test_futile_gap_suppresses_flat_metric_and_records_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "3")
    state = tmp_path / "state"
    assert futility.futile_gap_ids(state, [_gap()]) == set()
    first = datetime.now(timezone.utc).isoformat()
    for i in range(3):
        _row(state, "goal-gap-x", f"c{i}", first)
    assert "goal-gap-x" in futility.futile_gap_ids(state, [_gap()])
    rec = json.loads((state / "demand" / "futility.json").read_text())["goal-gap-x"]
    assert {"gap_id", "metric", "attempt_count", "metric_delta"} <= rec.keys()
    rows = [json.loads(x) for x in (state / "ledger" / "cycles.jsonl").read_text().splitlines() if "goal_gap_futile" in x]
    assert rows and rows[-1]["metric"] == "repeat_failure_rate"


def test_ledger_event_exact_ac4_fields(tmp_path, monkeypatch):
    """AC4: ledger event carries gap_id, metric name, attempt_count, metric_delta
    with correct values — not just key presence.
    Fails on pre-#996 code (no goal_gap_futile event emitted at all).
    """
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "2")
    state = tmp_path / "state"
    futility.futile_gap_ids(state, [_gap()])  # create initial record
    ts = datetime.now(timezone.utc).isoformat()
    for i in range(2):
        _row(state, "goal-gap-x", f"c{i}", ts)
    futility.futile_gap_ids(state, [_gap()])  # trigger futile → emits ledger event
    ledger_text = (state / "ledger" / "cycles.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in ledger_text.splitlines() if "goal_gap_futile" in line]
    assert events, "no goal_gap_futile event written to ledger"
    ev = events[-1]
    assert ev["phase"] == "goal_gap_futile"
    assert ev["gap_id"] == "goal-gap-x"
    assert ev["metric"] == "repeat_failure_rate"
    assert ev["attempt_count"] == 2, f"expected 2 attempts, got {ev['attempt_count']}"
    assert ev["metric_delta"] == 0.0, f"expected 0.0 delta (flat metric), got {ev['metric_delta']}"
    assert "ts" in ev, "ledger event missing ts"


def test_sidecar_exact_attempt_count_and_delta(tmp_path, monkeypatch):
    """AC4: sidecar record carries correct values (not just key presence):
    attempt_count == N_PROPOSALS and metric_delta == 0.0 for a flat metric.
    Fails on pre-#996 code (no futility.json written).
    """
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "3")
    state = tmp_path / "state"
    futility.futile_gap_ids(state, [_gap()])
    ts = datetime.now(timezone.utc).isoformat()
    for i in range(3):
        _row(state, "goal-gap-x", f"c{i}", ts)
    futility.futile_gap_ids(state, [_gap()])
    rec = json.loads((state / "demand" / "futility.json").read_text())["goal-gap-x"]
    assert rec["gap_id"] == "goal-gap-x"
    assert rec["metric"] == "repeat_failure_rate"
    assert rec["attempt_count"] == 3, f"expected 3 attempts, got {rec['attempt_count']}"
    assert rec["metric_delta"] == 0.0, f"expected 0.0 delta (flat metric), got {rec['metric_delta']}"


def test_improved_metric_not_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "2")
    state = tmp_path / "state"
    assert futility.futile_gap_ids(state, [_gap()]) == set()
    ts = datetime.now(timezone.utc).isoformat()
    for i in range(2):
        _row(state, "goal-gap-x", f"c{i}", ts)
    assert futility.futile_gap_ids(state, [_gap(current=0.1)]) == set()


def test_suppression_expires_after_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "1")
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_TTL_DAYS", "1")
    state = tmp_path / "state"
    assert futility.futile_gap_ids(state, [_gap()]) == set()
    ts = datetime.now(timezone.utc).isoformat()
    _row(state, "goal-gap-x", "c0", ts)
    assert "goal-gap-x" in futility.futile_gap_ids(state, [_gap()])
    path = state / "demand" / "futility.json"
    data = json.loads(path.read_text())
    data["goal-gap-x"]["futile_until"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text(json.dumps(data))
    assert futility.futile_gap_ids(state, [_gap()]) == set()


def test_deny_set_contains_module():
    from nanobot.runtime.runtime_deny import _RUNTIME_DENY_ALWAYS_FILES
    assert "nanobot/runtime/goal_gap_futility.py" in _RUNTIME_DENY_ALWAYS_FILES


def test_no_llm_and_snapshot(tmp_path):
    source = Path("nanobot/runtime/goal_gap_futility.py").read_text()
    assert not any(token in source for token in ("openai", "litellm", "LLMProvider"))
    assert futility.futility_snapshot(tmp_path / "state") == {"futile_gap_ids": [], "total_tracked": 0}
