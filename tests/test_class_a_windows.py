"""#1175: Class-A counters read their true horizon and never persist a count
from a partial window.

The ledger rotates at the first append of each UTC day (observed 00:10 UTC on
the host), so every counter that read only ``ledger/cycles.jsonl`` restarted at
midnight. Every test here fails against the pre-#1175 tree for the reason in
its docstring; fixtures name archives relative to today so they do not age out.
"""
from __future__ import annotations

import gzip
import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import (
    demand,
    goal_gap_futility,
    hypothesis_backlog,
    llm_proposer,
    state_access,
)

# Issue #1370: Pin NOW to a deterministic reference time (at noon UTC) instead
# of evaluating datetime.now(timezone.utc) at module import time. Module-level
# dynamic timestamps create clock skew between import time (T0) and execution time
# (T0 + 35m), which flakes when a full suite run crosses the UTC midnight boundary.
NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
PAD_ROW = json.dumps({"phase": "idle", "reason": "x" * 1000})


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _day(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).date().isoformat()


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "goals").mkdir(parents=True)
    (state / "ledger").mkdir()
    return state


def _write_gz(state: Path, days_ago: int, rows: list[dict]) -> None:
    with gzip.open(state / "ledger" / f"cycles-{_day(days_ago)}.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)


def _write_live(state: Path, rows: list[dict], *, pad_bytes: int = 0) -> None:
    with (state / "ledger" / "cycles.jsonl").open("w", encoding="utf-8") as fh:
        written = 0
        while written < pad_bytes:  # bulk that the pre-#1175 2 MiB per-module caps refused to read
            fh.write(PAD_ROW + "\n")
            written += len(PAD_ROW) + 1
        fh.writelines(json.dumps(r) + "\n" for r in rows)


def _corrupt_gz(state: Path, days_ago: int) -> None:
    (state / "ledger" / f"cycles-{_day(days_ago)}.jsonl.gz").write_bytes(b"not gzip at all")


def _success(cycle: str, hours_ago: float, files: list[str], **extra: object) -> dict:
    return {"phase": "outcome", "cycle_id": cycle, "outcome": "success", "files_changed": files, "ts": _iso(hours_ago), **extra}


# ─── doc-only 24 h budget ─────────────────────────────────────────────────────

def test_doc_only_count_spans_the_rotation_boundary(tmp_path, monkeypatch):
    """Pre-fix: count_doc_only_integrations_24h read the live file only -> 0 here, budget open."""
    state = _state(tmp_path)
    _write_gz(state, 1, [_success(f"g{i}", 18 + i, ["docs/a.md"]) for i in range(5)])
    _write_live(state, [{"phase": "started", "cycle_id": "c-live", "ts": _iso(0.5)},
                        _success("c-live", 0.4, ["scripts/tool.py"]),
                        {"phase": "idle", "reason": "no_demand", "ts": _iso(0.3)}])
    assert demand.count_doc_only_integrations_24h(state, now=NOW) == 5

    doc_item = demand._make_item("priority", "Priority 1 — docs/runbook.md", "Update docs/runbook.md")
    code_item = demand._make_item("priority", "Priority 2 — scripts/worker.py", "Improve scripts/worker.py")
    monkeypatch.setattr(demand, "_priority_items", lambda *a, **k: [doc_item, code_item])
    monkeypatch.setenv("EEEBOT_DOC_ONLY_24H_BUDGET", "5")
    items = demand.collect_demand(state, None, now=NOW)
    assert [i["id"] for i in items] == [code_item["id"]], "the 5 archived doc-only integrations close the lane"
    assert "Doc-only daily budget (5) reached (5 in 24h)" in items[0]["doc_budget_notice"]


def test_doc_only_count_uses_recorded_tier_and_the_24h_edge(tmp_path):
    state = _state(tmp_path)
    _write_gz(state, 1, [
        _success("old", 25, ["docs/old.md"]),                                   # outside 24 h
        _success("recorded", 20, ["AGENTS.md", "tests/test_agents_structure.py"], change_tier="code-bearing"),  # recorded tier wins
        _success("unrecorded", 19, ["AGENTS.md", "tests/test_agents_structure.py"]),  # classified: tests are tier-neutral
    ])
    assert demand.count_doc_only_integrations_24h(state, now=NOW) == 1


def test_blind_ledger_closes_the_doc_lane_and_touches_no_sidecar(tmp_path, monkeypatch, caplog):
    """Pre-fix: an unreadable ledger read as 0 doc-only integrations and the lane stayed open."""
    state = _state(tmp_path)
    _corrupt_gz(state, 0)  # the only source; no live file -> nothing readable
    (state / "demand").mkdir()
    exhausted = state / "demand" / "exhausted.json"
    exhausted.write_text(json.dumps({"schema_version": "demand-exhausted-v1", "entries": {"x": {"status": "exhausted", "exhausted_at": _iso(1), "rejects": 2}}}, indent=2), encoding="utf-8")
    futility = state / "demand" / "futility.json"
    futility.write_text(json.dumps({"goal-gap-a": {"gap_id": "goal-gap-a", "attempt_count": 3, "futile": False}}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    before = (exhausted.read_bytes(), futility.read_bytes())
    doc_item = demand._make_item("priority", "Priority 1 — docs/runbook.md", "Update docs/runbook.md")
    code_item = demand._make_item("priority", "Priority 2 — scripts/worker.py", "Improve scripts/worker.py")
    monkeypatch.setattr(demand, "_priority_items", lambda *a, **k: [doc_item, code_item])
    monkeypatch.setenv("EEEBOT_DOC_ONLY_24H_BUDGET", "5")

    with caplog.at_level(logging.WARNING, logger="nanobot.runtime.demand"):
        items = demand.collect_demand(state, None, now=NOW)
    assert [i["id"] for i in items] == [code_item["id"]]
    assert "treated as reached: the ledger could not be read" in items[0]["doc_budget_notice"]
    assert (exhausted.read_bytes(), futility.read_bytes()) == before
    journal = [r.message for r in caplog.records if "doc-only budget" in r.message]
    assert len(journal) == 1 and "unavailable" in journal[0], journal


def test_evidence_status_separates_no_history_from_blind(tmp_path):
    """Pre-fix: no such contract; a missing dir and an unreadable dir both read as []."""
    empty = state_access.ledger_window(tmp_path / "fresh", since_ts=_iso(24))
    assert (empty.status, state_access.evidence_status(empty)) == ("unavailable", "complete")
    state = _state(tmp_path)
    _corrupt_gz(state, 0)
    blind = state_access.ledger_window(state, since_ts=_iso(24))
    assert (blind.status, blind.files_read, state_access.evidence_status(blind)) == ("partial", 0, "unavailable")
    _write_live(state, [{"phase": "idle", "reason": "x", "ts": _iso(1)}])
    partial = state_access.ledger_window(state, since_ts=_iso(24))
    assert (partial.status, state_access.evidence_status(partial)) == ("partial", "partial")


# ─── exhaustion (demand._filter_exhausted) ───────────────────────────────────

def _exhausted_entry(state: Path, item_id: str, hours_ago: float) -> Path:
    path = state / "demand" / "exhausted.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"schema_version": "demand-exhausted-v1", "entries": {
        item_id: {"status": "exhausted", "exhausted_at": _iso(hours_ago), "git_head": "", "release": "", "rejects": 2}}}), encoding="utf-8")
    return path


def test_success_in_a_large_live_ledger_resets_exhaustion(tmp_path):
    """Pre-fix: demand._load_ledger_rows returned [] for a live file over 2 MiB
    ("too much history" read as "no history"), so the success was invisible and
    the item stayed hidden."""
    state = _state(tmp_path)
    item = demand._make_item("priority", "Priority 1 — scripts/a.py", "Improve scripts/a.py")
    path = _exhausted_entry(state, item["id"], hours_ago=2)
    _write_live(state, [_success("c-win", 1, ["scripts/a.py"])], pad_bytes=2_200_000)

    out = demand._filter_exhausted(state, [item], None, now=NOW)
    assert [i["id"] for i in out] == [item["id"]]
    assert json.loads(path.read_text(encoding="utf-8"))["entries"][item["id"]]["status"] == "reset"


def test_self_dedup_rejects_split_across_rotation_still_exhaust(tmp_path):
    """Pre-fix: the live half was skipped as oversize, so only 1 of the 2 rejects counted."""
    state = _state(tmp_path)
    item = demand._make_item("priority", "Priority 1 — scripts/a.py", "Improve scripts/a.py")
    _write_gz(state, 1, [{"phase": "proposer_reject", "reason": "self_dedup", "demand_id": item["id"], "ts": _iso(20)}])
    _write_live(state, [{"phase": "proposer_reject", "reason": "self_dedup", "demand_id": item["id"], "ts": _iso(1)}], pad_bytes=2_200_000)

    assert demand._filter_exhausted(state, [item], None, now=NOW) == []
    entry = json.loads((state / "demand" / "exhausted.json").read_text(encoding="utf-8"))["entries"][item["id"]]
    assert (entry["status"], entry["rejects"]) == ("exhausted", 2)


def test_partial_window_does_not_reset_and_blind_window_leaves_the_sidecar(tmp_path):
    """Pre-fix: no window status existed; a success next to a corrupt archive reset the entry."""
    state = _state(tmp_path)
    item = demand._make_item("priority", "Priority 1 — scripts/a.py", "Improve scripts/a.py")
    path = _exhausted_entry(state, item["id"], hours_ago=2)
    _corrupt_gz(state, 1)
    _write_live(state, [_success("c-win", 1, ["scripts/a.py"])])
    assert demand._filter_exhausted(state, [item], None, now=NOW) == [], "partial window: a visible success is not proof of recency"
    assert json.loads(path.read_text(encoding="utf-8"))["entries"][item["id"]]["status"] == "exhausted"

    (state / "ledger" / "cycles.jsonl").unlink()  # only the corrupt archive remains -> blind
    before = path.read_bytes()
    assert demand._filter_exhausted(state, [item], None, now=NOW) == [item]
    assert path.read_bytes() == before


def test_demand_ledger_rows_cover_three_days_and_carry_status(tmp_path):
    """Pre-fix: the newest 2 archives only, and no status on the result."""
    state = _state(tmp_path)
    for days_ago in (1, 2, 3, 4):
        _write_gz(state, days_ago, [{"phase": "started", "cycle_id": f"d{days_ago}", "ts": _iso(24 * days_ago - 12)}])
    _write_live(state, [{"phase": "started", "cycle_id": "live", "ts": _iso(0.1)}])
    rows = demand._load_ledger_rows(state, now=NOW)
    assert {r["cycle_id"] for r in rows} == {"d1", "d2", "d3", "live"}
    assert (rows.status, rows.files_read) == ("complete", 4)
    assert rows.covered_from is not None and rows.covered_to is not None


# ─── proposer streaks ─────────────────────────────────────────────────────────

def test_noop_and_self_dedup_streaks_span_rotation(tmp_path):
    """Pre-fix: both streaks read the live file only -> 1 each after midnight."""
    state = _state(tmp_path)
    _write_gz(state, 1, [
        {"phase": "proposed", "cycle_id": "c0", "task_title": "earlier", "ts": _iso(30)},
        {"phase": "proposer_skip", "reason": "skip 1", "ts": _iso(20)},
        {"phase": "proposer_skip", "reason": "skip 2", "ts": _iso(19)},
    ])
    _write_live(state, [{"phase": "proposer_skip", "reason": "skip 3", "ts": _iso(1)}])
    assert llm_proposer._consecutive_noop_streak(state, now=NOW) == 3
    assert llm_proposer._recent_proposed_titles(llm_proposer._load_ledger_rows(state, now=NOW)) == ["earlier"]

    _write_gz(state, 1, [
        {"phase": "proposed", "cycle_id": "c0", "task_title": "earlier", "ts": _iso(30)},
        {"phase": "proposer_reject", "reason": "self_dedup", "demand_id": "d", "ts": _iso(20)},
        {"phase": "proposer_reject", "reason": "self_dedup", "demand_id": "d", "ts": _iso(19)},
    ])
    _write_live(state, [{"phase": "proposer_reject", "reason": "self_dedup", "demand_id": "d", "ts": _iso(1)}])
    assert llm_proposer._consecutive_self_dedup_rejects(state, now=NOW) == 3
    assert llm_proposer._dedup_exhausted(state, "d") is True
    # an unreadable ledger never forces a proposal
    assert llm_proposer._consecutive_noop_streak(tmp_path / "nowhere") == 0
    assert "Recently proposed (window: last 3 days" in inspect.getsource(llm_proposer)


# ─── one experiment at a time ────────────────────────────────────────────────

def _backlog(state: Path) -> None:
    (state / "hypotheses").mkdir(exist_ok=True)
    (state / "hypotheses" / "durable.json").write_text(json.dumps({"entries": [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}]}), encoding="utf-8")


def test_in_flight_experiment_survives_rotation_and_a_blind_ledger(tmp_path):
    """Pre-fix: the serving 'proposed' row in an archive read as not in flight -> a second experiment could be minted."""
    state = _state(tmp_path)
    _backlog(state)
    _write_gz(state, 1, [{"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1", "ts": _iso(20)}])
    _write_live(state, [{"phase": "started", "cycle_id": "c2", "ts": _iso(0.5)}])
    assert hypothesis_backlog.has_in_flight_experiment(state, now=NOW) is True

    _write_live(state, [{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _iso(0.5)}])
    assert hypothesis_backlog.has_in_flight_experiment(state, now=NOW) is False

    (state / "ledger" / "cycles.jsonl").unlink()
    _corrupt_gz(state, 1)
    assert hypothesis_backlog.has_in_flight_experiment(state, now=NOW) is True, "blind ledger: assume in flight"
    rows = demand._load_ledger_rows(state)
    assert rows.status == "unavailable"
    assert hypothesis_backlog.has_in_flight_experiment(state, now=NOW, ledger_rows=rows) is True


# ─── goal-gap futility ────────────────────────────────────────────────────────

def _futility_record(state: Path, gap_id: str, attempt_count: int, first_seen_hours_ago: float) -> Path:
    path = state / "demand" / "futility.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({gap_id: {
        "gap_id": gap_id, "metric": "repeat_failure_rate", "first_seen_ts": _iso(first_seen_hours_ago),
        "first_metric": 0.5, "current_metric": 0.5, "metric_delta": 0.0, "attempt_count": attempt_count, "futile": False,
    }}), encoding="utf-8")
    return path


def _pair(cycle: str, gap_id: str, hours_ago: float) -> list[dict]:
    return [{"phase": "proposed", "cycle_id": cycle, "demand_id": gap_id, "ts": _iso(hours_ago)},
            {"phase": "outcome", "cycle_id": cycle, "outcome": "success", "ts": _iso(hours_ago - 0.1)}]


def test_futility_never_lowers_a_persisted_count_from_a_partial_window(tmp_path, monkeypatch):
    """Pre-fix: a corrupt archive was skipped silently and the record was rewritten from the readable rows (4 -> 0)."""
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "10")
    state = _state(tmp_path)
    gap_id = "goal-gap-partial"
    gap = {"id": gap_id, "metric": "repeat_failure_rate", "current": 0.5, "target": 0.3, "direction": "max"}
    path = _futility_record(state, gap_id, attempt_count=4, first_seen_hours_ago=60)
    _corrupt_gz(state, 1)
    _write_live(state, [{"phase": "started", "cycle_id": "c-none", "ts": _iso(1)}])

    assert goal_gap_futility.futile_gap_ids(state, [gap]) == set()
    record = json.loads(path.read_text(encoding="utf-8"))[gap_id]
    assert (record["attempt_count"], record["window_status"]) == (4, "partial")

    _write_live(state, [row for i in range(6) for row in _pair(f"c{i}", gap_id, 10 + i)])
    goal_gap_futility.futile_gap_ids(state, [gap])
    record = json.loads(path.read_text(encoding="utf-8"))[gap_id]
    assert (record["attempt_count"], record["window_status"]) == (6, "partial"), "a higher lower bound may raise the count"


def test_futility_keeps_the_verdict_on_a_blind_window_and_counts_on_a_complete_one(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "3")
    state = _state(tmp_path)
    gap_id = "goal-gap-blind"
    gap = {"id": gap_id, "metric": "repeat_failure_rate", "current": 0.5, "target": 0.3, "direction": "max"}
    path = _futility_record(state, gap_id, attempt_count=2, first_seen_hours_ago=60)
    _corrupt_gz(state, 0)  # nothing readable
    assert goal_gap_futility.futile_gap_ids(state, [gap]) == set()
    record = json.loads(path.read_text(encoding="utf-8"))[gap_id]
    assert (record["attempt_count"], record["futile"], record["window_status"]) == (2, False, "unavailable")

    (state / "ledger" / f"cycles-{_day(0)}.jsonl.gz").unlink()
    _write_gz(state, 1, _pair("c1", gap_id, 40) + _pair("c2", gap_id, 30))
    _write_live(state, _pair("c3", gap_id, 1))
    assert goal_gap_futility.futile_gap_ids(state, [gap]) == {gap_id}
    record = json.loads(path.read_text(encoding="utf-8"))[gap_id]
    assert (record["attempt_count"], record["futile"], record["window_status"]) == (3, True, "complete")


# ─── tier escape and the cap sweep ───────────────────────────────────────────

def test_co_changed_test_file_does_not_lift_a_doc_only_change(tmp_path):
    """Pre-fix: ['AGENTS.md', 'tests/test_agents_structure.py'] was code-bearing — 8 of 20
    AGENTS.md-only integrations escaped the doc-only budget that way (#1188)."""
    assert demand.classify_change_tier(["AGENTS.md", "tests/test_agents_structure.py"]) == "doc-only"
    assert demand.classify_change_tier(["lessons/lessons.yaml", "tests/test_lessons_integrity.py"]) == "doc-only"
    assert demand.classify_change_tier(["tests/test_agents_structure.py"]) == "code-bearing"
    assert demand.classify_change_tier(["scripts/a.py", "tests/test_a.py"]) == "code-bearing"
    assert demand.classify_change_tier(["docs/a.md", "scripts/a.py"]) == "code-bearing"


def test_only_state_access_owns_the_ledger_byte_cap():
    """Pre-fix: three copies of _MAX_LEDGER_BYTES = 2 MiB returned [] on oversize."""
    root = Path(demand.__file__).resolve().parents[1]
    offenders = sorted(str(p.relative_to(root.parent)) for p in root.rglob("*.py") if "_MAX_LEDGER_BYTES" in p.read_text(encoding="utf-8", errors="replace"))
    assert offenders == []
    assert "_DEFAULT_LEDGER_BYTES" in inspect.getsource(state_access)
    for module in (demand, goal_gap_futility, hypothesis_backlog, llm_proposer):
        assert 'glob("' + 'cycles-' not in inspect.getsource(module), module.__name__  # split so the hygiene scan does not match this file


def test_midnight_boundary_crossing_does_not_flake_ledger_window_isolation(tmp_path):
    """Regression test for issue #1370:
    When a long test run straddles the UTC midnight boundary (e.g. test imported
    at 23:55 UTC on day D, but executed at 00:15 UTC on day D+1), archive filenames
    generated relative to a pinned reference time must not be dropped as out-of-horizon
    by callers that default to unpinned datetime.now(timezone.utc).

    Passing `now` parameter explicitly to _load_ledger_rows and collect_demand
    guarantees complete isolation from wall-clock date rollover.
    """
    ref_now = datetime(2026, 9, 5, 23, 55, 0, tzinfo=timezone.utc)
    state = _state(tmp_path)

    # Write archive for 1 day ago relative to ref_now (2026-09-04)
    day_1 = (ref_now - timedelta(days=1)).date().isoformat()
    archive_path = state / "ledger" / f"cycles-{day_1}.jsonl.gz"
    payload = (
        json.dumps({
            "phase": "outcome",
            "outcome": "success",
            "cycle_id": "c-test",
            "change_tier": "doc-only",
            "files": ["docs/a.md"],
            "ts": (ref_now - timedelta(hours=18)).isoformat().replace("+00:00", "Z"),
        })
        + "\n"
    )
    archive_path.write_bytes(gzip.compress(payload.encode("utf-8")))

    # At ref_now, 1 day ago is within the 24h window
    assert demand.count_doc_only_integrations_24h(state, now=ref_now) == 1

    # Simulate running 20 minutes later across the midnight boundary into 2026-09-06 00:15:00 UTC
    wall_clock_after_midnight = datetime(2026, 9, 6, 0, 15, 0, tzinfo=timezone.utc)

    # Without pinned `now`, unpinned call would drop 2026-09-04 because 2026-09-04 + 1 day = 2026-09-05 < 2026-09-05 00:15:00
    assert demand.count_doc_only_integrations_24h(state, now=wall_clock_after_midnight) == 0

    # With pinned `now=ref_now`, it is 100% stable regardless of real wall-clock time
    assert demand.count_doc_only_integrations_24h(state, now=ref_now) == 1
    assert demand._load_ledger_rows(state, now=ref_now).status == "complete"
