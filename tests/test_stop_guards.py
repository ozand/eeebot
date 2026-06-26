"""Unit tests for cycle stop-guards (R11/R12/R13).

Pure-function tests — no coordinator import, so they run under the CI matrix
regardless of the heavy runtime dependencies.
"""
from nanobot.runtime.stop_guards import (
    MAX_ITERATIONS_DEFAULT,
    REVISION_CAP_DEFAULT,
    STALL_THRESHOLD_DEFAULT,
    budget_exceeded,
    derive_stop_reason,
    evaluate_stall,
    is_valid_stop_reason,
    lane_iteration,
    pick_alternative_task,
    revision_outcome,
    should_switch_lane,
    stall_signal,
)


# ── R11: stall signals ───────────────────────────────────────────────────────

def test_blocked_outcome_is_a_stall():
    assert stall_signal(
        result_status="BLOCK", outcome="blocked",
        metric_current=0.0, metric_frontier=0.0, previous_experiment=None,
    ) == "no_progress_outcome"


def test_repeated_blocker_is_distinct_signal():
    prev = {"result_status": "BLOCK"}
    assert stall_signal(
        result_status="BLOCK", outcome="blocked",
        metric_current=0.0, metric_frontier=0.0, previous_experiment=prev,
    ) == "same_blocker_repeats"


def test_discard_is_a_stall():
    assert stall_signal(
        result_status="PASS", outcome="discard",
        metric_current=0.5, metric_frontier=1.0, previous_experiment=None,
    ) == "discarded_no_keep"


def test_first_keep_is_progress_not_stall():
    assert stall_signal(
        result_status="PASS", outcome="keep",
        metric_current=1.0, metric_frontier=1.0, previous_experiment=None,
    ) is None


def test_keep_with_frontier_advance_is_progress():
    prev = {"metric_current": 1.0, "metric_frontier": 1.0}
    assert stall_signal(
        result_status="PASS", outcome="keep",
        metric_current=1.2, metric_frontier=1.2, previous_experiment=prev,
    ) is None


def test_keep_without_movement_is_stall():
    prev = {"metric_current": 1.0, "metric_frontier": 1.0}
    assert stall_signal(
        result_status="PASS", outcome="keep",
        metric_current=1.0, metric_frontier=1.0, previous_experiment=prev,
    ) == "verifier_unchanged"


# ── R11: consecutive counter + stop ──────────────────────────────────────────

def _blocked_eval(previous):
    return evaluate_stall(
        result_status="BLOCK", outcome="blocked",
        metric_current=0.0, metric_frontier=0.0, previous_experiment=previous,
    )


def test_consecutive_counter_chains_off_previous():
    first = _blocked_eval(None)
    assert first["consecutive"] == 1
    assert first["stalled"] is True
    assert first["stop"] is False  # threshold is 2

    second = _blocked_eval({"result_status": "BLOCK", "stall": first})
    assert second["consecutive"] == 2
    assert second["stop"] is True   # reached default threshold


def test_progress_resets_the_counter():
    first = _blocked_eval(None)
    assert first["consecutive"] == 1
    progressed = evaluate_stall(
        result_status="PASS", outcome="keep",
        metric_current=2.0, metric_frontier=2.0,
        previous_experiment={"metric_current": 1.0, "metric_frontier": 1.0, "stall": first},
    )
    assert progressed["consecutive"] == 0
    assert progressed["stalled"] is False
    assert progressed["stop"] is False


def test_threshold_is_configurable():
    one = evaluate_stall(
        result_status="BLOCK", outcome="blocked",
        metric_current=0.0, metric_frontier=0.0, previous_experiment=None,
        threshold=1,
    )
    assert one["stop"] is True


# ── R13: stop_reason enum ────────────────────────────────────────────────────

def test_stop_reason_no_progress_when_stalled():
    stall = {"stop": True}
    assert derive_stop_reason(outcome="blocked", stall=stall) == "no_progress"


def test_stop_reason_gate_clean_for_normal_cycle():
    stall = {"stop": False}
    assert derive_stop_reason(outcome="keep", stall=stall) == "gate_clean"


def test_stop_reason_budget_takes_precedence_over_gate_clean():
    assert derive_stop_reason(
        outcome="keep", stall={"stop": False}, budget_exceeded="requests",
    ) == "budget_requests"


def test_stop_reason_max_iterations_wins():
    assert derive_stop_reason(
        outcome="blocked", stall={"stop": True},
        budget_exceeded="tokens", max_iterations_reached=True,
    ) == "max_iterations"


def test_is_valid_stop_reason():
    assert is_valid_stop_reason("no_progress")
    assert is_valid_stop_reason("gate_clean")
    assert is_valid_stop_reason("max_iterations")
    assert is_valid_stop_reason("budget_requests")
    assert not is_valid_stop_reason("budget_")
    assert not is_valid_stop_reason("")
    assert not is_valid_stop_reason("nonsense")


# ── R12: bounded revisions ───────────────────────────────────────────────────

def test_revision_passed():
    rec = revision_outcome(revisions=1, smoke_passed=True, cap=3)
    assert rec["outcome"] == "passed"
    assert rec["capped"] is False
    assert rec["count"] == 1


def test_revision_cap_reached_blocks():
    rec = revision_outcome(revisions=3, smoke_passed=False, cap=3)
    assert rec["outcome"] == "blocked"
    assert rec["capped"] is True
    assert rec["max"] == 3
    assert rec["gate"] == "smoke"


def test_revision_under_cap_is_unresolved_not_blocked():
    rec = revision_outcome(revisions=1, smoke_passed=False, cap=3)
    assert rec["outcome"] == "unresolved"
    assert rec["capped"] is False


def test_default_constants():
    assert STALL_THRESHOLD_DEFAULT == 2
    assert REVISION_CAP_DEFAULT == 3
    assert MAX_ITERATIONS_DEFAULT == 12


# ── R13: budget_exceeded ─────────────────────────────────────────────────────

_CAPS = {"max_requests": 2, "max_tool_calls": 12, "max_subagents": 2, "max_timeout_seconds": 900}


def test_budget_within_caps_is_none():
    used = {"requests": 1, "tool_calls": 4, "subagents": 1, "elapsed_seconds": 100}
    assert budget_exceeded(_CAPS, used) is None


def test_budget_requests_exceeded():
    used = {"requests": 3, "tool_calls": 4, "subagents": 1, "elapsed_seconds": 100}
    assert budget_exceeded(_CAPS, used) == "requests"


def test_budget_timeout_exceeded_maps_to_timeout():
    used = {"requests": 1, "tool_calls": 4, "subagents": 1, "elapsed_seconds": 1200}
    assert budget_exceeded(_CAPS, used) == "timeout"


def test_budget_exceeded_drives_stop_reason():
    used = {"requests": 99, "elapsed_seconds": 0}
    name = budget_exceeded(_CAPS, used)
    assert derive_stop_reason(outcome="keep", stall={"stop": False}, budget_exceeded=name) == "budget_requests"


def test_budget_handles_missing_dicts():
    assert budget_exceeded(None, {"requests": 9}) is None
    assert budget_exceeded(_CAPS, None) is None


# ── R13: lane_iteration ──────────────────────────────────────────────────────

def test_lane_iteration_starts_at_one():
    assert lane_iteration("goal-a", None) == 1
    assert lane_iteration("goal-a", {"goal_id": "goal-b", "lane_iteration": 5}) == 1


def test_lane_iteration_increments_same_goal():
    prev = {"goal_id": "goal-a", "lane_iteration": 4}
    assert lane_iteration("goal-a", prev) == 5


def test_lane_iteration_reaches_max_drives_stop_reason():
    prev = {"goal_id": "goal-a", "lane_iteration": MAX_ITERATIONS_DEFAULT - 1}
    n = lane_iteration("goal-a", prev)
    assert n == MAX_ITERATIONS_DEFAULT
    reason = derive_stop_reason(
        outcome="keep", stall={"stop": False},
        max_iterations_reached=n >= MAX_ITERATIONS_DEFAULT,
    )
    assert reason == "max_iterations"


# ── R11 enforcement: should_switch_lane + pick_alternative_task ───────────────

def test_should_switch_lane_true_when_prev_stopped():
    assert should_switch_lane({"stall": {"stop": True}}) is True


def test_should_switch_lane_false_otherwise():
    assert should_switch_lane({"stall": {"stop": False}}) is False
    assert should_switch_lane({}) is False
    assert should_switch_lane(None) is False


def test_pick_alternative_task_skips_current():
    tasks = [{"task_id": "t1"}, {"task_id": "t2", "title": "Second"}]
    assert pick_alternative_task("t1", tasks) == {"task_id": "t2", "title": "Second"}


def test_pick_alternative_task_none_when_only_current():
    assert pick_alternative_task("t1", [{"task_id": "t1"}]) is None
    assert pick_alternative_task("t1", []) is None
    assert pick_alternative_task("t1", None) is None
