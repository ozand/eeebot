"""#1184: goal-gap futility counts attempts by lever surface, not demand id, and
suppression follows the unit.

Live measurement (host, 2026-09-02, comment on #996): the flat ``stale_feeds``
gap had ``attempt_count: 1`` while 10 integrated cycles from the priority,
reflection and hypothesis lanes had edited ``scripts/collect_host_metrics.py``
and ``scripts/check_stale_feeds.py`` under 9 fresh demand ids. Every test here
fails against the pre-#1184 tree for the reason in its docstring.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import demand, goal_gap_futility, llm_proposer, scorecard
from nanobot.runtime.heldout import checkers

NOW = datetime.now(timezone.utc)
SURFACE = ["host_metrics", "stale_feed"]
GAP_ID = "goal-gap-a820ca0c8bb3"


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "goals").mkdir(parents=True)
    (state / "ledger").mkdir()
    (state / "demand").mkdir()
    return state


def _write_live(state: Path, rows: list[dict]) -> None:
    with (state / "ledger" / "cycles.jsonl").open("w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)


def _cycle(cycle: str, demand_id: str, files: list[str], hours_ago: float) -> list[dict]:
    """A proposed row (lane = demand id prefix) and its integrated outcome."""
    return [
        {"phase": "proposed", "cycle_id": cycle, "demand_id": demand_id, "target_path": files[0], "ts": _iso(hours_ago)},
        {"phase": "outcome", "cycle_id": cycle, "outcome": "success", "files_changed": files, "ts": _iso(hours_ago - 0.2)},
    ]


def _record(state: Path, gap_id: str, metric: str, *, attempt_count: int = 0, first_seen_hours_ago: float = 48, **extra) -> Path:
    path = state / "demand" / "futility.json"
    path.write_text(json.dumps({gap_id: {
        "gap_id": gap_id, "metric": metric, "first_seen_ts": _iso(first_seen_hours_ago), "first_metric": 1.0,
        "current_metric": 1.0, "metric_delta": 0.0, "attempt_count": attempt_count, "futile": False, **extra,
    }}), encoding="utf-8")
    return path


def _gap(gap_id: str = GAP_ID, metric: str = "stale_feeds", surface: list[str] | None = SURFACE, current: float = 1.0, direction: str = "max") -> dict:
    return {"id": gap_id, "metric": metric, "current": current, "target": 0.0, "direction": direction, "vector": "V1", "surface": list(surface or [])}


def _load_record(state: Path, gap_id: str = GAP_ID) -> dict:
    return json.loads((state / "demand" / "futility.json").read_text(encoding="utf-8"))[gap_id]


def _futile_row(state: Path) -> dict:
    rows = [json.loads(line) for line in (state / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines() if "goal_gap_futile" in line]
    assert rows, "no goal_gap_futile ledger row"
    return rows[-1]


# ─── counting ────────────────────────────────────────────────────────────────

def test_stale_feeds_counts_cross_lane_surface_hits_not_its_own_id(tmp_path):
    """Pre-fix: only cycles whose proposed row carried the gap's own demand id counted -> 2, never futile."""
    state = _state(tmp_path)
    _record(state, GAP_ID, "stale_feeds")
    lanes = ["priority-338ed4f63940", "reflection-241392e2feba", "hypothesis-9f06864f04c1"]
    rows: list[dict] = []
    for i in range(10):  # ten fresh ids across three lanes, all on the surface
        files = ["tests/test_collect_host_metrics.py"] if i == 9 else ["scripts/collect_host_metrics.py"]
        rows += _cycle(f"c{i}", f"{lanes[i % 3]}{i}", files, 40 - i)
    rows += _cycle("own-1", GAP_ID, ["scripts/verify_imports.py"], 20)  # the gap's own id, off the surface
    rows += _cycle("own-2", GAP_ID, ["docs/feeds.md"], 19)
    _write_live(state, rows)

    assert goal_gap_futility.futile_gap_ids(state, [_gap()]) == {GAP_ID}
    record = _load_record(state)
    assert (record["attempt_count"], record["attempt_unit"], record["surface"], record["futile"]) == (10, "lever_surface", SURFACE, True)
    assert len(record["attempt_sources"]) == 10
    assert record["attempt_sources"][0].keys() == {"cycle_id", "demand_id", "ts"}
    assert {s["demand_id"].split("-")[0] for s in record["attempt_sources"]} == {"priority", "reflection", "hypothesis"}
    row = _futile_row(state)
    assert (row["attempt_unit"], row["attempt_count"], row["futile"], row["gap_id"]) == ("lever_surface", 10, True, GAP_ID)


def test_defect_lane_is_exempt_and_unlinked_cycles_do_not_count(tmp_path):
    """Pre-fix: no lane concept at all (id-count only)."""
    state = _state(tmp_path)
    _record(state, GAP_ID, "stale_feeds")
    rows: list[dict] = []
    for i in range(10):
        rows += _cycle(f"d{i}", f"defect-4a4d672f6ac{i}", ["scripts/collect_host_metrics.py"], 40 - i)
    rows.append({"phase": "outcome", "cycle_id": "orphan", "outcome": "success", "files_changed": ["scripts/collect_host_metrics.py"], "ts": _iso(5)})
    rows += _cycle("r1", "reflection-aaaaaaaaaaaa", ["scripts/collect_host_metrics.py"], 4)
    _write_live(state, rows)

    assert goal_gap_futility.futile_gap_ids(state, [_gap()]) == set()
    record = _load_record(state)
    assert (record["attempt_count"], record["attempt_unit"]) == (1, "lever_surface")
    assert [s["cycle_id"] for s in record["attempt_sources"]] == ["r1"]


def test_heldout_counts_checker_hits_and_ignores_own_off_surface_proposals(tmp_path):
    """Pre-fix: the gap's own 3 proposals on non-checker files counted (3); the 2 checker edits did not."""
    state = _state(tmp_path)
    gap_id = "goal-gap-c09521b7459a"
    _record(state, gap_id, "heldout_gap")
    rows: list[dict] = []
    for i, path in enumerate(["scripts/verify_imports.py", "tests/test_summarize_failure_reasons.py", "tests/test_workspace_validation_helpers.py"]):
        rows += _cycle(f"own{i}", gap_id, [path], 30 - i)
    rows += _cycle("h1", "hypothesis-000000000001", ["scripts/eeebot_dashboard.py"], 20)
    rows += _cycle("h2", "hypothesis-000000000002", ["scripts/loop_health_report.py", "tests/test_loop_health_report.py"], 10)
    _write_live(state, rows)

    gap = _gap(gap_id, "heldout_gap", sorted(checkers.CHECKERS), current=0.5)
    goal_gap_futility.futile_gap_ids(state, [gap])
    record = _load_record(state, gap_id)
    assert (record["attempt_count"], record["attempt_unit"]) == (2, "lever_surface")
    assert record["surface"] == sorted(checkers.CHECKERS)
    assert [s["cycle_id"] for s in record["attempt_sources"]] == ["h1", "h2"]


def test_global_ratio_keeps_the_id_count_and_says_so(tmp_path):
    """Pre-fix: no attempt_unit / surface / attempt_sources on the record."""
    state = _state(tmp_path)
    gap_id = "goal-gap-2d9ab3aa9d09"
    _record(state, gap_id, "confirmed_ratio")
    rows = _cycle("o1", gap_id, ["scripts/eeebot_dashboard.py"], 30) + _cycle("o2", gap_id, ["scripts/x.py"], 20)
    rows += _cycle("shared", "reflection-bbbbbbbbbbbb", ["scripts/eeebot_dashboard.py"], 10)  # same path, other lane
    _write_live(state, rows)

    goal_gap_futility.futile_gap_ids(state, [_gap(gap_id, "confirmed_ratio", surface=[], current=0.41, direction="min")])
    record = _load_record(state, gap_id)
    assert (record["attempt_count"], record["attempt_unit"], record["surface"], record["attempt_sources"]) == (2, "demand_id", [], [])


def test_attempt_sources_keep_the_newest_twenty(tmp_path):
    state = _state(tmp_path)
    _record(state, GAP_ID, "stale_feeds")
    rows: list[dict] = []
    for i in range(25):
        rows += _cycle(f"c{i:02d}", f"reflection-{i:012d}", ["scripts/check_stale_feeds.py"], 47 - i)
    _write_live(state, rows)
    goal_gap_futility.futile_gap_ids(state, [_gap()])
    record = _load_record(state)
    assert record["attempt_count"] == 25
    assert [s["cycle_id"] for s in record["attempt_sources"]] == [f"c{i:02d}" for i in range(5, 25)]


def test_partial_window_applies_the_never_lower_rule_to_the_surface_count(tmp_path):
    """Pre-fix: the surface unit did not exist, so the id-count (0) was floored at the persisted 4."""
    state = _state(tmp_path)
    _record(state, GAP_ID, "stale_feeds", attempt_count=4, attempt_unit="lever_surface", surface=SURFACE)
    (state / "ledger" / f"cycles-{(NOW - timedelta(days=1)).date().isoformat()}.jsonl.gz").write_bytes(b"not gzip")
    rows: list[dict] = []
    for i in range(6):
        rows += _cycle(f"c{i}", f"reflection-{i:012d}", ["scripts/collect_host_metrics.py"], 10 - i)
    _write_live(state, rows)
    goal_gap_futility.futile_gap_ids(state, [_gap()])
    record = _load_record(state)
    assert (record["attempt_count"], record["window_status"], record["attempt_unit"]) == (6, "partial", "lever_surface")


# ─── the surface comes from the scorecard, with the direction ────────────────

def _snapshot() -> dict:
    return {
        "loop": {"repeat_failure_rate": 0.5},
        "quality": {"compile_clean_ratio": 0.9, "script_count": 10},
        "cost": {"tokens_per_integration": 100.0},
        "value": {"confirmed_ratio": 0.2, "completed_declared": 5, "owner_live_ratio": 0.9, "owner_live_inventory": 5},
        "heldout": {"heldout_gap": 0.5},
        "feeds": {"stale": 1, "total": 5, "stale_names": ["host_metrics"]},
    }


def test_scorecard_gaps_carry_direction_and_lever_surface(tmp_path):
    """Pre-fix: gap dicts had neither key, so futility's improvement test never fired and no surface existed."""
    state = _state(tmp_path)
    (state / "demand" / "py_compile_watermark.json").write_text(json.dumps({
        "schema_version": "demand-py-compile-watermark-v1", "git_head": "abc",
        "failures": [{"path": "scripts/broken_b.py", "error": "SyntaxError"}, {"path": "scripts/broken_a.py", "error": "SyntaxError"}],
    }), encoding="utf-8")
    gaps = {g["metric"]: g for g in scorecard._compute_gaps(_snapshot(), [], NOW, state_dir=state)}
    assert gaps["stale_feeds"]["direction"] == "max" and gaps["stale_feeds"]["surface"] == ["host_metrics", "stale_feed"]
    assert gaps["heldout_gap"]["surface"] == sorted(checkers.CHECKERS)
    assert gaps["compile_clean_ratio"]["surface"] == ["scripts/broken_a.py", "scripts/broken_b.py"]
    assert (gaps["confirmed_ratio"]["direction"], gaps["confirmed_ratio"]["surface"]) == ("min", [])
    assert (gaps["repeat_failure_rate"]["direction"], gaps["repeat_failure_rate"]["surface"]) == ("max", [])
    assert scorecard._compute_gaps(_snapshot(), [], NOW)  # state_dir stays optional for existing callers


def test_improving_gap_from_the_scorecard_is_not_futile(tmp_path, monkeypatch):
    """Pre-fix: scorecard gaps carried no direction, so _improved() was always False and an
    improving heldout_gap with enough attempts was marked futile anyway."""
    monkeypatch.setenv("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "2")
    state = _state(tmp_path)
    gap = next(g for g in scorecard._compute_gaps(_snapshot(), [], NOW, state_dir=state) if g["metric"] == "heldout_gap")
    gap_id = "goal-gap-c09521b7459a"
    path = _record(state, gap_id, "heldout_gap")
    data = json.loads(path.read_text(encoding="utf-8"))
    data[gap_id]["first_metric"] = 0.8  # metric fell 0.8 -> 0.5 (direction max): improving
    path.write_text(json.dumps(data), encoding="utf-8")
    rows = _cycle("h1", "hypothesis-000000000001", ["scripts/eeebot_dashboard.py"], 20) + _cycle("h2", "hypothesis-000000000002", ["scripts/prune_failed_backlog.py"], 10)
    _write_live(state, rows)
    assert goal_gap_futility.futile_gap_ids(state, [{**gap, "id": gap_id}]) == set()
    record = _load_record(state, gap_id)
    assert (record["attempt_count"], record["futile"]) == (2, False)


# ─── suppression follows the unit ────────────────────────────────────────────

def _futile_record(state: Path) -> None:
    _record(
        state, GAP_ID, "stale_feeds", attempt_count=10,
        attempt_unit="lever_surface", surface=SURFACE,
        futile=True,
        futile_until=(NOW + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
    )


def test_demand_drops_non_defect_items_aimed_at_a_futile_surface(tmp_path, monkeypatch, caplog):
    """Pre-fix: only the goal-gap item itself was hidden; 11 of 12 live attacks came from other lanes."""
    state = _state(tmp_path)
    _futile_record(state)
    reflection = demand._make_item("reflection", "Harden the host metrics collector retry loop", "cycle c: flaky", affected_path="scripts/collect_host_metrics.py")
    priority = demand._make_item("priority", "Priority 3 — restore scripts/check_stale_feeds.py output", "Fix the stale feed checker")
    defect = demand._make_item("defect", "scripts/collect_host_metrics.py fails to compile", "SyntaxError", affected_path="scripts/collect_host_metrics.py")
    elsewhere = demand._make_item("reflection", "Speed up scripts/test_runner.py", "slow", affected_path="scripts/test_runner.py")
    monkeypatch.setattr(demand, "_priority_items", lambda *a, **k: [priority, reflection, defect, elsewhere])

    with caplog.at_level(logging.WARNING, logger="nanobot.runtime.demand"):
        items = demand.collect_demand(state, None)
    assert [i["id"] for i in items] == [defect["id"], elsewhere["id"]]
    assert any("futile surface: dropped 2" in r.message for r in caplog.records)
    assert goal_gap_futility.futile_surfaces(state)[0]["gap_id"] == GAP_ID


def test_proposer_refuses_a_target_on_a_futile_surface_with_its_own_reason(tmp_path, monkeypatch):
    """Pre-fix: the proposal passed dedup and would have been written; no futile_surface reason existed."""
    state = _state(tmp_path)
    _futile_record(state)
    proposal = {"task_title": "Add retry to the host metrics collector", "target_path": "scripts/collect_host_metrics.py", "serves": "priority 1", "rationale": "r"}
    dup, feedback, matched = llm_proposer._is_duplicate_proposal(state, None, proposal)
    assert dup is True and matched == f"futile_surface:{GAP_ID}"
    assert "lever surface" in feedback and "10 lever_surface attempts" in feedback
    assert llm_proposer._is_duplicate_proposal(state, None, {**proposal, "target_path": "scripts/other.py"})[0] is False

    # same gates as tests/test_llm_proposer.py's autouse fixture: proposer on, the
    # pre-#760 supply-driven policy (no demand items needed to fire)
    monkeypatch.setenv("SELFEVO_LLM_PROPOSER_ENABLED", "1")
    monkeypatch.setenv("SELFEVO_DEMAND_DRIVEN_ENABLED", "0")
    (state / "goals" / "goal_text.json").write_text(json.dumps({"text": "no priority section, so should_propose is True"}), encoding="utf-8")
    monkeypatch.setattr(llm_proposer, "propose", lambda context, *, rejection_reason=None, timeout=120.0, system_prompt=None: dict(proposal))
    assert llm_proposer.maybe_propose(state, None) is None
    rows = [json.loads(line) for line in (state / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    rejects = [r for r in rows if r.get("phase") == "proposer_reject"]
    assert rejects and rejects[-1]["reason"] == "futile_surface"
    assert rejects[-1]["target_path"] == "scripts/collect_host_metrics.py" and rejects[-1]["matched_against"] == f"futile_surface:{GAP_ID}"


def test_expired_or_id_count_records_expose_no_surface(tmp_path):
    state = _state(tmp_path)
    _record(state, GAP_ID, "stale_feeds", attempt_count=10, attempt_unit="lever_surface", surface=SURFACE,
            futile=True, futile_until=_iso(1))  # expired an hour ago
    assert goal_gap_futility.futile_surfaces(state) == []
    _record(state, "goal-gap-2d9ab3aa9d09", "confirmed_ratio", attempt_count=12, attempt_unit="demand_id", surface=[],
            futile=True, futile_until=(NOW + timedelta(days=7)).isoformat().replace("+00:00", "Z"))
    assert goal_gap_futility.futile_surfaces(state) == []
    assert goal_gap_futility.futile_surface_for(state, "scripts/collect_host_metrics.py") is None
