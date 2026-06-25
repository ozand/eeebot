"""Unit tests for cycle stop-guards (R11/R12/R13).

Pure-function tests — no coordinator import, so they run under the CI matrix
regardless of the heavy runtime dependencies.
"""
from nanobot.runtime.stop_guards import (
    REVISION_CAP_DEFAULT,
    STALL_THRESHOLD_DEFAULT,
    derive_stop_reason,
    evaluate_stall,
    is_valid_stop_reason,
    revision_outcome,
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
