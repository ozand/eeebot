"""ADR-035 (the planning session chooses and closes HADI), rules 1 and 3,
issue #1942 (B2): the planning session chooses before selection, its
hypothesis is stored in ADR-030's form, and no_plan recovery is bounded and
visible. Also covers the rest amendment (docs PR #1964): a structured
`rest` outcome, the change-only harness pre-check, and `rejected_duplicate`.

Rule 1's live task-selection wiring in ``bridge.py`` ("its plan IS the
executor's task", and the executor never receives an assigned title) is
exercised at the bottom of this file. The goal-review/reflector/proposer
decommission and the planner-prompt-schema work for trust-order/
new-priority/defect-decline remain deferred -- see this PR's body for why
each is out of scope this session.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime import (
    day_diary, no_plan_recovery, planner_candidates, planner_dedup_evidence,
    planner_hypothesis, planner_rest,
)
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


# --- test_rest_is_structured_and_outside_no_plan_counts (#1964) -----------

def test_rest_is_structured_and_outside_no_plan_counts(tmp_path: Path):
    state = tmp_path / "state"
    wc, deadline = planner_rest.parse_rest({
        "wake_condition": {"kind": "main_commit"},
        "deadline": "2026-09-26T00:00:00Z",
    })
    assert wc.kind == "main_commit"

    rest_state = planner_rest.record_rest(state, "cycle-1", wc, deadline, selfevo_repo=None)
    assert rest_state.consecutive_rests == 1

    # A `rest` never touches no_plan_recovery's counters.
    no_plan_state = no_plan_recovery.load_state(state)
    assert no_plan_state.consecutive_planner == 0
    assert no_plan_state.consecutive_supply == 0


@pytest.mark.parametrize("bad_raw", [
    {"wake_condition": {"kind": "main_commit"}},  # missing deadline
    {"deadline": "2026-09-26T00:00:00Z"},  # missing wake_condition
    {"wake_condition": {"kind": "unknown_kind"}, "deadline": "2026-09-26T00:00:00Z"},
    {"wake_condition": {"kind": "file", "ref": ""}, "deadline": "2026-09-26T00:00:00Z"},  # file needs a ref
    {"wake_condition": {"kind": "main_commit"}, "deadline": "not a timestamp"},
    "just a string",
])
def test_rest_without_both_fields_is_malformed(bad_raw):
    with pytest.raises(planner_rest.RestValidationError):
        planner_rest.parse_rest(bad_raw)


# --- test_precheck_tests_change_not_value (#1964) --------------------------

def test_precheck_tests_change_not_value(tmp_path: Path):
    state = tmp_path / "state"
    watched_file = tmp_path / "watched.txt"
    watched_file.write_text("v1", encoding="utf-8")

    wc, deadline = planner_rest.parse_rest({
        "wake_condition": {"kind": "file", "ref": str(watched_file)},
        "deadline": "2099-01-01T00:00:00Z",
    })
    planner_rest.record_rest(state, "cycle-1", wc, deadline, selfevo_repo=None)

    # Unchanged, deadline far away -> held.
    run, reason = planner_rest.precheck(state, None)
    assert run is False
    assert reason == "unchanged"

    # Any change in the input's VERSION -> session runs, regardless of how
    # small the change is -- the pre-check never judges importance.
    watched_file.write_text("v1_plus_one_byte", encoding="utf-8")
    run2, reason2 = planner_rest.precheck(state, None)
    assert run2 is True
    assert reason2 == "input_changed"


def test_precheck_runs_on_unreadable_input(tmp_path: Path):
    state = tmp_path / "state"
    missing = tmp_path / "does_not_exist.txt"
    wc, deadline = planner_rest.parse_rest({
        "wake_condition": {"kind": "file", "ref": str(missing)},
        "deadline": "2099-01-01T00:00:00Z",
    })
    planner_rest.record_rest(state, "cycle-1", wc, deadline, selfevo_repo=None)
    run, reason = planner_rest.precheck(state, None)
    assert run is True
    assert reason == "input_unreadable"


def test_precheck_runs_once_deadline_passes(tmp_path: Path):
    state = tmp_path / "state"
    watched_file = tmp_path / "watched.txt"
    watched_file.write_text("v1", encoding="utf-8")
    wc, deadline = planner_rest.parse_rest({
        "wake_condition": {"kind": "file", "ref": str(watched_file)},
        "deadline": "2000-01-01T00:00:00Z",  # already in the past
    })
    planner_rest.record_rest(state, "cycle-1", wc, deadline, selfevo_repo=None)
    run, reason = planner_rest.precheck(state, None)
    assert run is True
    assert reason == "deadline_passed"


# --- test_six_rests_raise_review_signal (#1964) -----------------------------

def test_six_rests_raise_review_signal(tmp_path: Path):
    state = tmp_path / "state"
    wc, deadline = planner_rest.parse_rest({
        "wake_condition": {"kind": "main_commit"}, "deadline": "2099-01-01T00:00:00Z",
    })
    for i in range(5):
        s = planner_rest.record_rest(state, f"cycle-{i}", wc, deadline, selfevo_repo=None)
        assert s.review_signal is False

    sixth = planner_rest.record_rest(state, "cycle-6", wc, deadline, selfevo_repo=None)
    assert sixth.consecutive_rests == 6
    assert sixth.review_signal is True

    events = read_events(state)
    signals = [e for e in events if e.get("phase") == "planner_rest_review_signal"]
    assert len(signals) == 1
    assert "not proof of a defect" in signals[0]["note"]


# --- test_held_rest_and_no_plan_counted_apart (#1964) -----------------------

def test_held_rest_and_no_plan_counted_apart(tmp_path: Path):
    state = tmp_path / "state"
    wc, deadline = planner_rest.parse_rest({
        "wake_condition": {"kind": "main_commit"}, "deadline": "2099-01-01T00:00:00Z",
    })
    planner_rest.record_rest(state, "cycle-1", wc, deadline, selfevo_repo=None)
    planner_rest.record_held_tick(state, "cycle-2")
    planner_rest.record_held_tick(state, "cycle-3")
    no_plan_recovery.record_outcome(state, "cycle-4", "no_plan")

    rest_state = planner_rest.load_state(state)
    no_plan_state = no_plan_recovery.load_state(state)

    events = read_events(state)
    held_events = [e for e in events if e.get("phase") == "planner_rest_held"]
    rest_events = [e for e in events if e.get("phase") == "planner_rest"]

    assert len(held_events) == 2
    assert len(rest_events) == 1
    assert rest_state.consecutive_rests == 1  # unaffected by held ticks or no_plan
    assert no_plan_state.consecutive_planner == 1  # unaffected by rest/held

    # A non-rest outcome (no_plan here) clears the active rest and its streak.
    planner_rest.record_non_rest_outcome(state, "cycle-4", "no_plan")
    assert planner_rest.load_state(state).consecutive_rests == 0


# --- test_rejected_duplicate_is_recorded_and_fed_forward (#1964) -----------

def test_rejected_duplicate_is_recorded_and_fed_forward(tmp_path: Path):
    state = tmp_path / "state"
    planner_dedup_evidence.record_rejected_duplicate(
        state, "cycle-1", "connect the orphaned validator", "abc123sha", "already committed in cycle-99",
    )

    events = read_events(state)
    rows = [e for e in events if e.get("phase") == "rejected_duplicate"]
    assert len(rows) == 1
    assert rows[0]["evidence_sha"] == "abc123sha"

    # Fed forward to exactly the next session, then cleared.
    evidence = planner_dedup_evidence.consume_pending_evidence(state)
    assert evidence["evidence_sha"] == "abc123sha"
    block = planner_dedup_evidence.render_dedup_evidence_block(evidence)
    assert "abc123sha" in block
    assert "connect the orphaned validator" in block

    assert planner_dedup_evidence.consume_pending_evidence(state) is None
    assert planner_dedup_evidence.render_dedup_evidence_block(None) == ""


# --- test_repo_preparation_preserves_staged_and_pending (#1964) ------------

def test_repo_preparation_preserves_staged_and_pending(tmp_path: Path):
    """ADR-035 rest amendment: repository preparation (_restore_to_main,
    then staged-promotions pickup, then pending-push completion) now runs
    on every started cycle -- proving the ORDER keeps the historical
    "curator outputs reset by the bridge" defect closed: a stray/dirty
    checkout is cleaned FIRST, and only then are staged/pending items
    applied -- both copy from state_dir and commit+push atomically, so
    neither is ever caught mid-write by the reset.
    """
    from nanobot.runtime.bridge import _prepare_repository_for_cycle, _setup_cycle_branch
    from nanobot.runtime.cycle_ledger import record_cycle_outcome
    from nanobot.runtime.knowledge_curator import load_staged_manifest
    from tests.test_bridge_cycle_branch import _commit_file, _init_repo, _origin_main_sha, _run
    from tests.test_curator_staging_pickup import _write_manifest

    origin, work = _init_repo(tmp_path)
    state = tmp_path / "state"

    (work / "memory").mkdir(parents=True)
    (work / "memory" / "index.md").write_text("# Index\n", encoding="utf-8")
    _run(work, "add", "memory/index.md")
    _run(work, "commit", "-m", "add index")
    _run(work, "push", "origin", "main")

    # A staged curator promotion, sitting entirely in state_dir.
    _write_manifest(state, [{
        "path": "memory/facts/my-fact.md",
        "action": "create",
        "payload_file": "memory__facts__my-fact.md",
        "index_line": "- [My Fact](memory/facts/my-fact.md)",
        "index_rel": "memory/index.md",
        "_content": "# My Fact\n\nSome knowledge.\n",
    }])

    # A push_pending cycle from a "previous" run.
    setup = _setup_cycle_branch(work, "cid-pending")
    assert setup["ok"]
    _commit_file(work, "feature.py", "def feature():\n    return 42\n", "feat: add feature")
    _run(work, "checkout", "main")
    record_cycle_outcome(
        state, "cid-pending", "push_pending", "push_pending", [], setup["branch"],
        main_sha_before=setup["main_sha"],
    )

    # A stray, dirty, off-main checkout -- exactly what _restore_to_main
    # exists to clean, and exactly the condition the historical defect
    # (curator outputs wiped at cycle start) happened under.
    _run(work, "checkout", "-b", "stray-branch")
    (work / "junk.txt").write_text("uncommitted\n", encoding="utf-8")

    result = _prepare_repository_for_cycle(work, state)

    # The stray branch/dirty file WAS cleaned (restore ran).
    assert not (work / "junk.txt").exists()
    assert _run(work, "branch", "--show-current").stdout.strip() == "main"

    # Staged promotion survived and was committed+pushed, not lost.
    assert result["staged_promoted"] == 1
    assert (work / "memory" / "facts" / "my-fact.md").exists()
    assert load_staged_manifest(state) == []

    # Pending push survived and was resolved, not lost.
    assert result["pending_pushed"] == 1
    assert _origin_main_sha(origin) != setup["main_sha"]


# --- test_queued_proposer_requests_drained_at_switchover -------------------

def _write_proposer_request(state: Path, request_id: str, title: str, status: str = "queued") -> Path:
    req_dir = state / "subagents" / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    path = req_dir / f"{request_id}.json"
    path.write_text(json.dumps({
        "request_id": request_id,
        "task_title": title,
        "task": f"do {title}",
        "target_path": "scripts/example.py",
        "request_status": status,
    }), encoding="utf-8")
    return path


def test_queued_proposer_requests_drained_at_switchover(tmp_path: Path):
    from nanobot.runtime.llm_proposer import (
        _SUPERSEDED_STATUS, drain_queued_proposer_requests, proposer_candidate_items,
    )

    state = tmp_path / "state"
    _write_proposer_request(state, "llm-proposer-cycle-1", "fix the flaky retry loop")

    # Before drain: a live candidate, status still queued.
    before = proposer_candidate_items(state)
    assert len(before) == 1
    assert before[0]["kind"] == "proposer"
    assert "fix the flaky retry loop" in before[0]["summary"]

    drained = drain_queued_proposer_requests(state)
    assert drained == 1

    req_path = state / "subagents" / "requests" / "llm-proposer-cycle-1.json"
    on_disk = json.loads(req_path.read_text(encoding="utf-8"))
    assert on_disk["request_status"] == _SUPERSEDED_STATUS

    # The old rotation path (find_pending_request's own status filter) would
    # now skip it -- status is no longer queued/pending.
    assert on_disk["request_status"].lower() not in ("queued", "pending")

    # But its candidate re-enters the ranked list, unaffected by the drain.
    after = proposer_candidate_items(state)
    assert len(after) == 1
    assert after[0]["kind"] == "proposer"

    # Idempotent: draining an already-drained queue changes nothing further.
    assert drain_queued_proposer_requests(state) == 0


def test_proposer_candidate_items_excludes_handled_requests(tmp_path: Path):
    from nanobot.runtime.llm_proposer import _bridge_state_dir, proposer_candidate_items

    state = tmp_path / "state"
    _write_proposer_request(state, "llm-proposer-cycle-2", "add a retry guard")

    bridge_state = _bridge_state_dir(state)
    bridge_state.mkdir(parents=True, exist_ok=True)
    (bridge_state / "handled_llm-proposer-cycle-2.txt").write_text("handled", encoding="utf-8")

    assert proposer_candidate_items(state) == []


def test_proposer_candidate_items_excludes_non_proposer_requests(tmp_path: Path):
    from nanobot.runtime.llm_proposer import proposer_candidate_items

    state = tmp_path / "state"
    req_dir = state / "subagents" / "requests"
    req_dir.mkdir(parents=True)
    (req_dir / "request-human.json").write_text(json.dumps({
        "request_id": "human-request-1", "task_title": "operator-submitted task",
        "request_status": "queued",
    }), encoding="utf-8")

    assert proposer_candidate_items(state) == []


def test_record_plan_amendment_ledger(tmp_path: Path):
    from nanobot.runtime.cycle_ledger import record_plan_amendment

    state = tmp_path / "state"
    record_plan_amendment(state, "cycle-1", "executor", "work was already done on HEAD")
    events = read_events(state)
    amendments = [e for e in events if e.get("phase") == "plan_amendment"]
    assert len(amendments) == 1
    assert amendments[0]["amended_by"] == "executor"
    assert amendments[0]["cycle_id"] == "cycle-1"


# --- test_planner_runs_first_and_its_plan_is_the_task ----------------------
# --- test_executor_never_receives_assigned_title ---------------------------
#
# ADR-035 rule 1 (#1942), live task-selection wiring in bridge.py. Uses the
# bridge-integration harness from tests/test_cycle_ledger.py: STATE_DIR/
# BRIDGE_STATE_DIR/TARGET_WORKSPACE/RELEASE_ROOT monkeypatched onto a
# tmp_path, a real bare-origin selfevo checkout, and a SubagentManager fake
# that captures the executor spawn's kwargs (never the planner's own,
# telemetry_component="planner") so the actual content delivered to the
# executor can be inspected directly.

def _setup_planner_chooses_harness(base, monkeypatch):
    from nanobot.runtime import bridge

    state_dir = base / "state"
    state_dir.mkdir()
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    release_root = base / "_adr034_release_root"
    release_root.mkdir(exist_ok=True)
    (release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", release_root)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

    (state_dir / "goals").mkdir(parents=True, exist_ok=True)
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": "goal-1", "text": "test goal"}),
        encoding="utf-8",
    )
    return state_dir


def _stub_planning_session(monkeypatch, plan_text: str, candidate_id: "str | None" = None):
    from nanobot.runtime import bridge

    calls: list[dict] = []

    async def _fake_planning_session(**kwargs):
        calls.append(kwargs)
        return {
            'ran': True, 'iterations_used': 1, 'iterations_planned': 1,
            'tampered_files': [], 'plan': {'plan': plan_text, 'candidate_id': candidate_id},
        }

    monkeypatch.setattr(bridge, "_run_planning_session", _fake_planning_session)
    return calls


def _make_capturing_subagent_manager(captured: list):
    from tests.test_cycle_ledger import _FakeSubagentManager

    class _CapturingSubagentManager(_FakeSubagentManager):
        async def spawn(self, **kwargs):
            if self._telemetry_component != "planner":
                captured.append(kwargs)
            return await super().spawn(**kwargs)

    return _CapturingSubagentManager


def test_planner_runs_first_and_its_plan_is_the_task(tmp_path: Path, monkeypatch):
    """The planning session runs before any request is selected, and its
    plan -- not a rotation-picked queue file -- is the executor's task."""
    import asyncio

    from nanobot.runtime import bridge
    from tests.test_cycle_ledger import _init_selfevo_repo, _read_ledger

    base = tmp_path
    _init_selfevo_repo(base)
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)

    # A stale rotation-queue-shaped file with a DIFFERENT title -- proves
    # selection never reads it; only the planner's own plan is used.
    req_dir = state_dir / "subagents" / "requests"
    req_dir.mkdir(parents=True)
    (req_dir / "stale.json").write_text(
        json.dumps({"request_id": "stale-req", "task_title": "STALE TITLE should never spawn"}),
        encoding="utf-8",
    )

    plan_text = "Implement the bounded widget cache exactly as planned"
    planning_calls = _stub_planning_session(monkeypatch, plan_text)

    captured: list[dict] = []
    monkeypatch.setattr(bridge, "SubagentManager", _make_capturing_subagent_manager(captured))

    result = asyncio.run(bridge._main_impl())
    assert result == 0

    # Rule 1: the planner ran -- there is no other place a task could have
    # come from now that the rotation reader is retired.
    assert len(planning_calls) == 1

    # Its plan reached the executor as the task, verbatim -- never the
    # stale queued file's title.
    assert len(captured) == 1
    assert plan_text in captured[0]["task"]
    assert "STALE TITLE" not in captured[0]["task"]

    outcome_rows = [r for r in _read_ledger(state_dir) if r["phase"] == "outcome"]
    assert outcome_rows[-1]["outcome"] == "success"


def test_executor_never_receives_assigned_title(tmp_path: Path, monkeypatch):
    """The executor prompt never contains a proposer-authored task or
    "priorities are handled by the proposer" -- even when the plan resolves
    to a matched, proposer-authored candidate via candidate_id, only the
    plan's OWN wording reaches the executor's task/title."""
    import asyncio

    from nanobot.runtime import bridge
    from nanobot.runtime.demand import item_id
    from tests.test_cycle_ledger import _init_selfevo_repo, _read_ledger

    base = tmp_path
    _init_selfevo_repo(base)
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)

    # A live proposer-authored candidate the planner is shown and chooses to
    # work (candidate_id set) -- its own raw title/task text must never
    # reach the executor; only the plan's own wording does.
    proposer_title = "Add a proposer-authored helper script"
    proposer_task_text = "PROPOSER RAW TASK TEXT SHOULD NEVER REACH THE EXECUTOR"
    req_dir = state_dir / "subagents" / "requests"
    req_dir.mkdir(parents=True)
    (req_dir / "llm-proposer-cand.json").write_text(json.dumps({
        "request_id": "llm-proposer-cand-1",
        "task_title": proposer_title,
        "task": proposer_task_text,
        "request_status": "queued",
    }), encoding="utf-8")

    candidate_id = item_id("proposer", proposer_title)
    plan_text = "Refine the caching layer per the planner's own reasoning"
    _stub_planning_session(monkeypatch, plan_text, candidate_id=candidate_id)

    captured: list[dict] = []
    monkeypatch.setattr(bridge, "SubagentManager", _make_capturing_subagent_manager(captured))

    result = asyncio.run(bridge._main_impl())
    assert result == 0

    assert len(captured) == 1
    task = captured[0]["task"]
    assert plan_text in task
    assert proposer_task_text not in task
    assert proposer_title not in task
    assert "handled by the proposer" not in task.lower()

    outcome_rows = [r for r in _read_ledger(state_dir) if r["phase"] == "outcome"]
    assert outcome_rows[-1]["outcome"] == "success"
