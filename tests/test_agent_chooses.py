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


def _stub_planning_session(
    monkeypatch, plan_text: str, candidate_id: "str | None" = None,
    *, resume_branch: "str | None" = None, resume_cycle_id: "str | None" = None,
    resume_skip_opening_entry: bool = False,
):
    from nanobot.runtime import bridge

    calls: list[dict] = []

    async def _fake_planning_session(**kwargs):
        calls.append(kwargs)
        return {
            'ran': True, 'iterations_used': 1, 'iterations_planned': 1,
            'tampered_files': [], 'plan': {'plan': plan_text, 'candidate_id': candidate_id},
            # ADR-035 keep-work (#1942 B2): non-None only when the test
            # simulates a `keep` decision on a pending open increment.
            'resume_branch': resume_branch,
            'resume_cycle_id': resume_cycle_id,
            'resume_skip_opening_entry': resume_skip_opening_entry,
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


# --- test_supply_interruption_carries_open_increment -----------------------
# --- test_supply_hold_uses_rest_snapshot_with_backoff ----------------------
#
# Architect resolution 2026-09-25 (ADR-035 rule 3 / ADR-031 rule 2): a plan
# interrupted by a supplier-side executor failure is not retried or
# requeued -- it becomes the pending open increment the next planning
# session must resolve (keep/edit/delete), and holds further sessions on a
# doubling backoff (15 -> 30 -> 60 minutes, capped), reset by any model call
# that completes without a supply classification.

def test_supply_interruption_carries_open_increment(tmp_path: Path):
    from nanobot.runtime import open_increment
    from nanobot.runtime.planner_rest import WakeCondition

    state = tmp_path / "state"
    state.mkdir()
    # A readable, stable input to snapshot -- main_commit needs a real repo,
    # which this test has no reason to set up.
    marker = state / "supply_marker.txt"
    marker.write_text("unchanged", encoding="utf-8")
    open_increment.record_supply_interruption(
        state, "cycle-1",
        retry_key="self-abc123", plan_text="Refine the caching layer", candidate_id=None,
        wake_condition=WakeCondition(kind="file", ref=str(marker)),
    )

    pending = open_increment.pending_open_increment(state)
    assert pending is not None
    assert pending["reason"] == "interrupted_supply"
    assert pending["retry_key"] == "self-abc123"
    assert pending["plan_text"] == "Refine the caching layer"

    # Held immediately after interruption -- the deadline is in the future.
    run, reason = open_increment.precheck(state, None)
    assert run is False
    assert reason == "unchanged"

    # The next session resolves it (keep/edit/delete) -- clears pending and
    # the hold, resets the streak.
    open_increment.resolve(state, "cycle-2", "keep")
    assert open_increment.pending_open_increment(state) is None
    run, reason = open_increment.precheck(state, None)
    assert run is True
    assert reason == "no_active_hold"

    events = read_events(state)
    phases = [e["phase"] for e in events]
    assert "open_increment" in phases
    assert "open_increment_resolved" in phases
    resolved = [e for e in events if e["phase"] == "open_increment_resolved"][0]
    assert resolved["decision"] == "keep"


def test_supply_hold_uses_rest_snapshot_with_backoff(tmp_path: Path, monkeypatch):
    from datetime import datetime, timezone

    from nanobot.runtime import open_increment

    state = tmp_path / "state"

    # Doubling, capped: 1st/2nd/3rd/4th consecutive interruption of the
    # SAME retry_key, small base/cap so the test runs on real wall-clock
    # without waiting minutes.
    expected_seconds = [2, 4, 8, 8]  # base=2, cap=8 -> 2, 4, 8, 8 (capped)
    for expected in expected_seconds:
        state_obj = open_increment.record_supply_interruption(
            state, "cycle-x",
            retry_key="self-same", plan_text="same increment", candidate_id=None,
            cooldown_base_seconds=2, cooldown_cap_seconds=8,
        )
        deadline = datetime.strptime(state_obj.hold["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delta = (deadline - datetime.now(timezone.utc)).total_seconds()
        assert abs(delta - expected) <= 2, (expected, delta)

    assert open_increment.load_state(state).consecutive_supply_interrupts == 4

    # A completed model call (no supply classification) resets the streak.
    open_increment.record_model_call_completed(state)
    assert open_increment.load_state(state).consecutive_supply_interrupts == 0

    # The NEXT interruption of the same retry_key starts the backoff over.
    state_obj = open_increment.record_supply_interruption(
        state, "cycle-y",
        retry_key="self-same", plan_text="same increment", candidate_id=None,
        cooldown_base_seconds=2, cooldown_cap_seconds=8,
    )
    deadline = datetime.strptime(state_obj.hold["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = (deadline - datetime.now(timezone.utc)).total_seconds()
    assert abs(delta - 2) <= 2

    # Three consecutive interruptions raise the non-auto-clearing operator
    # flag; record_model_call_completed above did NOT touch it (only the
    # streak), and resolve() never clears it either -- only
    # operator_clear_flag does.
    open_increment.resolve(state, "cycle-y", "delete")
    assert open_increment.load_state(state).operator_flagged is True
    open_increment.operator_clear_flag(state, "cycle-z")
    assert open_increment.load_state(state).operator_flagged is False

    # The rendered block itself: the open increment's plan text is first,
    # and the session is told to decide keep/edit/delete before anything
    # else -- what the planning session's prompt actually surfaces.
    pending = {"plan_text": "Refine the caching layer", "interrupted_at": "2026-09-25T00:00:00Z"}
    block = open_increment.render_open_increment_block(pending, 2)
    assert block.startswith("## Open increment")
    assert "Refine the caching layer" in block
    assert "before planning anything else" in block
    assert "open_increment_decision" in block
    assert open_increment.render_open_increment_block(None, 0) == ""

    # Integration: before the deadline the bridge holds -- no session, no
    # repository preparation, no provider call at all. After the deadline
    # a session runs and the open increment is what pending_open_increment
    # (the exact input _run_planning_session renders first) still returns.
    import asyncio

    from nanobot.runtime import bridge
    from tests.test_cycle_ledger import _init_selfevo_repo

    base = tmp_path / "integration"
    base.mkdir()
    integration_state = _setup_planner_chooses_harness(base, monkeypatch)
    _init_selfevo_repo(base)

    repo_prep_calls: list[int] = []
    real_prep = bridge._prepare_repository_for_cycle

    def _tracking_prep(*a, **k):
        repo_prep_calls.append(1)
        return real_prep(*a, **k)

    monkeypatch.setattr(bridge, "_prepare_repository_for_cycle", _tracking_prep)
    provider_calls: list[int] = []

    def _tracking_provider(_config):
        provider_calls.append(1)
        return object()

    monkeypatch.setattr(bridge, "_make_provider", _tracking_provider)
    proposer_calls: list[int] = []
    real_maybe_propose = bridge.llm_proposer.maybe_propose

    def _tracking_maybe_propose(*a, **k):
        proposer_calls.append(1)
        return real_maybe_propose(*a, **k)

    monkeypatch.setattr(bridge.llm_proposer, "maybe_propose", _tracking_maybe_propose)

    open_increment.record_supply_interruption(
        integration_state, "cycle-prior",
        retry_key="self-integration", plan_text="Refine the caching layer", candidate_id=None,
        # Same selfevo_repo the bridge itself will snapshot at precheck
        # time -- otherwise the main_commit wake condition reads as
        # "changed" (None at record time vs a real sha at precheck time)
        # and the hold never actually engages.
        selfevo_repo=base / "eeebot-self-evolving",
        cooldown_base_seconds=9999, cooldown_cap_seconds=9999,
    )
    planning_calls = _stub_planning_session(monkeypatch, "irrelevant -- session must not run while held")
    monkeypatch.setattr(bridge, "SubagentManager", _make_capturing_subagent_manager([]))

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0
    assert repo_prep_calls == []
    assert provider_calls == []
    assert planning_calls == []
    assert proposer_calls == []

    # Force the deadline into the past (simulates cooldown elapsed).
    oi_state = open_increment.load_state(integration_state)
    oi_state.hold["deadline"] = "2000-01-01T00:00:00Z"
    open_increment._save_state(integration_state, oi_state)

    # The pending open increment is still there -- exactly what
    # _run_planning_session's own pending_open_increment() call would see
    # and render first, right as this next session starts.
    assert open_increment.pending_open_increment(integration_state) is not None

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0
    assert len(repo_prep_calls) == 1
    assert len(provider_calls) == 1
    assert len(planning_calls) == 1


# --- keep-work: a kept open_increment resumes its branch without reset
# (ADR-035, #1942 B2) ----------------------------------------------------------

def test_kept_increment_resumes_branch_without_reset(tmp_path: Path, monkeypatch):
    """"A retry resumes the branch. When the next session keeps an
    interrupted open_increment, the executor continues on the existing
    cycle branch with its checkpoints; the branch is never reset to main
    for a kept increment." (ADR-035 keep-work). Drives a real
    `bridge._main_impl()` run with `_run_planning_session` stubbed to
    simulate an already-resolved `keep` decision (`resume_branch`/
    `resume_cycle_id` set, mirroring what a real planning session's
    `open_increment_decision: "keep"` produces) and asserts the resumed
    branch's git history still contains the interrupted attempt's
    checkpoint commit -- a `_setup_cycle_branch` that fell back to its
    ordinary `-B origin/main` reset would have thrown it away.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _FakeSubagentManager, _init_selfevo_repo, _run

    from nanobot.runtime import bridge, open_increment
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _origin, work = _init_selfevo_repo(base)

    old_cycle_id = "cycle-interrupted-01"
    old_branch = f"selfevo/cycle-{old_cycle_id}"
    _run(work, "checkout", "-b", old_branch)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "wip.py").write_text("def wip():\n    return 'partial'\n", encoding="utf-8")
    _run(work, "add", "scripts/wip.py")
    commit = subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "selfevo: checkpoint — scripts/wip.py", "-m", CHECKPOINT_TRAILER],
        capture_output=True, text=True,
    )
    assert commit.returncode == 0, commit.stderr
    checkpoint_sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    _run(work, "checkout", "main")

    open_increment.record_supply_interruption(
        state_dir, old_cycle_id,
        retry_key="self-resume-test", plan_text="finish the wip feature", candidate_id=None,
        selfevo_repo=work, branch=old_branch,
    )
    # Force the hold's deadline into the past -- this test drives the
    # session that runs AFTER the cooldown elapses (the `keep` decision
    # itself), not the held tick (already covered by
    # test_supply_hold_uses_rest_snapshot_with_backoff).
    _oi_state = open_increment.load_state(state_dir)
    _oi_state.hold["deadline"] = "2000-01-01T00:00:00Z"
    open_increment._save_state(state_dir, _oi_state)

    planning_calls = _stub_planning_session(
        monkeypatch, "finish the wip feature",
        resume_branch=old_branch, resume_cycle_id=old_cycle_id, resume_skip_opening_entry=True,
    )
    monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0
    assert len(planning_calls) == 1

    # The cycle branch integrates and is deleted on success -- checking it
    # by name after the run is not meaningful. Instead: the checkpoint
    # commit must be an ancestor of the now-updated `main` (proof its work
    # survived into the integration), and the full log must show both the
    # checkpoint and the executor's own commit -- never a re-branched
    # history that silently dropped the checkpoint and only kept the new one.
    ancestor_check = subprocess.run(
        ["git", "-C", str(work), "merge-base", "--is-ancestor", checkpoint_sha, "main"],
    )
    assert ancestor_check.returncode == 0, (
        "resumed branch lost its checkpoint commit -- it never reached integrated main "
        "(_setup_cycle_branch must have reset to origin/main instead of reusing the branch)"
    )
    log = subprocess.run(
        ["git", "-C", str(work), "log", "--oneline", "main"], capture_output=True, text=True,
    ).stdout
    assert "feat: add feature" in log


def test_setup_cycle_branch_reuse_keeps_existing_commits(tmp_path: Path):
    """Narrower unit-level proof, directly on `_setup_cycle_branch`: with
    `reuse_existing_branch=True` and a same-named local branch already
    present, checkout reuses it (`resumed: True`, HEAD unchanged) instead of
    force-resetting it to `origin/main` -- the `-B` default's exact opposite.
    """
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo, _run

    from nanobot.runtime import bridge

    base = tmp_path / "base"
    base.mkdir()
    _origin, work = _init_selfevo_repo(base)

    cycle_id = "cycle-reuse-01"
    branch = f"selfevo/cycle-{cycle_id}"
    _run(work, "checkout", "-b", branch)
    (work / "extra.py").write_text("x = 1\n", encoding="utf-8")
    _run(work, "add", "extra.py")
    _run(work, "commit", "-m", "wip commit on the branch")
    branch_head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", branch], capture_output=True, text=True,
    ).stdout.strip()
    _run(work, "checkout", "main")

    result = bridge._setup_cycle_branch(work, cycle_id, reuse_existing_branch=True)
    assert result['ok'] is True
    assert result['branch'] == branch
    assert result.get('resumed') is True

    head_after = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert head_after == branch_head, "reuse path must not move the branch pointer"

    # A fresh (non-resume) setup on a DIFFERENT cycle_id still force-resets
    # from origin/main, unaffected by the reuse path existing.
    fresh = bridge._setup_cycle_branch(work, "cycle-fresh-01", reuse_existing_branch=False)
    assert fresh['ok'] is True
    assert fresh.get('resumed') is False
    fresh_head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert fresh_head == fresh['main_sha']


def test_change_shape_reads_last_non_checkpoint_commit(tmp_path: Path, monkeypatch):
    """ADR-035 keep-work architect addendum (#1942 B2): the branch's raw
    HEAD may itself be a checkpoint commit -- ``change_shape`` telemetry
    must classify the latest NON-artificial commit's subject instead, or a
    cycle whose last action was a checkpoint would read as
    ``unclassified`` instead of its real shape.
    """
    import asyncio

    from tests.test_cycle_ledger import _init_selfevo_repo, _run

    from nanobot.runtime import bridge
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER
    from nanobot.runtime.cycle_ledger import read_events

    class _FakeManagerRealThenCheckpoint:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            if self._telemetry_component == "planner":
                return "fake planner spawned (noop)"
            (self.workspace / "scripts").mkdir(exist_ok=True)
            (self.workspace / "scripts" / "feature.py").write_text("def feature():\n    return 42\n")
            _run(self.workspace, "add", "scripts/feature.py")
            _run(self.workspace, "commit", "-m", "feat: add feature")
            # The executor's last action before finishing is itself a
            # checkpoint -- no separate wrap-up commit follows it.
            (self.workspace / "scripts" / "feature.py").write_text(
                "def feature():\n    return 42\n\n# tweak\n",
            )
            _run(self.workspace, "add", "scripts/feature.py")
            import subprocess as _sp
            _sp.run(
                ["git", "-C", str(self.workspace), "commit",
                 "-m", "selfevo: checkpoint — scripts/feature.py", "-m", CHECKPOINT_TRAILER],
                check=True, capture_output=True,
            )
            return "fake subagent spawned"

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _init_selfevo_repo(base)

    _stub_planning_session(monkeypatch, "a novel bounded increment")
    monkeypatch.setattr(bridge, "SubagentManager", _FakeManagerRealThenCheckpoint)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    outcome_rows = [e for e in read_events(state_dir) if e.get("phase") == "outcome"]
    assert outcome_rows, "no outcome row recorded"
    assert outcome_rows[-1].get("change_shape") == "feature", (
        f"change_shape read the checkpoint's own subject instead of the real commit: {outcome_rows[-1]}"
    )


def test_integrated_branch_has_non_checkpoint_commit(tmp_path: Path, monkeypatch):
    """Architect invariant (ADR-035 keep-work addendum, #1942 B2): an
    integrated cycle branch always carries at least one non-artificial
    commit describing work -- otherwise self_dedup, the edit-budget
    counter, change_shape telemetry and the health activity metric (all
    four now excluding checkpoint/residual commits by pattern) would see
    NOTHING for a cycle whose entire work happened inside checkpoints
    (the typical killed-and-resumed shape).

    Simplest enforcement, chosen and explained in the PR: when every
    commit on the branch since pre-spawn is artificial,
    ``_main_impl_body`` adds one empty, task-titled closing commit before
    the gate/integration decision -- a marker, not a second copy of the
    diff (the checkpoints already carry the real file changes).
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo, _run

    from nanobot.runtime import bridge
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER, is_artificial_commit_subject
    from nanobot.runtime.cycle_ledger import read_events

    class _CheckpointOnlyManager:
        """Simulates a cycle whose ENTIRE work happened inside checkpoints
        -- no separate closing commit, unlike _FakeSubagentManager."""

        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            if self._telemetry_component == "planner":
                return "fake planner spawned (noop)"
            import subprocess as _sp
            (self.workspace / "scripts").mkdir(exist_ok=True)
            (self.workspace / "scripts" / "feature.py").write_text("def feature():\n    return 42\n")
            _run(self.workspace, "add", "scripts/feature.py")
            _sp.run(
                ["git", "-C", str(self.workspace), "commit",
                 "-m", "selfevo: checkpoint — scripts/feature.py", "-m", CHECKPOINT_TRAILER],
                check=True, capture_output=True,
            )
            return "fake subagent spawned"

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _init_selfevo_repo(base)

    _stub_planning_session(monkeypatch, "finish the checkpoint-only feature")
    monkeypatch.setattr(bridge, "SubagentManager", _CheckpointOnlyManager)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    outcome_rows = [e for e in read_events(state_dir) if e.get("phase") == "outcome"]
    assert outcome_rows and outcome_rows[-1].get("outcome") == "success", outcome_rows

    # The invariant itself: main's history for this cycle must contain a
    # non-artificial commit even though the executor made only checkpoints.
    log = subprocess.run(
        ["git", "-C", str(base / "eeebot-self-evolving"), "log", "--format=%s", "-5"],
        capture_output=True, text=True,
    ).stdout
    subjects = [s for s in log.splitlines() if s.strip()]
    assert any(not is_artificial_commit_subject(s) and not s.startswith(("merge:", "chore: regenerate skills/index.md"))
               for s in subjects), f"no non-artificial commit found in main's recent history: {subjects}"

    # And the reader this whole addendum is about: change_shape must be
    # present (a real, if generic, classification), never absent/"unknown".
    assert "change_shape" in outcome_rows[-1], (
        f"change_shape absent -- the checkpoint-only branch left no evidence: {outcome_rows[-1]}"
    )


# --- keep-work: a killed attempt keeps its checkpoint commits (ADR-035,
# #1942 B2) --------------------------------------------------------------------

def test_killed_attempt_keeps_checkpoint_commits(tmp_path: Path):
    """"The executor's work is committed to the cycle branch as it goes, at
    least at every completed step that changed files ... a kill loses
    minutes, not the attempt." (ADR-035 keep-work). Drives a real
    ``SubagentManager._run_subagent`` loop (not the bridge-level
    ``_FakeSubagentManager`` shortcut other tests use, which bypasses the
    loop entirely) against a fake multi-turn provider, so the per-iteration
    checkpoint hook in ``nanobot.agent.subagent`` actually fires. Two
    completed tool-executing steps must each leave their OWN commit,
    proving granularity (not one commit at the very end) -- a kill after
    step 1 but before step 2 would keep step 1's file, which a single
    end-of-run commit could not guarantee.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.agent.subagent import SubagentManager
    from nanobot.providers.base import ToolCallRequest

    base = tmp_path / "base"
    base.mkdir()
    _origin, work = _init_selfevo_repo(base)
    # The checkpoint writer only fires on a selfevo/cycle-* branch (pG
    # review: never on whatever happens to be checked out) -- this test is
    # about granularity within a real cycle, so put the workspace where a
    # real one always is.
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "selfevo/cycle-checkpoint-test"], check=True, capture_output=True)

    class FakeResponse:
        def __init__(self, content="", finish_reason="stop", has_tool_calls=False, tool_calls=None, usage=None):
            self.content = content
            self.finish_reason = finish_reason
            self.has_tool_calls = has_tool_calls
            self.tool_calls = tool_calls or []
            self.usage = usage or {}
            self.reasoning_content = None
            self.thinking_blocks = None

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def chat_with_retry(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(
                    has_tool_calls=True,
                    tool_calls=[ToolCallRequest(
                        id="call-1", name="write_file",
                        arguments={"path": "scripts/step_one.py", "content": "STEP = 1\n"},
                    )],
                )
            if self.calls == 2:
                return FakeResponse(
                    has_tool_calls=True,
                    tool_calls=[ToolCallRequest(
                        id="call-2", name="write_file",
                        arguments={"path": "scripts/step_two.py", "content": "STEP = 2\n"},
                    )],
                )
            return FakeResponse(content="done, two steps completed")

    from unittest.mock import AsyncMock

    manager = SubagentManager(
        provider=FakeProvider(),
        workspace=work,
        bus=AsyncMock(),
        model="fake/model",
        max_iterations=5,
        checkpoint_commits=True,
        expected_cycle_branch="selfevo/cycle-checkpoint-test",
    )

    asyncio.run(manager._run_subagent("t1", "finish the wip feature", "label", {"channel": "cli", "chat_id": "direct"}))

    log = subprocess.run(
        ["git", "-C", str(work), "log", "--format=%s%x1f%b%x1e"], capture_output=True, text=True,
    ).stdout
    records = [r.strip("\n") for r in log.split("\x1e") if r.strip()]
    checkpoint_records = [r for r in records if r.split("\x1f", 1)[0].startswith("selfevo: checkpoint")]
    assert len(checkpoint_records) == 2, f"expected 2 checkpoint commits, got {len(checkpoint_records)}:\n{records}"

    subjects = [r.split("\x1f", 1)[0] for r in checkpoint_records]
    assert any("step_one.py" in s for s in subjects)
    assert any("step_two.py" in s for s in subjects)
    for record in checkpoint_records:
        subject, _sep, body = record.partition("\x1f")
        assert "Selfevo-Checkpoint: true" in body

    # Granularity: the FIRST checkpoint (oldest commit touching step_one.py)
    # must not already contain step_two.py -- a kill right after step 1
    # would resume with only step_one.py's work, not both.
    first_checkpoint_files = subprocess.run(
        ["git", "-C", str(work), "log", "--diff-filter=A", "--name-only", "--format=", "--", "scripts/step_one.py"],
        capture_output=True, text=True,
    ).stdout
    assert "step_two.py" not in first_checkpoint_files


def test_checkpoint_commit_never_lands_on_main(tmp_path: Path):
    """pG review: `_maybe_checkpoint_commit` must never fire on whatever
    branch happens to be checked out -- only on a `selfevo/cycle-*` cycle
    branch. `bridge._setup_cycle_branch` can fail open and leave the
    checkout on `main` while the executor still spawns; committing a
    checkpoint there would land it on the SHARED local `main` (the
    "workspace main diverges" defect class), not a harmless checkpoint.
    Drives the same real ``SubagentManager._run_subagent`` loop as
    ``test_killed_attempt_keeps_checkpoint_commits``, but on a workspace
    left checked out on `main` -- the loop must complete with a real file
    change and NO new commit at all.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.agent.subagent import SubagentManager
    from nanobot.providers.base import ToolCallRequest

    base = tmp_path / "base"
    base.mkdir()
    _origin, work = _init_selfevo_repo(base)

    head_before = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    branch_before = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert branch_before == "main"

    class FakeResponse:
        def __init__(self, content="", has_tool_calls=False, tool_calls=None):
            self.content = content
            self.finish_reason = "stop"
            self.has_tool_calls = has_tool_calls
            self.tool_calls = tool_calls or []
            self.usage = {}
            self.reasoning_content = None
            self.thinking_blocks = None

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def chat_with_retry(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(
                    has_tool_calls=True,
                    tool_calls=[ToolCallRequest(
                        id="call-1", name="write_file",
                        arguments={"path": "scripts/on_main.py", "content": "X = 1\n"},
                    )],
                )
            return FakeResponse(content="done")

    from unittest.mock import AsyncMock

    manager = SubagentManager(
        provider=FakeProvider(),
        workspace=work,
        bus=AsyncMock(),
        model="fake/model",
        max_iterations=5,
        checkpoint_commits=True,
        expected_cycle_branch="selfevo/cycle-should-be-elsewhere",
    )

    asyncio.run(manager._run_subagent("t1", "task on main", "label", {"channel": "cli", "chat_id": "direct"}))

    # The tool call really ran (file exists, uncommitted) -- this isn't
    # passing because nothing happened.
    assert (work / "scripts" / "on_main.py").is_file()
    status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain", "-uall"], capture_output=True, text=True,
    ).stdout
    assert "on_main.py" in status

    head_after = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert head_after == head_before, "checkpoint committed on main -- the branch guard did not fire"


def test_checkpoint_bound_to_expected_branch(tmp_path: Path, monkeypatch):
    """D3 (ADR-035 Test Contract, external review finding #3; architect
    resolution on #1979 external-review followups): the checkpoint writer
    must bind to the SPECIFIC branch the bridge handed this executor for
    THIS cycle, using the workspace's ORDINARY index (no private/temp
    index) and a compare-and-swap ``update-ref`` -- never a prefix guess
    from HEAD, and never a silent overwrite when the branch moved
    concurrently. Three scenarios, all against real repos:

    1. HEAD on a DIFFERENT ``selfevo/cycle-*`` branch than expected ->
       skip, no commit anywhere, main untouched (drives the real
       ``_run_subagent`` loop, same pattern as
       ``test_checkpoint_commit_never_lands_on_main``).
    2. HEAD on the expected branch, but the branch moves (a concurrent
       writer lands a commit on it via plumbing, simulating a race)
       between the checkpoint capturing that branch's tip and its final
       ``update-ref`` -- the CAS must reject the write; the concurrent
       commit stays the branch tip, the checkpoint's own attempt never
       lands, and the uncommitted file change is left exactly as it was
       (not lost).
    3. A normal, uncontested checkpoint leaves the INDEX clean afterwards
       (``git status --porcelain`` empty, ``git diff --cached --quiet``
       exits 0 -- the index equals the new HEAD's tree, no desync from
       skipping ``git commit``), and a subsequent ORDINARY ``git commit``
       on that branch preserves the checkpoint's file rather than
       reverting it.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.agent.subagent import SubagentManager
    from nanobot.providers.base import ToolCallRequest
    from unittest.mock import AsyncMock

    class FakeResponse:
        def __init__(self, content="", has_tool_calls=False, tool_calls=None):
            self.content = content
            self.finish_reason = "stop"
            self.has_tool_calls = has_tool_calls
            self.tool_calls = tool_calls or []
            self.usage = {}
            self.reasoning_content = None
            self.thinking_blocks = None

    class FakeProvider:
        def __init__(self, path: str, content: str):
            self.calls = 0
            self._path = path
            self._content = content

        async def chat_with_retry(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(
                    has_tool_calls=True,
                    tool_calls=[ToolCallRequest(
                        id="call-1", name="write_file",
                        arguments={"path": self._path, "content": self._content},
                    )],
                )
            return FakeResponse(content="done")

    # --- Scenario 1: wrong branch -------------------------------------
    base = tmp_path / "base"
    base.mkdir()
    _origin, work = _init_selfevo_repo(base)

    other_cycle_branch = "selfevo/cycle-B"
    expected_branch = "selfevo/cycle-A"
    # The workspace is checked out on cycle B's branch -- a real
    # selfevo/cycle-* branch, so a prefix check would have let this
    # through -- while THIS executor was spawned for cycle A.
    subprocess.run(["git", "-C", str(work), "checkout", "-b", other_cycle_branch], check=True, capture_output=True)

    head_before = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    expected_branch_before = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--verify", expected_branch],
        capture_output=True, text=True,
    )
    assert expected_branch_before.returncode != 0, "expected_branch must not exist yet in this fixture"

    manager = SubagentManager(
        provider=FakeProvider("scripts/on_wrong_branch.py", "X = 1\n"),
        workspace=work,
        bus=AsyncMock(),
        model="fake/model",
        max_iterations=5,
        checkpoint_commits=True,
        expected_cycle_branch=expected_branch,
    )

    asyncio.run(manager._run_subagent("t1", "task on wrong cycle branch", "label", {"channel": "cli", "chat_id": "direct"}))

    # The tool call really ran -- this isn't passing because nothing happened.
    assert (work / "scripts" / "on_wrong_branch.py").is_file()
    status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain", "-uall"], capture_output=True, text=True,
    ).stdout
    assert "on_wrong_branch.py" in status

    head_after = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert head_after == head_before, (
        "checkpoint committed onto the checked-out branch (cycle B) despite "
        "expecting cycle A -- the exact-match guard did not fire"
    )

    expected_branch_after = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--verify", expected_branch],
        capture_output=True, text=True,
    )
    assert expected_branch_after.returncode != 0, (
        "checkpoint fabricated the expected branch out of thin air instead of skipping"
    )

    # --- Scenario 2: concurrent branch move (race) --------------------
    base2 = tmp_path / "base2"
    base2.mkdir()
    _origin2, work2 = _init_selfevo_repo(base2)
    branch2 = "selfevo/cycle-race"
    subprocess.run(["git", "-C", str(work2), "checkout", "-b", branch2], check=True, capture_output=True)
    (work2 / "scripts").mkdir(exist_ok=True)
    (work2 / "scripts" / "dirty.py").write_text("X = 1\n", encoding="utf-8")

    old_tip = subprocess.run(
        ["git", "-C", str(work2), "rev-parse", branch2], capture_output=True, text=True,
    ).stdout.strip()

    real_run = subprocess.run
    advanced = {"done": False}

    def _race_after_old_capture(cmd, *args, **kwargs):
        result = real_run(cmd, *args, **kwargs)
        if (
            not advanced["done"]
            and isinstance(cmd, list)
            and cmd[-2:] == ["rev-parse", f"refs/heads/{branch2}"]
            and result.returncode == 0
        ):
            advanced["done"] = True
            # A concurrent writer lands a real commit on the SAME branch
            # via plumbing -- this checkout's working tree/index are
            # untouched by it, exactly like a race with another process.
            tree_sha = real_run(
                ["git", "-C", str(work2), "rev-parse", f"{old_tip}^{{tree}}"],
                capture_output=True, text=True,
            ).stdout.strip()
            concurrent_sha = real_run(
                ["git", "-C", str(work2), "commit-tree", tree_sha, "-p", old_tip, "-m", "concurrent writer"],
                capture_output=True, text=True,
            ).stdout.strip()
            real_run(
                ["git", "-C", str(work2), "update-ref", f"refs/heads/{branch2}", concurrent_sha, old_tip],
                capture_output=True, text=True,
            )
        return result

    manager2 = SubagentManager(
        provider=FakeProvider("scripts/dirty.py", "X = 1\n"), workspace=work2, bus=AsyncMock(), model="fake/model",
        expected_cycle_branch=branch2,
    )

    monkeypatch.setattr(subprocess, "run", _race_after_old_capture)
    manager2._maybe_checkpoint_commit()
    monkeypatch.undo()

    branch2_subject = subprocess.run(
        ["git", "-C", str(work2), "log", "-1", "--format=%s", branch2], capture_output=True, text=True,
    ).stdout.strip()
    assert branch2_subject == "concurrent writer", (
        "the checkpoint's own commit landed despite the branch moving concurrently -- "
        f"branch tip subject is {branch2_subject!r}"
    )
    assert (work2 / "scripts" / "dirty.py").read_text(encoding="utf-8") == "X = 1\n", (
        "the uncommitted change was lost, not merely left uncommitted"
    )


def test_rejected_checkpoint_resets_index_keeps_worktree(tmp_path: Path, monkeypatch):
    """D3 (ADR-035 Test Contract #1979, Codex P2 on 16710f62): after a
    REJECTED checkpoint -- HEAD not on the expected branch, or a rejected
    ``update-ref`` CAS -- ``_maybe_checkpoint_commit`` runs ``git reset
    -q`` (never ``--hard``): the INDEX is reset back to HEAD, but the
    WORKING TREE keeps the executor's edits (nothing is lost, only left
    uncommitted), and the skip reason is logged. Two sub-scenarios,
    against real repos.
    """
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.agent import subagent as subagent_module
    from nanobot.agent.subagent import SubagentManager

    logged_reasons: list = []
    monkeypatch.setattr(
        subagent_module.logger, "debug",
        lambda *args, **kwargs: logged_reasons.append(args),
    )

    class _NeverCalledProvider:
        async def chat_with_retry(self, *args, **kwargs):
            raise AssertionError("provider must never be called by _maybe_checkpoint_commit")

    # --- Sub-scenario 1: HEAD not on the expected branch -------------
    base = tmp_path / "base"
    base.mkdir()
    _origin, work = _init_selfevo_repo(base)
    branch_b = "selfevo/cycle-B"
    expected_a = "selfevo/cycle-A"
    subprocess.run(["git", "-C", str(work), "checkout", "-b", branch_b], check=True, capture_output=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "edit.py").write_text("EXECUTOR_EDIT = 1\n", encoding="utf-8")
    # Simulates the executor having already staged its own in-progress edit
    # (e.g. via its own tool-driven `git add`) before the checkpoint hook
    # runs on the wrong branch.
    subprocess.run(["git", "-C", str(work), "add", "scripts/edit.py"], check=True, capture_output=True)

    manager1 = SubagentManager(
        provider=_NeverCalledProvider(), workspace=work, bus=object(), model="fake/model",
        expected_cycle_branch=expected_a,
    )
    manager1._maybe_checkpoint_commit()

    assert (work / "scripts" / "edit.py").read_text(encoding="utf-8") == "EXECUTOR_EDIT = 1\n", (
        "the executor's edit must survive in the working tree, only ever unstaged"
    )
    diff_cached_1 = subprocess.run(["git", "-C", str(work), "diff", "--cached", "--quiet"])
    assert diff_cached_1.returncode == 0, (
        "the index must be reset to HEAD (no staged entries left) after a rejected checkpoint"
    )
    status_1 = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain", "-uall"], capture_output=True, text=True,
    ).stdout
    assert "scripts/edit.py" in status_1, "the file itself must still be present, just unstaged"
    assert any("HEAD not on expected branch" in str(call) for call in logged_reasons), (
        f"the skip reason must be recorded: {logged_reasons!r}"
    )

    # --- Sub-scenario 2: a rejected update-ref CAS (concurrent branch move) ---
    logged_reasons.clear()
    base2 = tmp_path / "base2"
    base2.mkdir()
    _origin2, work2 = _init_selfevo_repo(base2)
    branch2 = "selfevo/cycle-race-reset"
    subprocess.run(["git", "-C", str(work2), "checkout", "-b", branch2], check=True, capture_output=True)
    (work2 / "scripts").mkdir(exist_ok=True)
    (work2 / "scripts" / "dirty.py").write_text("EXECUTOR_EDIT = 2\n", encoding="utf-8")

    old_tip = subprocess.run(
        ["git", "-C", str(work2), "rev-parse", branch2], capture_output=True, text=True,
    ).stdout.strip()

    real_run = subprocess.run
    advanced = {"done": False}

    def _race_after_old_capture(cmd, *args, **kwargs):
        result = real_run(cmd, *args, **kwargs)
        if (
            not advanced["done"]
            and isinstance(cmd, list)
            and cmd[-2:] == ["rev-parse", f"refs/heads/{branch2}"]
            and result.returncode == 0
        ):
            advanced["done"] = True
            tree_sha = real_run(
                ["git", "-C", str(work2), "rev-parse", f"{old_tip}^{{tree}}"],
                capture_output=True, text=True,
            ).stdout.strip()
            concurrent_sha = real_run(
                ["git", "-C", str(work2), "commit-tree", tree_sha, "-p", old_tip, "-m", "concurrent writer"],
                capture_output=True, text=True,
            ).stdout.strip()
            real_run(
                ["git", "-C", str(work2), "update-ref", f"refs/heads/{branch2}", concurrent_sha, old_tip],
                capture_output=True, text=True,
            )
        return result

    monkeypatch.setattr(subprocess, "run", _race_after_old_capture)
    manager2 = SubagentManager(
        provider=_NeverCalledProvider(), workspace=work2, bus=object(), model="fake/model",
        expected_cycle_branch=branch2,
    )
    manager2._maybe_checkpoint_commit()
    monkeypatch.undo()

    assert (work2 / "scripts" / "dirty.py").read_text(encoding="utf-8") == "EXECUTOR_EDIT = 2\n", (
        "the executor's edit must survive a rejected CAS, not be lost"
    )
    diff_cached_2 = subprocess.run(["git", "-C", str(work2), "diff", "--cached", "--quiet"])
    assert diff_cached_2.returncode == 0, "the index must be reset to HEAD after a rejected CAS"
    assert any("update-ref CAS rejected" in str(call) for call in logged_reasons), (
        f"the CAS-rejection reason must be recorded: {logged_reasons!r}"
    )


def test_checkpoint_keeps_shared_index_coherent(tmp_path: Path):
    """D3 (ADR-035 Test Contract #1979, 16710f62): after a successful
    checkpoint CAS, the workspace's ORDINARY (shared, not private) index
    must equal the new HEAD's tree -- ``git diff --cached --quiet`` exits
    0 -- and a subsequent ORDINARY ``git commit`` on that branch must
    preserve the checkpoint's file rather than reverting/losing it. This
    is the direct consequence of building the commit's tree from
    ``write-tree`` against the same index ``git add`` just staged
    (``_maybe_checkpoint_commit`` never uses a private/temporary index),
    but is worth proving directly: a checkpoint that looked successful
    while secretly leaving the shared index out of sync would corrupt
    every commit made after it.
    """
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.agent.subagent import SubagentManager
    from nanobot.providers.base import ToolCallRequest
    from unittest.mock import AsyncMock

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def chat_with_retry(self, *args, **kwargs):
            self.calls += 1
            return None

    base = tmp_path / "base"
    base.mkdir()
    _origin, work = _init_selfevo_repo(base)
    branch = "selfevo/cycle-clean-index"
    subprocess.run(["git", "-C", str(work), "checkout", "-b", branch], check=True, capture_output=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "clean.py").write_text("Y = 2\n", encoding="utf-8")

    manager = SubagentManager(
        provider=FakeProvider(), workspace=work, bus=AsyncMock(), model="fake/model",
        expected_cycle_branch=branch,
    )
    manager._maybe_checkpoint_commit()

    commit_subject = subprocess.run(
        ["git", "-C", str(work), "log", "-1", "--format=%s", branch], capture_output=True, text=True,
    ).stdout.strip()
    assert "clean.py" in commit_subject, f"expected checkpoint did not land: {commit_subject!r}"

    clean_status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain"], capture_output=True, text=True,
    ).stdout
    assert clean_status == "", f"index desynced from the committed tree after a checkpoint: {clean_status!r}"

    diff_cached = subprocess.run(["git", "-C", str(work), "diff", "--cached", "--quiet"])
    assert diff_cached.returncode == 0, "index does not match the new HEAD's tree after the checkpoint"

    (work / "scripts" / "second.py").write_text("Z = 3\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "scripts/second.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "a normal follow-up commit"], check=True, capture_output=True,
    )
    tracked_files = subprocess.run(
        ["git", "-C", str(work), "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True,
    ).stdout
    assert "scripts/clean.py" in tracked_files, (
        "the checkpoint's file was reverted/lost by the next ordinary commit instead of preserved"
    )
    assert "scripts/second.py" in tracked_files
    assert (work / "scripts" / "clean.py").read_text(encoding="utf-8") == "Y = 2\n"


def test_unfinished_session_never_integrates(tmp_path: Path, monkeypatch):
    """D1 (ADR-035 Test Contract, external review finding #1): session
    STATUS decides completeness, not commit count. A session whose own
    telemetry says ``status: error`` is not finished -- however many
    checkpoint commits its branch already holds -- and must never reach
    the smoke/gate/integrate path, must never get a manufactured closing
    commit, and must leave `main` untouched.

    Drives a real ``bridge._main_impl()`` cycle with a fake executor
    spawn that (1) commits a real checkpoint to the cycle branch, exactly
    as ``_maybe_checkpoint_commit`` would mid-run, THEN (2) writes its
    OWN terminal telemetry with ``status: "error"`` -- exactly the
    checkpoint-then-died-on-the-next-step scenario the external review
    reproduced. The error text does not match the supplier-outage
    pattern, so this must classify as ``interrupted_defect``, not
    ``interrupted_supply``.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.runtime import bridge, open_increment
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _origin, work = _init_selfevo_repo(base)

    class _CheckpointThenErrorManager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            if self._telemetry_component == "planner":
                return "fake planner spawned (noop)"
            (self.workspace / "scripts").mkdir(exist_ok=True)
            (self.workspace / "scripts" / "partial.py").write_text(
                "def partial():\n    return 'wip'\n", encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(self.workspace), "add", "scripts/partial.py"],
                check=True, capture_output=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(self.workspace), "commit",
                    "-m", "selfevo: checkpoint — scripts/partial.py", "-m", CHECKPOINT_TRAILER,
                ],
                check=True, capture_output=True,
            )
            task_id = "errored-task-01"
            (state_dir / "subagents").mkdir(parents=True, exist_ok=True)
            (state_dir / "subagents" / f"{task_id}.json").write_text(
                json.dumps({
                    "status": "error",
                    "summary": "Error: unexpected exception in tool execution",
                    "result": "Error: unexpected exception in tool execution",
                }),
                encoding="utf-8",
            )

            async def _noop():
                return None

            self._running_tasks[task_id] = asyncio.create_task(_noop())
            return "fake subagent spawned"

    monkeypatch.setattr(bridge, "SubagentManager", _CheckpointThenErrorManager)
    _stub_planning_session(monkeypatch, "finish the wip feature")

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    # `main` legitimately advances for unrelated bookkeeping (the opening
    # diary entry, pushed before the executor even spawns) -- what must
    # NEVER happen is the checkpoint's own work reaching it.
    main_tree = subprocess.run(
        ["git", "-C", str(work), "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True,
    ).stdout
    assert "scripts/partial.py" not in main_tree, (
        "an errored session with checkpoints must never integrate onto main"
    )
    main_subjects = subprocess.run(
        ["git", "-C", str(work), "log", "--format=%s", "main"], capture_output=True, text=True,
    ).stdout
    assert "merge: integrate" not in main_subjects

    # No manufactured closing commit -- exactly the one checkpoint commit
    # this test wrote, nothing else, on whatever branch it landed on.
    branches = subprocess.run(
        ["git", "-C", str(work), "for-each-ref", "--format=%(refname:short)", "refs/heads/selfevo/cycle-*"],
        capture_output=True, text=True,
    ).stdout.split()
    assert len(branches) == 1, f"expected exactly one retained cycle branch, got {branches!r}"
    subjects = subprocess.run(
        ["git", "-C", str(work), "log", "--format=%s", f"main..{branches[0]}"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert len([s for s in subjects if s.strip()]) == 1, (
        f"expected exactly the one checkpoint commit, no manufactured closing marker: {subjects!r}"
    )

    pending = open_increment.pending_open_increment(state_dir)
    assert pending is not None, "an unfinished, checkpointed session must leave a pending open increment"
    assert pending["reason"] == "interrupted_defect", (
        "a non-supplier error text must classify as our own defect, not a supplier interruption"
    )
    assert pending["branch"] == branches[0]


# --- keep-work: a resumed increment keeps one opening entry (ADR-035,
# #1942 B2) --------------------------------------------------------------------

def test_resumed_increment_keeps_one_opening(tmp_path: Path, monkeypatch):
    """"One opening, one plan chain. A resumed increment appends to its
    existing diary entry and plan instead of writing a new opening entry
    per attempt." (ADR-035 keep-work). Simulates the interrupted attempt's
    own opening entry (written before it started, ADR-028 rule 2) directly
    via ``_write_diary_open_entry``, then drives a `keep`-resumed
    ``_main_impl()`` cycle and asserts the diary gained NO second opening
    entry for the same task title -- only ``resume_skip_opening_entry``
    (wired from ``open_increment.pending["opening_entry_written"]``)
    stands between this and a duplicate per ADR-028 rule 2's own
    unconditional write.
    """
    import asyncio

    from tests.test_cycle_ledger import _FakeSubagentManager, _init_selfevo_repo

    from nanobot.runtime import bridge, open_increment

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _origin, work = _init_selfevo_repo(base)

    task_title = "finish the wip feature (resume test)"
    old_cycle_id = "cycle-interrupted-02"
    old_branch = f"selfevo/cycle-{old_cycle_id}"

    # The interrupted attempt's own opening entry, written before it began
    # executing -- exactly what ADR-028 rule 2 already does today, simulated
    # directly rather than by running a doomed-to-fail first cycle.
    write_result = bridge._write_diary_open_entry(work, state_dir, old_cycle_id, task_title)
    assert write_result['outcome'] == 'integrated', write_result

    diary_files_before = sorted((work / "diary").glob("*.md"))
    assert len(diary_files_before) == 1
    diary_text_before = diary_files_before[0].read_text(encoding="utf-8")
    assert diary_text_before.count(task_title) == 1

    open_increment.record_supply_interruption(
        state_dir, old_cycle_id,
        retry_key="self-resume-opening-test", plan_text=task_title, candidate_id=None,
        selfevo_repo=work, branch=old_branch,
    )
    _oi_state = open_increment.load_state(state_dir)
    _oi_state.hold["deadline"] = "2000-01-01T00:00:00Z"
    open_increment._save_state(state_dir, _oi_state)

    # The interrupted attempt's own branch, with a checkpoint commit, so the
    # resume path's branch-reuse setup succeeds (not the focus of this test,
    # but required for the cycle to run to completion).
    import subprocess

    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER
    subprocess.run(["git", "-C", str(work), "checkout", "-b", old_branch], check=True, capture_output=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "wip.py").write_text("def wip():\n    return 'partial'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "scripts/wip.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "selfevo: checkpoint — scripts/wip.py", "-m", CHECKPOINT_TRAILER],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "checkout", "main"], check=True, capture_output=True)

    _stub_planning_session(
        monkeypatch, task_title,
        resume_branch=old_branch, resume_cycle_id=old_cycle_id, resume_skip_opening_entry=True,
    )
    monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    diary_files_after = sorted((work / "diary").glob("*.md"))
    assert len(diary_files_after) == 1, "resume must not create a second day file for this"
    diary_text_after = diary_files_after[0].read_text(encoding="utf-8")
    assert diary_text_after.count(task_title) == 1, (
        f"resume wrote a second opening entry for the same task title:\n{diary_text_after}"
    )


# --- keep-work: D2 -- register before the execution can be killed
# (ADR-035 Test Contract, #1942 B2) --------------------------------------------

def test_killed_attempt_becomes_interrupted_kill(tmp_path: Path, monkeypatch):
    """D2 (ADR-035 Test Contract, external review finding #2): a hard kill
    between the attempt's own registration (written BEFORE the executor
    can be killed) and any terminal outcome must not lose the attempt
    silently. Simulates the kill directly: register a running attempt
    exactly as ``bridge._evaluate_candidate`` does right after cycle-
    branch setup succeeds, but never record a terminal outcome for it (an
    imitation of the process dying in between) -- then drives a REAL
    ``bridge._main_impl()`` tick (planning and the executor spawn are
    stubbed, as in every other ``_main_impl``-level test here; the kill-
    detection wiring itself lives in ``_main_impl``'s own body, before the
    planning call, so it runs whether or not the planning session is
    stubbed) and asserts the stale registration became a pending open
    increment with reason ``interrupted_kill``, naming the dead attempt's
    own branch -- not silently discarded, and not conflated with a
    supplier interruption.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _FakeSubagentManager, _init_selfevo_repo
    from nanobot.runtime import bridge, open_increment
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _origin, work = _init_selfevo_repo(base)

    old_cycle_id = "cycle-killed-01"
    old_branch = f"selfevo/cycle-{old_cycle_id}"
    subprocess.run(["git", "-C", str(work), "checkout", "-b", old_branch], check=True, capture_output=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "wip.py").write_text("def wip():\n    return 'partial'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "scripts/wip.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "selfevo: checkpoint — scripts/wip.py", "-m", CHECKPOINT_TRAILER],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "checkout", "main"], check=True, capture_output=True)

    # The registration a real attempt writes BEFORE it can be killed --
    # simulated directly here, with no matching terminal outcome ever
    # recorded, exactly like a hard kill would leave it.
    open_increment.record_attempt_started(state_dir, old_cycle_id, old_branch)

    _stub_planning_session(monkeypatch, "a brand new, unrelated task")
    monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    pending = open_increment.pending_open_increment(state_dir)
    assert pending is not None, "the killed attempt's registration must become a pending open increment"
    assert pending["reason"] == "interrupted_kill"
    assert pending["cycle_id"] == old_cycle_id
    assert pending["branch"] == old_branch

    state = open_increment.load_state(state_dir)
    assert state.hold is None, "a kill is not supplier evidence -- it must not start a backoff hold"


@pytest.mark.parametrize("executor_status", ["ok", "bounded_stop", "blocked", "cancelled", "error", None])
def test_recovery_maps_every_executor_status(tmp_path: Path, executor_status):
    """D2 (ADR-035 Test Contract #1979, 16710f62): full mapping. The GATE
    never rendered a verdict for the killed attempt's cycle_id
    (record_attempt_finished never ran) regardless of what the
    executor's OWN telemetry says -- ``ok``/``bounded_stop``/``blocked``/
    ``cancelled``, or no telemetry at all, all become ``interrupted_kill``,
    with ``executor_status`` carried into the record as evidence. The ONE
    exception is a telemetry-confirmed ``error``: that is the existing
    supply/defect classifier's job (elsewhere, synchronous, same tick),
    never re-classified as a kill here -- ``check_running_for_kill``
    leaves the registration standing, untouched, for that classifier to
    own.
    """
    from nanobot.runtime import open_increment

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    old_cycle_id = "cycle-killed-01"
    old_branch = f"selfevo/cycle-{old_cycle_id}"
    open_increment.record_attempt_started(state_dir, old_cycle_id, old_branch)

    if executor_status is not None:
        task_id = f"tid-{executor_status}"
        open_increment.record_attempt_task_id(state_dir, old_cycle_id, task_id)
        (state_dir / "subagents").mkdir(parents=True, exist_ok=True)
        (state_dir / "subagents" / f"{task_id}.json").write_text(
            json.dumps({"status": executor_status, "result": "whatever"}), encoding="utf-8",
        )

    stale = open_increment.check_running_for_kill(state_dir, "cycle-fresh-02")

    if executor_status == "error":
        assert stale is None, (
            "a telemetry-confirmed error must be left to the existing "
            "supply/defect classifier, never reclassified as a kill here"
        )
        assert open_increment.load_state(state_dir).running is not None, (
            "the registration must survive untouched for the classifier that owns it"
        )
        return

    assert stale is not None, f"executor_status={executor_status!r} must be classified as a kill"
    assert stale["cycle_id"] == old_cycle_id
    assert stale["branch"] == old_branch
    assert stale["executor_status"] == executor_status

    open_increment.record_kill_interruption(
        state_dir, stale["cycle_id"],
        retry_key=f"kill:{old_cycle_id}",
        plan_text="(a prior attempt was interrupted by a hard kill)",
        candidate_id=None, branch=stale["branch"], executor_status=stale["executor_status"],
    )
    pending = open_increment.pending_open_increment(state_dir)
    assert pending is not None
    assert pending["reason"] == "interrupted_kill"
    assert pending["cycle_id"] == old_cycle_id
    assert pending["branch"] == old_branch
    assert pending["executor_status"] == executor_status

    state = open_increment.load_state(state_dir)
    assert state.hold is None, "a kill is not supplier evidence -- it must not start a backoff hold"


def test_keep_record_survives_kill_before_execution(tmp_path: Path):
    """D2 (ADR-035 Test Contract, external review finding #2): ``resolve``'s
    ``keep`` decision must not erase the pending open increment before
    handing control back to the resumed attempt -- only annotate it. If
    the process is killed in the gap between this decision and the
    resumed attempt's OWN ``record_attempt_started`` registration, the
    pending record is the ONLY durable proof left that this increment
    still needs a terminal outcome (the 77ca scenario B2 exists for).
    Drives ``resolve()`` directly against a real state file -- no bridge
    involved -- and asserts the pending record is still there, right
    after ``resolve()`` returns and BEFORE anything re-registers the
    resumed attempt.
    """
    from nanobot.runtime import open_increment

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    old_cycle_id = "cycle-interrupted-09"
    old_branch = f"selfevo/cycle-{old_cycle_id}"
    open_increment.record_supply_interruption(
        state_dir, old_cycle_id,
        retry_key="keep-survival-test", plan_text="finish the wip feature", candidate_id=None,
        branch=old_branch,
    )
    assert open_increment.pending_open_increment(state_dir) is not None

    deciding_cycle_id = "cycle-deciding-10"
    open_increment.resolve(state_dir, deciding_cycle_id, "keep")

    # This is the exact gap the decision text calls out: the resumed
    # attempt has NOT re-registered itself yet (no record_attempt_started
    # call has happened for this decision) -- if a kill lands here, this
    # pending record is all that is left. It must still be there.
    pending = open_increment.pending_open_increment(state_dir)
    assert pending is not None, "resolve(keep) erased the pending record before the resumed attempt could re-register"
    assert pending["cycle_id"] == old_cycle_id
    assert pending["branch"] == old_branch
    assert pending["reason"] == "interrupted_supply"
    assert pending["resumed_by"] == deciding_cycle_id, "keep must annotate who is resuming it"

    state = open_increment.load_state(state_dir)
    assert state.hold is None
    assert state.held_ticks == 0
    assert state.consecutive_supply_interrupts == 0


def test_keep_never_falls_back_to_reset(tmp_path: Path, monkeypatch):
    """D4 (ADR-035 Test Contract, external review finding #4): a `keep`
    resume must never fall through to `git checkout -B <branch>
    <origin/main>` -- that resets the very branch the keep decision was
    meant to preserve, destroying its checkpoints. Two sub-scenarios,
    both against a real repo:

    1. The branch exists but its reuse checkout fails (e.g. a rejecting
       post-checkout hook) -- must stop with reason
       ``resume_checkout_failed``, the branch's tip untouched, and the
       workspace left where it was, never silently switched onto a
       freshly reset branch.
    2. The branch does not exist at all -- must stop with an explicit
       reason (``resume_branch_missing``), never silently start a fresh
       cycle off main as if nothing was being resumed.
    """
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.runtime import bridge
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    base = tmp_path / "base"
    base.mkdir()
    _origin, work = _init_selfevo_repo(base)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    cycle_id = "cycle-keep-checkout-fail"
    branch = f"selfevo/cycle-{cycle_id}"
    subprocess.run(["git", "-C", str(work), "checkout", "-b", branch], check=True, capture_output=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "retained.py").write_text("def retained():\n    return 'kept'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "scripts/retained.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "selfevo: checkpoint — scripts/retained.py", "-m", CHECKPOINT_TRAILER],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "checkout", "main"], check=True, capture_output=True)
    branch_tip_before = subprocess.run(
        ["git", "-C", str(work), "rev-parse", branch], capture_output=True, text=True,
    ).stdout.strip()

    real_run = subprocess.run

    def _fail_reuse_checkout(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[-2:] == ["checkout", branch]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="simulated post-checkout hook failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fail_reuse_checkout)
    result = bridge._setup_cycle_branch(work, cycle_id, state_dir, reuse_existing_branch=True)

    assert result["ok"] is False
    assert result["reason"] == "resume_checkout_failed"

    branch_tip_after = subprocess.run(
        ["git", "-C", str(work), "rev-parse", branch], capture_output=True, text=True,
    ).stdout.strip()
    assert branch_tip_after == branch_tip_before, "keep fell back to a reset despite a failed reuse checkout"

    current_branch = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    assert current_branch == "main", "workspace was left on the target branch despite a failed checkout"

    # Sub-scenario 2: the branch does not exist at all.
    missing_cycle_id = "cycle-keep-branch-missing"
    missing_branch = f"selfevo/cycle-{missing_cycle_id}"
    result2 = bridge._setup_cycle_branch(work, missing_cycle_id, state_dir, reuse_existing_branch=True)
    assert result2["ok"] is False
    assert result2["reason"] == "resume_branch_missing"

    missing_exists = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--verify", "--quiet", missing_branch],
        capture_output=True, text=True,
    )
    assert missing_exists.returncode != 0, "keep silently created a fresh branch instead of reporting it missing"


def test_increment_accounting_uses_merge_base(tmp_path: Path, monkeypatch):
    """D5 (ADR-035 Test Contract, external review finding #5): work,
    closing-commit eligibility, files_changed and commit counts must be
    measured from the INCREMENT's base (``merge-base(origin/main,
    branch)``), not the attempt's own pre-spawn HEAD. Two scenarios,
    both a `keep`-resumed cycle against a branch that already holds a
    checkpoint:

    1. The resumed executor verifies the retained work and makes NO new
       commit itself -- measured from the attempt's own pre-spawn HEAD
       (which IS the retained tip, since nothing moved it before the
       spawn) this would read as zero new commits and skip the
       closing-commit/gate entirely; measured from the increment's
       merge-base it correctly sees the checkpoint as real, unintegrated
       work and integrates it.
    2. The resumed executor adds ONE more commit -- files_changed (and
       the written result artifact) must cover the retained checkpoint's
       file AND the new commit's file, not just the latter.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.runtime import bridge
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    # --- Scenario 1: resumed, zero new commits ------------------------
    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _origin, work = _init_selfevo_repo(base)

    old_cycle_id = "cycle-retained-05"
    old_branch = f"selfevo/cycle-{old_cycle_id}"
    subprocess.run(["git", "-C", str(work), "checkout", "-b", old_branch], check=True, capture_output=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "retained.py").write_text("def retained():\n    return 'kept'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "scripts/retained.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "selfevo: checkpoint — scripts/retained.py", "-m", CHECKPOINT_TRAILER],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "checkout", "main"], check=True, capture_output=True)

    class _NoNewCommitManager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            # The resumed executor verifies the retained work and
            # finishes WITHOUT making any new commit itself.
            return "fake subagent spawned"

    monkeypatch.setattr(bridge, "SubagentManager", _NoNewCommitManager)
    _stub_planning_session(
        monkeypatch, "finish the retained feature (merge-base test)",
        resume_branch=old_branch, resume_cycle_id=old_cycle_id, resume_skip_opening_entry=True,
    )

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    main_tree = subprocess.run(
        ["git", "-C", str(work), "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True,
    ).stdout
    assert "scripts/retained.py" in main_tree, (
        "a resumed attempt making zero new commits must still integrate the retained checkpoint "
        "work when measured from the increment's merge-base"
    )

    # --- Scenario 2: resumed, one new commit on top of the retained one ---
    base2 = tmp_path / "base2"
    base2.mkdir()
    state_dir2 = _setup_planner_chooses_harness(base2, monkeypatch)
    _origin2, work2 = _init_selfevo_repo(base2)

    old_cycle_id2 = "cycle-retained-06"
    old_branch2 = f"selfevo/cycle-{old_cycle_id2}"
    subprocess.run(["git", "-C", str(work2), "checkout", "-b", old_branch2], check=True, capture_output=True)
    (work2 / "scripts").mkdir(exist_ok=True)
    (work2 / "scripts" / "retained2.py").write_text("def retained():\n    return 'kept'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work2), "add", "scripts/retained2.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work2), "commit", "-m", "selfevo: checkpoint — scripts/retained2.py", "-m", CHECKPOINT_TRAILER],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work2), "checkout", "main"], check=True, capture_output=True)

    class _OneNewCommitManager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            (self.workspace / "scripts" / "finishing.py").write_text(
                "def finishing():\n    return 'done'\n", encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(self.workspace), "add", "scripts/finishing.py"], check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(self.workspace), "commit", "-m", "feat: finish the increment"],
                check=True, capture_output=True,
            )
            return "fake subagent spawned"

    monkeypatch.setattr(bridge, "SubagentManager", _OneNewCommitManager)
    _stub_planning_session(
        monkeypatch, "finish the retained feature (merge-base test 2)",
        resume_branch=old_branch2, resume_cycle_id=old_cycle_id2, resume_skip_opening_entry=True,
    )

    rc2 = asyncio.run(bridge._main_impl())
    assert rc2 == 0

    result_files = list((state_dir2 / "subagents" / "results").glob("result-*.json"))
    assert len(result_files) == 1, f"expected exactly one result artifact, got {result_files!r}"
    result_payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    files_changed = result_payload.get("files_changed") or []
    assert "scripts/retained2.py" in files_changed, (
        f"files_changed omitted the retained checkpoint's file, measured only from this attempt's own "
        f"pre-spawn HEAD instead of the increment's merge-base: {files_changed!r}"
    )
    assert "scripts/finishing.py" in files_changed


def test_failed_closing_commit_blocks_integration(tmp_path: Path, monkeypatch):
    """D6 (ADR-035 Test Contract, external review finding #6): if the
    required closing commit (for an all-checkpoint branch) fails to be
    created, integration must not proceed -- "no closing commit created
    -> no integration" (reason ``closing_commit_failed``). Drives a real
    ``bridge._main_impl()`` cycle whose fake executor spawn commits ONLY
    a checkpoint (no separate closing commit -- the bridge itself must
    create one), with a real ``commit-msg`` hook installed that rejects
    every commit whose subject starts with ``selfevo: `` and does NOT
    carry the checkpoint trailer -- exactly the closing commit's own
    shape, never the checkpoint's -- mirroring the external review's own
    reproduction ("a real commit-message hook rejected the closing
    commit"). Asserts: the branch is retained with exactly the one
    checkpoint commit (no manufactured marker), and the checkpoint's
    work never reaches ``main``.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.runtime import bridge
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _origin, work = _init_selfevo_repo(base)

    hook_path = work / ".git" / "hooks" / "commit-msg"
    hook_path.write_text(
        "#!/bin/sh\n"
        "if grep -q '^selfevo: ' \"$1\" && ! grep -q 'Selfevo-Checkpoint: true' \"$1\"; then\n"
        "  echo 'rejected: non-checkpoint selfevo commit blocked by test hook' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook_path.chmod(0o755)

    class _CheckpointOnlyManager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            if self._telemetry_component == "planner":
                return "fake planner spawned (noop)"
            (self.workspace / "scripts").mkdir(exist_ok=True)
            (self.workspace / "scripts" / "wip.py").write_text(
                "def wip():\n    return 'partial'\n", encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(self.workspace), "add", "scripts/wip.py"], check=True, capture_output=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(self.workspace), "commit",
                    "-m", "selfevo: checkpoint — scripts/wip.py", "-m", CHECKPOINT_TRAILER,
                ],
                check=True, capture_output=True,
            )
            return "fake subagent spawned"

    monkeypatch.setattr(bridge, "SubagentManager", _CheckpointOnlyManager)
    _stub_planning_session(monkeypatch, "finish the wip feature (closing-commit-fail test)")

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    main_tree = subprocess.run(
        ["git", "-C", str(work), "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True,
    ).stdout
    assert "scripts/wip.py" not in main_tree, (
        "a checkpoint-only branch whose closing commit failed must never integrate"
    )

    branches = subprocess.run(
        ["git", "-C", str(work), "for-each-ref", "--format=%(refname:short)", "refs/heads/selfevo/cycle-*"],
        capture_output=True, text=True,
    ).stdout.split()
    assert len(branches) == 1, f"expected exactly one retained cycle branch, got {branches!r}"
    subjects = subprocess.run(
        ["git", "-C", str(work), "log", "--format=%s", f"main..{branches[0]}"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert len([s for s in subjects if s.strip()]) == 1, (
        f"a closing-commit failure must not leave a manufactured marker on the branch: {subjects!r}"
    )


def test_delete_keeps_inspection_ref(tmp_path: Path):
    """D7 (ADR-035 Test Contract, external review finding #7): `delete`
    must protect the branch's commit from immediate pruning by pinning
    it under `refs/selfevo/inspect/<cycle_id>` -- a ref namespace pruning
    never queries (`_prune_stale_cycle_branches` only ever lists
    `refs/heads/selfevo/cycle-*`) -- so the work stays inspectable even
    after the next prune sweep removes an old, unmerged forensic branch
    outside the normal retention window, exactly the external review's
    own reproduction.
    """
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.runtime import open_increment
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    base = tmp_path / "base"
    base.mkdir()
    state_dir = base / "state"
    state_dir.mkdir()
    _origin, work = _init_selfevo_repo(base)

    old_cycle_id = "cycle-delete-07"
    old_branch = f"selfevo/cycle-{old_cycle_id}"
    subprocess.run(["git", "-C", str(work), "checkout", "-b", old_branch], check=True, capture_output=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "inspect_me.py").write_text("def wip():\n    return 'partial'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "scripts/inspect_me.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "selfevo: checkpoint — scripts/inspect_me.py", "-m", CHECKPOINT_TRAILER],
        check=True, capture_output=True,
    )
    branch_tip = subprocess.run(
        ["git", "-C", str(work), "rev-parse", old_branch], capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(work), "checkout", "main"], check=True, capture_output=True)

    open_increment.record_supply_interruption(
        state_dir, old_cycle_id,
        retry_key="delete-test", plan_text="finish the wip feature", candidate_id=None,
        selfevo_repo=work, branch=old_branch,
    )
    assert open_increment.pending_open_increment(state_dir) is not None

    open_increment.resolve(state_dir, "cycle-deciding-08", "delete", selfevo_repo=work)

    assert open_increment.pending_open_increment(state_dir) is None

    inspection_ref = f"refs/selfevo/inspect/{old_cycle_id}"
    inspect_sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", inspection_ref], capture_output=True, text=True,
    ).stdout.strip()
    assert inspect_sha == branch_tip, "delete must pin the branch's tip under the inspection ref"

    # Prove pruning cannot touch it: force-delete the ORIGINAL branch ref
    # directly (simulating the next prune sweep removing an old, unmerged
    # forensic branch outside the retention window) and confirm the
    # commit is still reachable and named via the inspection ref.
    subprocess.run(["git", "-C", str(work), "branch", "-D", old_branch], check=True, capture_output=True)
    still_reachable = subprocess.run(
        ["git", "-C", str(work), "cat-file", "-e", inspection_ref], capture_output=True, text=True,
    )
    assert still_reachable.returncode == 0, (
        "the commit must remain reachable via the inspection ref after the branch is pruned"
    )


def test_missing_open_increment_decision_is_no_plan(tmp_path: Path, monkeypatch):
    """D8 (ADR-035 Test Contract, external review finding #8): a pending
    open increment with a MISSING or INVALID ``open_increment_decision``
    is an invalid plan -- ``no_plan``/``malformed``, planner family --
    never silent acceptance of the new plan with no resume routed
    through. Drives a real ``bridge._main_impl()`` cycle with a fake
    planner spawn whose telemetry carries an otherwise well-formed plan
    (parses fine, has a non-empty ``plan`` field) but simply omits
    ``open_increment_decision``, while a real pending increment already
    exists. The REAL ``_run_planning_session`` runs (only
    ``SubagentManager`` is faked) so the actual validation logic is
    exercised, not a stub.
    """
    import asyncio
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.runtime import bridge, no_plan_recovery, open_increment
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _origin, work = _init_selfevo_repo(base)

    # _run_planning_session (real, not stubbed, in THIS test) hard-requires
    # the release-owned task-writing contract to exist before it will even
    # attempt to parse a plan.
    _task_writing_dir = bridge.RELEASE_ROOT / "nanobot" / "skills" / "task-writing"
    _task_writing_dir.mkdir(parents=True, exist_ok=True)
    (_task_writing_dir / "SKILL.md").write_text("task-writing contract (test stub)\n", encoding="utf-8")

    old_cycle_id = "cycle-no-decision-08"
    old_branch = f"selfevo/cycle-{old_cycle_id}"
    subprocess.run(["git", "-C", str(work), "checkout", "-b", old_branch], check=True, capture_output=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "wip.py").write_text("def wip():\n    return 'partial'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "scripts/wip.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "selfevo: checkpoint — scripts/wip.py", "-m", CHECKPOINT_TRAILER],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "checkout", "main"], check=True, capture_output=True)

    open_increment.record_supply_interruption(
        state_dir, old_cycle_id,
        retry_key="d8-test", plan_text="finish the wip feature", candidate_id=None,
        selfevo_repo=work, branch=old_branch,
    )
    _oi_state = open_increment.load_state(state_dir)
    _oi_state.hold["deadline"] = "2000-01-01T00:00:00Z"
    open_increment._save_state(state_dir, _oi_state)
    pending_before = open_increment.pending_open_increment(state_dir)
    assert pending_before is not None

    class _PlannerOmitsDecisionManager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            if self._telemetry_component != "planner":
                return "fake subagent spawned"
            task_id = "planner-no-decision-01"
            raw_plan = json.dumps({
                "insight": "the retained checkpoint still needs one more commit",
                "plan": "finish the wip feature",
                "iterations_planned": 1,
                # `open_increment_decision` deliberately omitted.
            })
            (state_dir / "subagents").mkdir(parents=True, exist_ok=True)
            (state_dir / "subagents" / f"{task_id}.json").write_text(
                json.dumps({
                    "status": "ok",
                    "result": raw_plan,
                    "context_usage": {"iterations": [{}]},
                }),
                encoding="utf-8",
            )

            async def _noop():
                return None

            self._running_tasks[task_id] = asyncio.create_task(_noop())
            return "fake planner spawned"

    monkeypatch.setattr(bridge, "SubagentManager", _PlannerOmitsDecisionManager)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    pending_after = open_increment.pending_open_increment(state_dir)
    assert pending_after == pending_before, (
        "an unresolved open increment must not be silently cleared/altered by an "
        "invalid plan that never named a decision"
    )

    no_plan_state = no_plan_recovery.load_state(state_dir)
    assert no_plan_state.consecutive_planner >= 1, (
        "a missing/invalid open_increment_decision must count as a planner-family no_plan outcome"
    )


def test_stopped_and_minimal_mode_are_enforced(tmp_path: Path, monkeypatch):
    """D9 (ADR-035 Test Contract, external review finding #9): both
    ``no_plan_recovery`` states are WIRED, not merely recorded --
    "recording a state that nothing acts on is the unobservable guard
    class" (architect resolution). Two scenarios:

    1. ``stopped`` blocks the planning session from even starting, until
       the operator resumes manually (``no_plan_recovery.resume``) -- a
       stubbed planning session must never be called.
    2. ``minimal_mode`` actually selects a smaller context: the
       candidates block is omitted from the planner's task text, not
       merely a recorded flag with no effect on what the session sees.
    """
    import asyncio

    from tests.test_cycle_ledger import _FakeSubagentManager, _init_selfevo_repo
    from nanobot.runtime import bridge, no_plan_recovery

    # --- Scenario 1: stopped blocks session start ---------------------
    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _init_selfevo_repo(base)

    _np_state = no_plan_recovery.load_state(state_dir)
    _np_state.stopped = True
    no_plan_recovery._save_state(state_dir, _np_state)

    planning_calls = _stub_planning_session(monkeypatch, "should never be used")
    monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0
    assert planning_calls == [], "a stopped planner must never even start a session"
    monkeypatch.undo()

    # --- Scenario 2: minimal_mode selects a smaller context ------------
    base2 = tmp_path / "base2"
    base2.mkdir()
    state_dir2 = _setup_planner_chooses_harness(base2, monkeypatch)
    _origin2, work2 = _init_selfevo_repo(base2)

    _task_writing_dir = bridge.RELEASE_ROOT / "nanobot" / "skills" / "task-writing"
    _task_writing_dir.mkdir(parents=True, exist_ok=True)
    (_task_writing_dir / "SKILL.md").write_text("task-writing contract (test stub)\n", encoding="utf-8")

    _np_state2 = no_plan_recovery.load_state(state_dir2)
    _np_state2.minimal_mode = True
    no_plan_recovery._save_state(state_dir2, _np_state2)

    captured_tasks: list = []

    class _CapturingPlannerManager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **kwargs):
            if self._telemetry_component != "planner":
                return "fake subagent spawned"
            captured_tasks.append(kwargs.get("task", ""))
            task_id = "planner-minimal-01"
            raw_plan = json.dumps({
                "insight": "ok", "plan": "a minimal-mode increment", "iterations_planned": 1,
            })
            (state_dir2 / "subagents").mkdir(parents=True, exist_ok=True)
            (state_dir2 / "subagents" / f"{task_id}.json").write_text(
                json.dumps({"status": "ok", "result": raw_plan, "context_usage": {"iterations": [{}]}}),
                encoding="utf-8",
            )

            async def _noop():
                return None

            self._running_tasks[task_id] = asyncio.create_task(_noop())
            return "fake planner spawned"

    monkeypatch.setattr(bridge, "SubagentManager", _CapturingPlannerManager)

    rc2 = asyncio.run(bridge._main_impl())
    assert rc2 == 0
    assert len(captured_tasks) == 1
    assert "minimal mode: candidate list omitted" in captured_tasks[0], (
        f"minimal_mode did not select a smaller context: {captured_tasks[0]!r}"
    )


def test_planner_supplier_error_classified_as_supply(tmp_path: Path, monkeypatch):
    """D10 (ADR-035 Test Contract, external review finding #10): a
    terminal supplier error in the PLANNER's own telemetry must be
    classified BEFORE any attempt to JSON-parse its (nonexistent) final
    answer -- family ``supply``, never ``planner``. Repeated supplier
    failures must build ``consecutive_supply`` (leading eventually to
    ``model_supply_degraded``), never ``consecutive_planner`` (which
    would wrongly march toward ``stopped``/``minimal_mode`` for a
    problem that is not the planner's own fault).
    """
    import asyncio

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.runtime import bridge, no_plan_recovery

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _init_selfevo_repo(base)

    _task_writing_dir = bridge.RELEASE_ROOT / "nanobot" / "skills" / "task-writing"
    _task_writing_dir.mkdir(parents=True, exist_ok=True)
    (_task_writing_dir / "SKILL.md").write_text("task-writing contract (test stub)\n", encoding="utf-8")

    class _PlannerSupplierErrorManager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            if self._telemetry_component != "planner":
                return "fake subagent spawned"
            task_id = "planner-supplier-error-01"
            (state_dir / "subagents").mkdir(parents=True, exist_ok=True)
            (state_dir / "subagents" / f"{task_id}.json").write_text(
                json.dumps({
                    "status": "error",
                    "summary": "Error: LLM execution failed: litellm.APIConnectionError (error code: 503)",
                    "result": "Error: LLM execution failed: litellm.APIConnectionError (error code: 503)",
                    "context_usage": {"iterations": [{}]},
                }),
                encoding="utf-8",
            )

            async def _noop():
                return None

            self._running_tasks[task_id] = asyncio.create_task(_noop())
            return "fake planner spawned"

    monkeypatch.setattr(bridge, "SubagentManager", _PlannerSupplierErrorManager)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    no_plan_state = no_plan_recovery.load_state(state_dir)
    assert no_plan_state.consecutive_supply >= 1, (
        "a terminal supplier error in planner telemetry must count as the supply family"
    )
    assert no_plan_state.consecutive_planner == 0, (
        "a supplier error must never be counted toward the planner family "
        "(malformed/no_plan/refused), which drives stopped/minimal_mode"
    )


def test_planner_rest_resets_supply_streak(tmp_path: Path, monkeypatch):
    """Small item (ADR-035 Test Contract, #1962): the supply-interruption
    streak resets on ANY successful planner model call, including
    ``rest`` -- not just the executor's own post-spawn path. A `rest`
    never launches an executor, so the completion-reset call on that
    path alone would leave the streak stuck even after the supplier
    plainly answered again.
    """
    import asyncio

    from tests.test_cycle_ledger import _init_selfevo_repo
    from nanobot.runtime import bridge, open_increment

    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _origin, work = _init_selfevo_repo(base)

    _task_writing_dir = bridge.RELEASE_ROOT / "nanobot" / "skills" / "task-writing"
    _task_writing_dir.mkdir(parents=True, exist_ok=True)
    (_task_writing_dir / "SKILL.md").write_text("task-writing contract (test stub)\n", encoding="utf-8")

    # An existing supply-interruption streak from a PRIOR, already-resolved
    # increment (no pending decision or hold left standing -- this test is
    # about the supply streak, not open-increment resolution/backoff).
    _oi_state = open_increment.load_state(state_dir)
    _oi_state.consecutive_supply_interrupts = 2
    _oi_state.pending = None
    _oi_state.hold = None
    open_increment._save_state(state_dir, _oi_state)

    class _PlannerRestManager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **_kwargs):
            if self._telemetry_component != "planner":
                return "fake subagent spawned"
            task_id = "planner-rest-01"
            raw_rest = json.dumps({
                "rest": {
                    "wake_condition": {"kind": "main_commit", "ref": ""},
                    "deadline": "2099-01-01T00:00:00Z",
                },
            })
            (state_dir / "subagents").mkdir(parents=True, exist_ok=True)
            (state_dir / "subagents" / f"{task_id}.json").write_text(
                json.dumps({"status": "ok", "result": raw_rest, "context_usage": {"iterations": [{}]}}),
                encoding="utf-8",
            )

            async def _noop():
                return None

            self._running_tasks[task_id] = asyncio.create_task(_noop())
            return "fake planner spawned"

    monkeypatch.setattr(bridge, "SubagentManager", _PlannerRestManager)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    assert open_increment.load_state(state_dir).consecutive_supply_interrupts == 0, (
        "a successful planner rest must reset the supply-interruption streak"
    )


def test_planner_rest_snapshot_checks_returncode(tmp_path: Path, monkeypatch):
    """Small item (ADR-035 Test Contract, #1962): ``planner_rest``'s
    version snapshot must check the returncode of ``git rev-parse``, not
    just its stdout -- a failing rev-parse can still print something to
    stdout (e.g. an ambiguous-ref echo) despite a nonzero exit. Ignoring
    the returncode would store that literal as a "version"; repeating the
    same failed read then looks unchanged forever, holding instead of
    waking on an unreadable input.
    """
    import subprocess

    from nanobot.runtime import planner_rest

    real_run = subprocess.run

    def _fake_failing_rev_parse(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[-2:] == ["rev-parse", "origin/main"]:
            return subprocess.CompletedProcess(cmd, 128, stdout="origin/main\n", stderr="fatal: ambiguous argument\n")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_failing_rev_parse)

    wc = planner_rest.WakeCondition(kind="main_commit", ref="")
    version = planner_rest.snapshot_version(tmp_path, tmp_path, wc)
    assert version is None, (
        f"a failing rev-parse (nonzero returncode) must read as unknown (None), got {version!r}"
    )


# --- keep-work: checkpoint commits excluded from "done work" readers (ADR-035,
# architect addendum, #1942 B2) -----------------------------------------------

def test_checkpoint_commit_excluded_from_self_dedup_and_recent_activity(tmp_path: Path):
    """A checkpoint commit's own subject names the paths it touched -- the
    same shape as real work -- so it must be invisible to every "is this
    already done" git-log reader, or its own text becomes a false
    self_dedup/novelty-pressure match (the #1785 class of defect this
    exclusion mechanism, shared with the residual auto-commit, exists to
    prevent). Drives real git commands against a real repo -- no mocked git
    log parsing -- so a call-site that forgets to pass the shared exclusion
    patterns fails this test rather than merely diverging in production.
    """
    import subprocess

    from tests.test_cycle_ledger import _init_selfevo_repo, _run

    from nanobot.runtime import bridge, llm_proposer
    from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER

    base = tmp_path / "repo"
    base.mkdir()
    _origin, work = _init_selfevo_repo(base)

    target = work / "mod.py"
    target.write_text("def ok():\n    return True\n\n# changed\n", encoding="utf-8")
    _run(work, "add", "mod.py")
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "selfevo: checkpoint — mod.py", "-m", CHECKPOINT_TRAILER],
        check=True, capture_output=True,
    )
    _run(work, "push", "origin", "HEAD:main")

    # self_dedup: the checkpoint commit must never surface as "the latest
    # non-residual commit touching mod.py" -- mod.py's own init commit is
    # the next real commit behind it, so a working exclusion falls through
    # to THAT, never the checkpoint; a broken one would return the
    # checkpoint's own sha/subject instead.
    commit = llm_proposer._latest_non_residual_commit_for_path(work, "mod.py")
    assert commit is not None
    _sha, subject = commit
    assert subject == "init", f"checkpoint commit leaked into self_dedup evidence: {commit}"

    # recent-activity/do-not-repeat window: the checkpoint commit must not
    # appear in the novelty-pressure prompt block at all.
    context = bridge._recent_activity_context(None, work)
    assert "checkpoint" not in context.lower()
    assert "mod.py" not in context

    # The raw exclusion window itself: _recent_commits_with_paths must have
    # dropped the checkpoint commit, not merely hidden it downstream.
    commits = bridge._recent_commits_with_paths(work, since="7 days ago")
    subjects = [subject for _sha, subject, _paths in commits]
    assert not any(s.lower().startswith("selfevo: checkpoint") for s in subjects)


def test_checkpoint_is_not_an_integration(tmp_path: Path):
    """A checkpoint commit is never itself an integration (ADR-035
    keep-work): integration only ever happens through the bridge's own
    explicit, smoke-gated call to ``_integrate_cycle_to_main`` -- never as a
    side effect of a cycle branch merely having commits on it. Two
    complementary checks: the shared exclusion (checkpoint commits are
    indistinguishable from "no real work" to every done-work reader, proven
    above) and a structural guard that ``_integrate_cycle_to_main`` has no
    caller outside the explicit smoke-gated sites -- a call site added
    without that gate would flip this assertion.
    """
    import ast
    import inspect

    from nanobot.runtime import bridge

    source = inspect.getsource(bridge)
    tree = ast.parse(source)
    call_lines = sorted(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == '_integrate_cycle_to_main'
    )
    # Exactly the two live call sites (the single-candidate path and the
    # explore-mode path) -- both gated behind a passed smoke test
    # (`record_gate_decision(..., True, 'smoke_passed', [])` immediately
    # precedes each). A checkpoint commit existing on a branch never reaches
    # either: both require the executor's own run to finish and the smoke
    # gate to pass first. A new call site added without that gate changes
    # this count and must be reviewed, not silently accepted.
    assert len(call_lines) == 2, (
        f"expected exactly two _integrate_cycle_to_main call sites, found {len(call_lines)} at "
        f"lines {call_lines} -- a new one must stay behind an explicit smoke-gated do_integration branch"
    )
    source_lines = source.splitlines()
    for lineno in call_lines:
        preceding = '\n'.join(source_lines[max(0, lineno - 20):lineno])
        assert 'smoke_passed' in preceding, (
            f"_integrate_cycle_to_main call at line {lineno} is not preceded by a "
            "smoke-gate decision within 20 lines -- integration must stay gated, "
            "never triggered by commit presence alone"
        )
