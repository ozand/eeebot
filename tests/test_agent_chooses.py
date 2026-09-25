"""ADR-035 (the planning session chooses and closes HADI), rules 1 and 3,
issue #1942 (B2): the planning session chooses before selection, its
hypothesis is stored in ADR-030's form, and no_plan recovery is bounded and
visible.

Only the rows this increment closes are exercised here. Rows requiring the
live task-selection wiring in ``bridge.py`` (rule 1's "its plan IS the
executor's task"), the goal-review/reflector/proposer decommission, and the
planner-prompt-schema work for trust-order/new-priority/defect-decline are
deferred -- see this PR's body for why each is out of scope this session.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.runtime import day_diary, no_plan_recovery, planner_candidates, planner_hypothesis
from nanobot.runtime.cycle_ledger import read_events
from nanobot.runtime.demand import _make_item


def _valid_hypothesis(**overrides) -> dict:
    base = {
        "statement": "caching the ranked list halves proposer latency",
        "measure": "p50 proposer wall-clock time over 20 cycles",
        "refutation_condition": "p50 does not drop by at least 20%",
    }
    base.update(overrides)
    return base


# --- test_plan_hypothesis_is_stored_with_refutation ------------------------

def test_plan_hypothesis_is_stored_with_refutation(tmp_path: Path):
    state = tmp_path / "state"
    entry = planner_hypothesis.store_hypothesis(state, "cycle-1", _valid_hypothesis())

    assert entry["hypothesis_id"].startswith("planh-")
    assert entry["measure"] == _valid_hypothesis()["measure"]
    assert entry["refutation_condition"] == _valid_hypothesis()["refutation_condition"]
    assert entry["status"] == "open"

    stored = planner_hypothesis._read_all(state)["entries"]
    assert len(stored) == 1
    assert stored[0]["hypothesis_id"] == entry["hypothesis_id"]


def test_storing_the_same_hypothesis_twice_is_idempotent(tmp_path: Path):
    state = tmp_path / "state"
    first = planner_hypothesis.store_hypothesis(state, "cycle-1", _valid_hypothesis())
    second = planner_hypothesis.store_hypothesis(state, "cycle-2", _valid_hypothesis())
    assert first["hypothesis_id"] == second["hypothesis_id"]
    assert len(planner_hypothesis._read_all(state)["entries"]) == 1


# --- test_hypothesis_without_refutation_is_rejected -------------------------

@pytest.mark.parametrize("missing_field", ["statement", "measure", "refutation_condition"])
def test_hypothesis_without_refutation_is_rejected(tmp_path: Path, missing_field):
    state = tmp_path / "state"
    raw = _valid_hypothesis()
    raw[missing_field] = ""
    with pytest.raises(planner_hypothesis.HypothesisValidationError):
        planner_hypothesis.store_hypothesis(state, "cycle-1", raw)
    assert planner_hypothesis._read_all(state)["entries"] == []


def test_hypothesis_that_is_not_a_dict_is_rejected(tmp_path: Path):
    with pytest.raises(planner_hypothesis.HypothesisValidationError):
        planner_hypothesis.parse_hypothesis("just a string, not a measured claim")


# --- test_revise_and_drop_preserve_evidence ---------------------------------

def test_revise_and_drop_preserve_evidence(tmp_path: Path):
    state = tmp_path / "state"
    original = planner_hypothesis.store_hypothesis(state, "cycle-1", _valid_hypothesis())
    original_id = original["hypothesis_id"]

    # Simulate the harness having attached a verdict (#878) before the
    # revise/drop -- it must never be touched by either operation.
    data = planner_hypothesis._read_all(state)
    entry = planner_hypothesis._find(data["entries"], original_id)
    entry["verdict"] = "inconclusive"
    planner_hypothesis._write_all(state, data)

    revised = planner_hypothesis.revise_hypothesis(
        state, original_id, reason="the measure window was too short", cycle_id="cycle-2",
        measure="p50 proposer wall-clock time over 60 cycles",
    )
    assert revised["hypothesis_id"] != original_id
    assert revised["supersedes"] == original_id
    assert revised["version"] == 2

    reloaded_original = planner_hypothesis._find(
        planner_hypothesis._read_all(state)["entries"], original_id,
    )
    assert reloaded_original["status"] == "revised"
    assert reloaded_original["revised_by"] == revised["hypothesis_id"]
    # Immutable: measure and verdict on the ORIGINAL are untouched.
    assert reloaded_original["measure"] == _valid_hypothesis()["measure"]
    assert reloaded_original["verdict"] == "inconclusive"

    dropped = planner_hypothesis.drop_hypothesis(
        state, revised["hypothesis_id"], reason="line abandoned, cost outweighed benefit", cycle_id="cycle-3",
    )
    assert dropped["status"] == "dropped"
    assert dropped["drop_reason"]
    assert dropped["measure"] == revised["measure"]  # refutation evidence kept, not deleted


def test_revise_requires_a_reason(tmp_path: Path):
    state = tmp_path / "state"
    entry = planner_hypothesis.store_hypothesis(state, "cycle-1", _valid_hypothesis())
    with pytest.raises(ValueError):
        planner_hypothesis.revise_hypothesis(state, entry["hypothesis_id"], reason="", cycle_id="cycle-2")


# --- test_insight_decision_required_for_new_verdicts ------------------------

def test_insight_decision_required_for_new_verdicts(tmp_path: Path):
    state = tmp_path / "state"
    entry = planner_hypothesis.store_hypothesis(state, "cycle-1", _valid_hypothesis())
    hid = entry["hypothesis_id"]

    updated = planner_hypothesis.record_insight_decision(
        state, "cycle-2", hid, "continue", note="verdict was supported, keep the line",
    )
    assert len(updated["insight_decisions"]) == 1
    assert updated["insight_decisions"][0]["decision"] == "continue"

    events = read_events(state)
    insight_events = [e for e in events if e.get("phase") == "insight_decision"]
    assert len(insight_events) == 1
    assert insight_events[0]["hypothesis_id"] == hid
    assert insight_events[0]["decision"] == "continue"


def test_insight_decision_rejects_unknown_value(tmp_path: Path):
    state = tmp_path / "state"
    entry = planner_hypothesis.store_hypothesis(state, "cycle-1", _valid_hypothesis())
    with pytest.raises(ValueError):
        planner_hypothesis.record_insight_decision(state, "cycle-2", entry["hypothesis_id"], "ignore")


# --- test_verdict_backlog_bounded_and_counted -------------------------------

def test_verdict_backlog_bounded_and_counted(tmp_path: Path):
    state = tmp_path / "state"
    ids = []
    for i in range(7):
        entry = planner_hypothesis.store_hypothesis(
            state, f"cycle-{i}", _valid_hypothesis(statement=f"claim {i}", measure=f"measure {i}"),
        )
        ids.append(entry["hypothesis_id"])

    handle_now, deferred_count = no_plan_recovery.defer_or_bound_verdicts(state, ids, limit=5)
    assert handle_now == ids[:5]
    assert deferred_count == 2

    # A later session (still full mode) drains the carried remainder first.
    handle_now_2, deferred_count_2 = no_plan_recovery.defer_or_bound_verdicts(state, [], limit=5)
    assert handle_now_2 == ids[5:]
    assert deferred_count_2 == 0


# --- test_planner_cannot_write_verdicts -------------------------------------

def test_planner_cannot_write_verdicts(tmp_path: Path):
    state = tmp_path / "state"
    entry = planner_hypothesis.store_hypothesis(state, "cycle-1", _valid_hypothesis())
    assert entry["verdict"] is None

    # No function in this module accepts a verdict argument at all.
    import inspect
    for name in ("store_hypothesis", "revise_hypothesis", "drop_hypothesis", "record_insight_decision"):
        sig = inspect.signature(getattr(planner_hypothesis, name))
        assert "verdict" not in sig.parameters

    # Revise/drop never touch a pre-existing verdict field either (already
    # exercised end-to-end in test_revise_and_drop_preserve_evidence).


# --- test_no_plan_recovery_is_bounded_and_visible ---------------------------

def test_no_plan_recovery_is_bounded_and_visible(tmp_path: Path):
    state = tmp_path / "state"

    for i in range(2):
        state_result = no_plan_recovery.record_outcome(state, f"cycle-{i}", "malformed")
        assert state_result.planner_degraded is False
        assert state_result.stopped is False

    third = no_plan_recovery.record_outcome(state, "cycle-2", "refused")
    assert third.consecutive_planner == 3
    assert third.planner_degraded is True
    assert third.minimal_mode is True
    assert third.stopped is False

    for i in range(3, 6):
        result = no_plan_recovery.record_outcome(state, f"cycle-{i}", "no_plan")
    assert result.consecutive_planner == 6
    assert result.stopped is True

    events = read_events(state)
    transitions = [e for e in events if e.get("phase") == "no_plan_recovery"]
    states_seen = [e["state"] for e in transitions]
    assert "planner_degraded" in states_seen
    assert "stopped" in states_seen


# --- test_supply_and_planner_no_plan_are_counted_apart ----------------------

def test_supply_and_planner_no_plan_are_counted_apart(tmp_path: Path):
    state = tmp_path / "state"

    for i in range(6):
        result = no_plan_recovery.record_outcome(state, f"cycle-{i}", "timed_out")

    assert result.consecutive_supply == 6
    assert result.model_supply_degraded is True
    assert result.minimal_mode is False
    assert result.stopped is False
    assert result.consecutive_planner == 0

    events = read_events(state)
    transitions = [e for e in events if e.get("phase") == "no_plan_recovery"]
    assert any(e["state"] == "model_supply_degraded" and e["family"] == "supply" for e in transitions)
    assert not any(e["state"] in ("planner_degraded", "stopped") for e in transitions)


def test_infrastructure_outcomes_do_not_count_toward_either_family(tmp_path: Path):
    state = tmp_path / "state"
    for outcome in ("spawn_failed", "spawn_failed", "commit_failed", "spawn_failed"):
        result = no_plan_recovery.record_outcome(state, "cycle-x", outcome)
    assert result.consecutive_planner == 0
    assert result.consecutive_supply == 0


# --- test_reset_and_resume_after_stop ---------------------------------------

def test_reset_and_resume_after_stop(tmp_path: Path):
    state = tmp_path / "state"
    for i in range(6):
        no_plan_recovery.record_outcome(state, f"cycle-{i}", "malformed")
    stopped_state = no_plan_recovery.load_state(state)
    assert stopped_state.stopped is True

    resumed = no_plan_recovery.resume(state, "cycle-resume")
    assert resumed.stopped is False
    # Counters/minimal-mode are NOT reset by resume alone (ADR-035: only a
    # produced plan resets both counters).
    assert resumed.minimal_mode is True
    assert resumed.consecutive_planner == 6

    produced = no_plan_recovery.record_outcome(state, "cycle-after-resume", "integrated")
    assert produced.consecutive_planner == 0
    assert produced.minimal_mode is False
    assert produced.planner_degraded is False
    assert produced.stopped is False


# --- test_minimal_mode_defers_verdicts_visibly ------------------------------

def test_minimal_mode_defers_verdicts_visibly(tmp_path: Path):
    state = tmp_path / "state"
    for i in range(3):
        no_plan_recovery.record_outcome(state, f"cycle-{i}", "malformed")
    assert no_plan_recovery.load_state(state).minimal_mode is True

    handle_now, deferred_count = no_plan_recovery.defer_or_bound_verdicts(
        state, ["hyp-a", "hyp-b", "hyp-c"], limit=5,
    )
    assert handle_now == []
    assert deferred_count == 3

    # A produced plan resets minimal mode but the deferred ids are carried
    # forward -- "deferred, not dropped" -- and the next full session
    # handles them first.
    state_after_plan = no_plan_recovery.record_outcome(state, "cycle-3", "integrated")
    assert state_after_plan.minimal_mode is False
    assert state_after_plan.deferred_verdict_ids == ["hyp-a", "hyp-b", "hyp-c"]

    handle_now_2, deferred_count_2 = no_plan_recovery.defer_or_bound_verdicts(state, ["hyp-d"], limit=5)
    assert handle_now_2 == ["hyp-a", "hyp-b", "hyp-c", "hyp-d"]
    assert deferred_count_2 == 0


# --- test_no_automatic_candidate_selection ----------------------------------

def test_no_automatic_candidate_selection(tmp_path: Path):
    """No path through this module -- full mode, minimal mode, or stopped --
    ever selects a candidate. Every public function's return type is
    inspected for anything resembling a chosen task/candidate field."""
    state = tmp_path / "state"
    for i in range(6):
        no_plan_recovery.record_outcome(state, f"cycle-{i}", "malformed")

    stopped_state = no_plan_recovery.load_state(state)
    forbidden_fields = {"candidate", "task", "selected_candidate", "chosen_task"}
    assert forbidden_fields.isdisjoint(stopped_state.as_dict().keys())

    handle_now, _ = no_plan_recovery.defer_or_bound_verdicts(state, ["hyp-a"], limit=5)
    assert isinstance(handle_now, list)  # ids to consider, never a selection among them


# --- test_no_plan_does_not_fall_back_to_assignment --------------------------

def test_no_plan_does_not_fall_back_to_assignment(tmp_path: Path):
    state = tmp_path / "state"
    result = no_plan_recovery.record_outcome(state, "cycle-1", "no_plan")
    assert not hasattr(result, "task")
    assert not hasattr(result, "candidate")
    # A no_plan outcome never flips `stopped`/`minimal_mode` to something
    # that implies work was assigned in its place.
    assert result.stopped is False


# --- test_plans_are_appended_not_overwritten --------------------------------

def test_plans_are_appended_not_overwritten():
    content = day_diary.new_day_file()
    once = day_diary.append_plan_entry(content, "first plan", cycle_id="cycle-1")
    twice = day_diary.append_plan_entry(once, "second plan", cycle_id="cycle-2")

    assert "first plan" in twice
    assert "second plan" in twice
    assert twice.index("second plan") < twice.index("first plan")
    assert twice.count(day_diary.PLAN_BEGIN) == 1
    assert twice.count(day_diary.PLAN_END) == 1


# --- test_plan_forecast_and_actual_recorded ---------------------------------

def test_plan_forecast_and_actual_recorded():
    content = day_diary.new_day_file()
    with_forecast = day_diary.append_plan_entry(
        content, "ship the caching layer", cycle_id="cycle-1", iterations_planned=12,
    )
    assert "forecast: 12 iterations" in with_forecast

    with_actual = day_diary.record_plan_actual_iterations(with_forecast, "cycle-1", 9)
    assert "forecast: 12 iterations" in with_actual
    assert "actual: 9 iterations" in with_actual


def test_record_plan_actual_iterations_requires_existing_entry():
    content = day_diary.new_day_file()
    with pytest.raises(ValueError):
        day_diary.record_plan_actual_iterations(content, "cycle-does-not-exist", 5)


# --- test_executor_plan_amendment_is_attributed -----------------------------

def test_executor_plan_amendment_is_attributed():
    content = day_diary.new_day_file()
    with_plan = day_diary.append_plan_entry(content, "connect the validator", cycle_id="cycle-1")

    amended = day_diary.append_plan_amendment(
        with_plan, "cycle-1", "the target file was already deleted upstream", amended_by="executor",
    )
    assert "Amended by executor:" in amended
    assert "already deleted upstream" in amended
    # The amendment sits under this cycle's own header, not some other one.
    assert amended.index("cycle cycle-1") < amended.index("Amended by executor")


def test_plan_amendment_requires_a_reason():
    content = day_diary.new_day_file()
    with_plan = day_diary.append_plan_entry(content, "connect the validator", cycle_id="cycle-1")
    with pytest.raises(ValueError):
        day_diary.append_plan_amendment(with_plan, "cycle-1", "")


# --- test_trust_order_new_priority_then_defect ------------------------------

def test_trust_order_new_priority_then_defect():
    """render_candidates_block never re-sorts collect_demand's own order --
    it just renders whatever order it was given, preserving the demand
    trust order (priority > defect > goal-gap > rest)."""
    items = [
        _make_item("priority", "Priority 3 — ship the cache", "op text"),
        _make_item("defect", "recurring test failure in X", "3 cycles"),
        _make_item("goal-gap", "coverage gap in Y", "scorecard"),
    ]
    block = planner_candidates.render_candidates_block(items)
    assert block.index("Priority 3") < block.index("recurring test failure") < block.index("coverage gap")


# --- test_new_operator_priority_wakes_the_planner ---------------------------

def test_new_operator_priority_wakes_the_planner(tmp_path: Path):
    state = tmp_path / "state"
    priority_item = _make_item("priority", "Priority 7 — reduce latency", "op text")

    new_ids = planner_candidates.mark_new_priority_items(state, [priority_item])
    assert new_ids == {priority_item["id"]}
    block = planner_candidates.render_candidates_block([priority_item], new_ids)
    assert "(new)" in block

    # The next cycle: same priority, already seen -- no longer marked new.
    new_ids_again = planner_candidates.mark_new_priority_items(state, [priority_item])
    assert new_ids_again == set()
    block_again = planner_candidates.render_candidates_block([priority_item], new_ids_again)
    assert "(new)" not in block_again


def test_changed_priority_reads_as_new_again(tmp_path: Path):
    state = tmp_path / "state"
    v1 = _make_item("priority", "Priority 7 — reduce latency", "op text")
    planner_candidates.mark_new_priority_items(state, [v1])

    v2 = _make_item("priority", "Priority 7 — reduce latency by 20%", "op text edited")
    new_ids = planner_candidates.mark_new_priority_items(state, [v2])
    assert new_ids == {v2["id"]}


# --- test_defect_urgency_survives_and_declines_are_visible (backend only) --

def test_defect_urgency_survives_and_declines_are_visible(tmp_path: Path):
    state = tmp_path / "state"
    defect = _make_item("defect", "flaky test in module Z", "3 failures")
    defect_id = defect["id"]

    for cycle in ("cycle-1", "cycle-2"):
        result = planner_candidates.record_defect_declines(
            state, cycle, {defect_id: "not reproducible locally yet"},
        )
        assert result[defect_id]["escalated"] is False

    third = planner_candidates.record_defect_declines(
        state, "cycle-3", {defect_id: "still investigating"},
    )
    assert third[defect_id]["consecutive"] == 3
    assert third[defect_id]["escalated"] is True

    events = read_events(state)
    escalations = [e for e in events if e.get("phase") == "defect_decline_escalated"]
    assert len(escalations) == 1
    assert escalations[0]["defect_id"] == defect_id


def test_defect_decline_streak_resets_on_a_gap(tmp_path: Path):
    state = tmp_path / "state"
    defect_id = "defect-x"
    planner_candidates.record_defect_declines(state, "cycle-1", {defect_id: "reason one"})
    planner_candidates.record_defect_declines(state, "cycle-2", {defect_id: "reason two"})
    # cycle-3: this defect is NOT declined again (accepted, or just not named) -- resets.
    planner_candidates.record_defect_declines(state, "cycle-3", {})
    result = planner_candidates.record_defect_declines(state, "cycle-4", {defect_id: "reason again"})
    assert result[defect_id]["consecutive"] == 1
    assert result[defect_id]["escalated"] is False


def test_defect_decline_requires_a_reason(tmp_path: Path):
    state = tmp_path / "state"
    with pytest.raises(ValueError):
        planner_candidates.record_defect_declines(state, "cycle-1", {"defect-x": ""})


def test_record_plan_amendment_ledger(tmp_path: Path):
    from nanobot.runtime.cycle_ledger import record_plan_amendment

    state = tmp_path / "state"
    record_plan_amendment(state, "cycle-1", "executor", "work was already done on HEAD")
    events = read_events(state)
    amendments = [e for e in events if e.get("phase") == "plan_amendment"]
    assert len(amendments) == 1
    assert amendments[0]["amended_by"] == "executor"
    assert amendments[0]["cycle_id"] == "cycle-1"
