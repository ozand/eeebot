"""Cycle stop-guards — no-progress STOP, bounded revisions, stop conditions.

Implements R11/R12/R13 of ``docs/specs/self-evolving-runtime/spec.md`` as small,
pure functions so the behaviour is unit-testable without importing the heavy
coordinator runtime. The coordinator wires :func:`evaluate_stall` /
:func:`derive_stop_reason` into the experiment snapshot; the subagent bridge
wires :func:`revision_outcome` into its smoke-gate repair loop.

Design input (not a dependency): ``ksimback/looper`` ``loop.yaml``
(``loop_control.no_progress``, ``gates.*.max_revisions``, ``stop_conditions``).
"""
from __future__ import annotations

from typing import Any

# R11: stop the lane once this many consecutive cycles stall.
STALL_THRESHOLD_DEFAULT = 2
# R12: a failed gate may be revised at most this many times before "blocked".
REVISION_CAP_DEFAULT = 3
# R13: a single goal/lane may run at most this many cycles before max_iterations.
MAX_ITERATIONS_DEFAULT = 12

# Map a budget cap key to the matching usage key (R13 budget_<name>).
_BUDGET_CAP_TO_USAGE = {
    "max_requests": ("requests", "requests"),
    "max_tool_calls": ("tool_calls", "tool_calls"),
    "max_subagents": ("subagents", "subagents"),
    "max_timeout_seconds": ("elapsed_seconds", "timeout"),
}

# R13: the only stop reasons a cycle/lane may record.
STOP_REASON_GATE_CLEAN = "gate_clean"
STOP_REASON_MAX_ITERATIONS = "max_iterations"
STOP_REASON_NO_PROGRESS = "no_progress"
STOP_REASONS = frozenset(
    {STOP_REASON_GATE_CLEAN, STOP_REASON_MAX_ITERATIONS, STOP_REASON_NO_PROGRESS}
)  # plus ``budget_<name>`` formed at runtime


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def stall_signal(
    *,
    result_status: str,
    outcome: str,
    metric_current: Any,
    metric_frontier: Any,
    previous_experiment: dict[str, Any] | None,
) -> str | None:
    """Name the observable stall signal for this cycle, or ``None`` if it made progress.

    A cycle is *stalled* (R11) when at least one signal holds: the same blocker
    repeats, the cycle produced no kept change, or the verifier/evaluation result
    is unchanged with no frontier movement.
    """
    outcome = (outcome or "").lower()
    result_status = (result_status or "").upper()

    if outcome in ("blocked", "crash"):
        prev_status = ""
        if isinstance(previous_experiment, dict):
            prev_status = str(previous_experiment.get("result_status") or "").upper()
        if prev_status in ("BLOCK", "ERROR"):
            return "same_blocker_repeats"
        return "no_progress_outcome"

    if outcome == "discard":
        return "discarded_no_keep"

    if outcome == "keep":
        # A kept change is progress only if it moved the frontier or the metric.
        if not isinstance(previous_experiment, dict):
            return None  # first kept result is progress, nothing to compare against
        cur = _to_float(metric_current)
        prev_cur = _to_float(previous_experiment.get("metric_current"))
        front = _to_float(metric_frontier)
        prev_front = _to_float(previous_experiment.get("metric_frontier"))
        if front is not None and prev_front is not None and front > prev_front:
            return None  # frontier advanced
        if cur is not None and prev_cur is not None and cur > prev_cur:
            return None  # current metric advanced
        return "verifier_unchanged"

    return None


def evaluate_stall(
    *,
    result_status: str,
    outcome: str,
    metric_current: Any,
    metric_frontier: Any,
    previous_experiment: dict[str, Any] | None,
    threshold: int = STALL_THRESHOLD_DEFAULT,
) -> dict[str, Any]:
    """Return the stall record for a cycle, carrying the consecutive counter (R11).

    The consecutive counter chains off ``previous_experiment["stall"]["consecutive"]``
    so the runtime needs only the prior snapshot, not the whole history.
    """
    signal = stall_signal(
        result_status=result_status,
        outcome=outcome,
        metric_current=metric_current,
        metric_frontier=metric_frontier,
        previous_experiment=previous_experiment,
    )
    prior = 0
    if isinstance(previous_experiment, dict):
        prev_stall = previous_experiment.get("stall")
        if isinstance(prev_stall, dict):
            try:
                prior = int(prev_stall.get("consecutive") or 0)
            except (TypeError, ValueError):
                prior = 0
    consecutive = prior + 1 if signal else 0
    stop = consecutive >= max(1, int(threshold))
    return {
        "signal": signal,
        "consecutive": consecutive,
        "threshold": int(threshold),
        "stalled": signal is not None,
        "stop": stop,
    }


def derive_stop_reason(
    *,
    outcome: str,
    stall: dict[str, Any] | None,
    budget_exceeded: str | None = None,
    max_iterations_reached: bool = False,
) -> str:
    """Pick the single enumerated stop reason for the cycle/lane (R13).

    Precedence: max_iterations → budget_<name> → no_progress → gate_clean.
    """
    if max_iterations_reached:
        return STOP_REASON_MAX_ITERATIONS
    if budget_exceeded:
        return f"budget_{budget_exceeded}"
    if isinstance(stall, dict) and stall.get("stop"):
        return STOP_REASON_NO_PROGRESS
    return STOP_REASON_GATE_CLEAN


def is_valid_stop_reason(reason: str) -> bool:
    """True if ``reason`` is one of the enumerated values (incl. ``budget_<name>``)."""
    if not reason:
        return False
    if reason in STOP_REASONS:
        return True
    return reason.startswith("budget_") and len(reason) > len("budget_")


def budget_exceeded(
    budget: dict[str, Any] | None,
    budget_used: dict[str, Any] | None,
) -> str | None:
    """Return the name of the first exceeded R2 budget cap, or ``None`` (R13).

    Compares each ``budget_used`` value against its matching cap in ``budget``.
    The returned name (``requests`` / ``tool_calls`` / ``subagents`` /
    ``timeout``) becomes ``stop_reason="budget_<name>"``.
    """
    if not isinstance(budget, dict) or not isinstance(budget_used, dict):
        return None
    for cap_key, (usage_key, name) in _BUDGET_CAP_TO_USAGE.items():
        cap = _to_float(budget.get(cap_key))
        used = _to_float(budget_used.get(usage_key))
        if cap is not None and used is not None and used > cap:
            return name
    return None


def lane_iteration(
    goal_id: str | None,
    previous_experiment: dict[str, Any] | None,
) -> int:
    """Consecutive cycle count for ``goal_id``, chaining off the prior snapshot (R13).

    Resets to 1 whenever the goal changes, so the counter measures how long the
    *current* lane has run — no separate durable counter file is needed.
    """
    prior = 0
    if isinstance(previous_experiment, dict) and previous_experiment.get("goal_id") == goal_id:
        try:
            prior = int(previous_experiment.get("lane_iteration") or 0)
        except (TypeError, ValueError):
            prior = 0
    return prior + 1


def should_switch_lane(previous_experiment: dict[str, Any] | None) -> bool:
    """True when the previous cycle tripped the no-progress stop (R11 enforcement)."""
    if not isinstance(previous_experiment, dict):
        return False
    stall = previous_experiment.get("stall")
    return bool(isinstance(stall, dict) and stall.get("stop"))


def pick_alternative_task(
    current_task_id: str | None,
    tasks: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Pick the first task whose id differs from ``current_task_id`` (R11 enforcement).

    Used to move off a stalled lane. Returns ``None`` when there is no distinct
    alternative — the caller then leaves the lane unchanged (the stall is still
    recorded).
    """
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = task.get("task_id") or task.get("taskId")
        if task_id and task_id != current_task_id:
            return task
    return None


def revision_outcome(
    *,
    revisions: int,
    smoke_passed: bool,
    cap: int = REVISION_CAP_DEFAULT,
) -> dict[str, Any]:
    """Summarise a smoke-gate repair loop for the bridge result artifact (R12).

    Returns a record whose ``outcome`` is ``"blocked"`` once the revision cap is
    reached without passing — revisions are never unbounded.
    """
    cap = max(0, int(cap))
    revisions = max(0, int(revisions))
    capped = (not smoke_passed) and revisions >= cap
    if smoke_passed:
        outcome = "passed"
    elif capped:
        outcome = "blocked"
    else:
        outcome = "unresolved"
    return {
        "gate": "smoke",
        "count": revisions,
        "max": cap,
        "smoke_passed": bool(smoke_passed),
        "capped": capped,
        "outcome": outcome,
    }
