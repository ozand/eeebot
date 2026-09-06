from __future__ import annotations

import gzip
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


def test_suppressed_terminal_attempts_count_toward_demand_futility(
    tmp_path, monkeypatch,
):
    """#1211: the 79 suppressed attempts on one live gap must not read as 0.

    The two reasons remain distinct evidence and retain their own fixes; this
    counter answers the separate question "how many terminal attempts did this
    demand consume?" A proposal without an outcome is not terminal and does not
    count.
    """
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "100")
    state = tmp_path / "state"
    gap_id = "goal-gap-5d4d5a9dc822"
    futility.futile_gap_ids(state, [_gap(gap_id=gap_id)])
    ts = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    for index in range(79):
        cycle_id = f"suppressed-{index}"
        reason = (
            "existence_index_duplicate" if index < 49
            else "recent_duplicate_failure"
        )
        rows.extend([
            {
                "phase": "proposed", "cycle_id": cycle_id,
                "demand_id": gap_id, "ts": ts,
            },
            {
                "phase": "outcome", "cycle_id": cycle_id,
                "outcome": "skipped-duplicate", "reason": reason, "ts": ts,
            },
        ])
    rows.append({
        "phase": "proposed", "cycle_id": "still-pending",
        "demand_id": gap_id, "ts": ts,
    })
    _write_active(state, rows)

    futility.futile_gap_ids(state, [_gap(gap_id=gap_id)])

    record = json.loads(
        (state / "demand" / "futility.json").read_text(encoding="utf-8")
    )[gap_id]
    assert record["attempt_count"] == 79
    assert record["attempt_unit"] == "demand_id"


def test_gap_absent_from_current_rows_is_marked_stale_and_returns_on_reappearance(tmp_path, monkeypatch):
    state = tmp_path / "state"
    first = _gap("goal-gap-a")
    second = _gap("goal-gap-b")

    futility.futile_gap_ids(state, [first, second])
    futility.futile_gap_ids(state, [first])
    records = json.loads((state / "demand" / "futility.json").read_text(encoding="utf-8"))
    assert records["goal-gap-b"]["stale"] is True
    assert records["goal-gap-b"]["last_evaluated_ts"]
    assert records["goal-gap-b"]["futility_status"] == "not_evaluated"
    snapshot = futility.futility_snapshot(state)
    assert snapshot["stale_gap_ids"] == ["goal-gap-b"]
    assert snapshot["measured_gap_ids"] == ["goal-gap-a"]

    futility.futile_gap_ids(state, [second])
    records = json.loads((state / "demand" / "futility.json").read_text(encoding="utf-8"))
    assert records["goal-gap-b"].get("stale") is False
    assert records["goal-gap-b"]["futility_status"] == "measured"


def test_unreadable_gap_input_does_not_mark_existing_records_stale(tmp_path, monkeypatch):
    state = tmp_path / "state"
    first = _gap("goal-gap-a")
    futility.futile_gap_ids(state, [first])

    class UnavailableRows(list):
        status = "unavailable"

    monkeypatch.setattr(futility, "_evidence", lambda *args, **kwargs: ([], "unavailable"))
    records = json.loads((state / "demand" / "futility.json").read_text(encoding="utf-8"))
    before = records["goal-gap-a"].copy()
    assert futility.futile_gap_ids(state, [], ledger_rows=UnavailableRows()) == set()
    after = json.loads((state / "demand" / "futility.json").read_text(encoding="utf-8"))
    assert after["goal-gap-a"] == before


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
    assert futility.futility_snapshot(tmp_path / "state") == {
        "futile_gap_ids": [], "total_tracked": 0,
        "stale_gap_ids": [], "measured_gap_ids": [],
    }


# ---------------------------------------------------------------------------
# #1166: rotation-aware ledger reading
# ---------------------------------------------------------------------------

def _write_gz_archive(state: Path, day: str, rows: list[dict]) -> None:
    """Write rows to a rotated .gz archive named cycles-<day>.jsonl.gz."""
    gz_path = state / "ledger" / f"cycles-{day}.jsonl.gz"
    gz_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _write_active(state: Path, rows: list[dict]) -> None:
    """Append rows to the active cycles.jsonl."""
    active = state / "ledger" / "cycles.jsonl"
    active.parent.mkdir(parents=True, exist_ok=True)
    with active.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_rotation_split_proposed_gz_success_active(tmp_path, monkeypatch):
    """#1166 primary fix: a proposed row in a rotated .gz archive plus its
    matching outcome:success in the active file must be counted as one
    integrated attempt.  This test FAILS against pre-#1166 code because the
    old _rows() only scanned the active file and missed the archived proposed
    row, so integrated_count returned 0 forever (futility never fired).
    """
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "1")
    state = tmp_path / "state"
    gap_id = "goal-gap-rotated"

    # First call to seed the first_seen_ts
    first_ts = datetime(2026, 8, 31, 6, 0, 0, tzinfo=timezone.utc)
    # Manually pre-seed the sidecar so first_seen is the known day before rotation
    (state / "demand").mkdir(parents=True, exist_ok=True)
    sidecar = state / "demand" / "futility.json"
    sidecar.write_text(json.dumps({
        gap_id: {
            "gap_id": gap_id,
            "metric": "repeat_failure_rate",
            "first_seen_ts": first_ts.isoformat().replace("+00:00", "Z"),
            "first_metric": 0.5,
            "current_metric": 0.5,
            "metric_delta": 0.0,
            "attempt_count": 0,
            "futile": False,
        }
    }), encoding="utf-8")

    # proposed row in rotated archive (the day of first_seen)
    _write_gz_archive(state, "2026-08-31", [
        {
            "phase": "proposed",
            "cycle_id": "c-split",
            "demand_id": gap_id,
            "ts": "2026-08-31T10:00:00Z",
        }
    ])
    # matching success row in active file (today)
    _write_active(state, [
        {
            "phase": "outcome",
            "cycle_id": "c-split",
            "outcome": "success",
            "integrated": True,
            "ts": "2026-09-01T08:00:00Z",
        }
    ])

    gap = _gap(gap_id=gap_id, current=0.5, direction="max")
    result = futility.futile_gap_ids(state, [gap])
    assert gap_id in result, (
        f"Expected {gap_id!r} to be futile (rotation-split proposed+success), got {result!r}. "
        "This indicates the archive was not scanned (pre-#1166 behavior)."
    )


def test_archive_bound_stops_at_horizon(tmp_path, monkeypatch):
    """#1166 bound requirement: archives before first_seen are not opened.

    The open-call assertion proves this is a bounded horizon read rather than
    merely filtering rows after decompressing every archive.
    """
    state = tmp_path / "state"
    gap_id = "goal-gap-bound"

    horizon = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)  # bound is Sep 1

    # Archive BEFORE horizon (Aug 30) – must NOT be read by _rows(horizon=...)
    _write_gz_archive(state, "2026-08-30", [
        {
            "phase": "proposed",
            "cycle_id": "c-before",
            "demand_id": gap_id,
            "ts": "2026-08-30T10:00:00Z",
        }
    ])
    # Archive ON horizon (Sep 1) – MUST be read
    _write_gz_archive(state, "2026-09-01", [
        {
            "phase": "proposed",
            "cycle_id": "c-on-horizon",
            "demand_id": gap_id,
            "ts": "2026-09-01T01:00:00Z",
        }
    ])

    opened: list[str] = []
    original_open = gzip.open

    def recording_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(gzip, "open", recording_open)
    # #1175: the read goes through state_access.ledger_window, whose archive
    # selection keeps a day file when day + 1 day >= horizon (the file may hold
    # the horizon's own rows); an archive two days before the horizon is never
    # opened. Rows are filtered by ts, so nothing before the horizon leaks in.
    rows = futility._window(state, horizon).rows
    cycle_ids = {r.get("cycle_id") for r in rows}

    assert "c-on-horizon" in cycle_ids, "Archive on the horizon day must be included"
    assert "c-before" not in cycle_ids, "Pre-horizon archive row must be excluded"
    assert opened == ["cycles-2026-09-01.jsonl.gz"], (
        "reader must not open archives older than first_seen horizon"
    )


def test_unreadable_vs_empty_distinguishable(tmp_path):
    """#1166 secondary fix, restated on the #1175 contract: a ledger that cannot
    be read must not look like a ledger with nothing in it. ``_evidence``
    reports ``complete`` for a missing ledger directory (no history yet) and
    ``unavailable`` when every source was skipped.
    """
    state = tmp_path / "state"
    horizon = datetime.now(timezone.utc) - timedelta(days=1)

    rows, status = futility._evidence(state, horizon)
    assert (rows, status) == ([], "complete"), "missing ledger dir is genuine emptiness"

    ledger_dir = state / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    (ledger_dir / f"cycles-{day}.jsonl.gz").write_bytes(b"not gzip")
    rows, status = futility._evidence(state, horizon)
    assert (rows, status) == ([], "unavailable"), "a corrupt-only ledger is not evidence"


def test_rotation_split_via_ledger_rows_path(tmp_path, monkeypatch):
    """#1166 ledger_rows supplementation: when demand.py passes ledger_rows
    (active file only), futility must still supplement with archive rows so a
    proposed→success pair split by midnight rotation is counted.
    """
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "1")
    state = tmp_path / "state"
    gap_id = "goal-gap-lr-path"

    first_ts = datetime(2026, 8, 31, 6, 0, 0, tzinfo=timezone.utc)
    (state / "demand").mkdir(parents=True, exist_ok=True)
    sidecar = state / "demand" / "futility.json"
    sidecar.write_text(json.dumps({
        gap_id: {
            "gap_id": gap_id,
            "metric": "repeat_failure_rate",
            "first_seen_ts": first_ts.isoformat().replace("+00:00", "Z"),
            "first_metric": 0.5,
            "current_metric": 0.5,
            "metric_delta": 0.0,
            "attempt_count": 0,
            "futile": False,
        }
    }), encoding="utf-8")

    # proposed row in archive (only – NOT in active file)
    _write_gz_archive(state, "2026-08-31", [
        {
            "phase": "proposed",
            "cycle_id": "c-lr",
            "demand_id": gap_id,
            "ts": "2026-08-31T10:00:00Z",
        }
    ])
    # success row in active file
    success_row = {
        "phase": "outcome",
        "cycle_id": "c-lr",
        "outcome": "success",
        "integrated": True,
        "ts": "2026-09-01T08:00:00Z",
    }
    _write_active(state, [success_row])

    # Simulate demand.py passing ledger_rows with ONLY the active file rows
    ledger_rows = [success_row]  # no archive rows (as demand.py would provide)

    gap = _gap(gap_id=gap_id, current=0.5, direction="max")
    result = futility.futile_gap_ids(state, [gap], ledger_rows=ledger_rows)
    assert gap_id in result, (
        f"Expected {gap_id!r} to be futile even via ledger_rows path (archive supplementation), "
        f"got {result!r}"
    )
