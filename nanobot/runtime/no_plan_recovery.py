"""ADR-035 rule 1: bounded, visible ``no_plan`` recovery.

Every planning-session outcome (:mod:`nanobot.runtime.bridge`'s
``_run_planning_session``, logged via ``cycle_ledger.record_planning_session``)
falls into one of three buckets:

- a plan was produced (``integrated``) -- resets recovery entirely;
- the model call did not complete -- **supply** family (timeout, 503, queue;
  today's only such outcome is ``timed_out``);
- the model answered without a usable plan -- **planner** family (``refused``,
  ``malformed``, and the ledger's own ``no_plan`` outcome, which already means
  "no final response" -- see ``bridge._run_planning_session``'s
  ``bounded_stop``/``iteration_budget_no_final`` reasons);
- infrastructure failures (``spawn_failed``, ``commit_failed``) are neither --
  ADR-035's Decision section is explicit that "infrastructure recovery --
  model endpoint, deploy rollback, unit health -- is not a cycle's work and
  never waited on a plan; it stays with the harness processes that own it
  today." These outcomes leave the counters untouched.

This module owns the bounded state machine over those two families:

- 3 consecutive **planner**-family -> ``planner_degraded`` + minimal mode.
- 6 consecutive **planner**-family -> ``stopped`` (the caller must stop
  starting cycles until an operator :func:`resume`).
- 6 consecutive **supply**-family -> ``model_supply_degraded``. Supply never
  triggers minimal mode (a shorter prompt does not shorten a queue) and never
  stops cycles on its own.
- Any ``integrated`` outcome resets both counters and every degraded/stopped
  flag.

Every threshold crossing (and the reset) is logged via
``cycle_ledger.record_no_plan_recovery_transition`` -- "the ledger records
every transition" (ADR-035 rule 1).

**No path here ever selects a candidate.** :class:`RecoveryState` carries no
task/candidate field, and no function in this module returns one --
`no_plan` recovery is diagnosis and gating, never assignment (rule 1's
"a silent fallback to assignment would make rule 1 optional in exactly the
cycles where it matters").
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Consecutive planner-family no_plan cycles before minimal mode.
PLANNER_DEGRADED_THRESHOLD = 3
#: Consecutive planner-family no_plan cycles before the bridge stops
#: starting cycles.
PLANNER_STOP_THRESHOLD = 6
#: Consecutive supply-family no_plan cycles before model_supply_degraded.
SUPPLY_DEGRADED_THRESHOLD = 6

#: Planning-session ledger outcomes (``cycle_ledger.VALID_PLANNING_OUTCOMES``)
#: that count toward one of the two no_plan families. Outcomes absent from
#: this mapping (``spawn_failed``, ``commit_failed``) are infrastructure
#: failures the ADR explicitly carves out -- see module docstring.
CAUSE_FAMILY: dict[str, str] = {
    "timed_out": "supply",
    "refused": "planner",
    "malformed": "planner",
    "no_plan": "planner",
    # D10 (ADR-035 Test Contract, external review finding #10): a terminal
    # supplier error in the planner's OWN telemetry (its LLM call itself
    # failed -- no final answer to even attempt parsing) is supply family,
    # same as a wall-clock timeout -- never planner family, which drives
    # stopped/minimal_mode for a problem that is not the planner's fault.
    "supplier_error": "supply",
}

_STATE_RELPATH = ("planner", "no_plan_recovery.json")
_SCHEMA = "no-plan-recovery-v1"


def classify(outcome: str) -> "str | None":
    """Return ``"supply"``, ``"planner"``, or ``None`` (success or an
    infrastructure outcome that never counts toward either family)."""
    return CAUSE_FAMILY.get(outcome)


@dataclass
class RecoveryState:
    consecutive_supply: int = 0
    consecutive_planner: int = 0
    minimal_mode: bool = False
    stopped: bool = False
    planner_degraded: bool = False
    model_supply_degraded: bool = False
    #: Hypothesis ids with a new verdict that minimal mode deferred, oldest
    #: first -- carried forward until a full session handles them
    #: (ADR-035 rule 1: "their count is carried and shown").
    deferred_verdict_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _state_path(state_dir: "Path") -> "Path":
    return Path(state_dir).joinpath(*_STATE_RELPATH)


def load_state(state_dir: "Path") -> RecoveryState:
    path = _state_path(state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return RecoveryState()
    known = {f for f in RecoveryState.__dataclass_fields__}
    kwargs = {k: v for k, v in raw.items() if k in known}
    try:
        return RecoveryState(**kwargs)
    except Exception:
        return RecoveryState()


def _save_state(state_dir: "Path", state: RecoveryState) -> None:
    path = _state_path(state_dir)
    payload = {"schema": _SCHEMA, **state.as_dict()}
    tmp_name: "str | None" = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as fh:
            tmp_name = fh.name
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
        tmp_name = None
    except Exception:
        pass
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def record_outcome(state_dir: "Path", cycle_id: str, outcome: str) -> RecoveryState:
    """Apply one planning-session outcome to the recovery state machine.

    Call this once per planning session, right after
    ``cycle_ledger.record_planning_session`` (same outcome string).  Returns
    the resulting :class:`RecoveryState`; the caller consults ``.stopped`` to
    decide whether to skip starting the cycle, and ``.minimal_mode`` to
    decide which context to give the next planning session.
    """
    from nanobot.runtime.cycle_ledger import record_no_plan_recovery_transition

    state = load_state(state_dir)

    if outcome == "integrated":
        if state.consecutive_supply or state.consecutive_planner or state.stopped or state.minimal_mode:
            record_no_plan_recovery_transition(state_dir, cycle_id, "planner", 0, "reset")
        state = RecoveryState(deferred_verdict_ids=state.deferred_verdict_ids)
        _save_state(state_dir, state)
        return state

    family = classify(outcome)
    if family is None:
        # Infrastructure failure: not this state machine's concern.
        return state

    if family == "supply":
        state.consecutive_supply += 1
        if state.consecutive_supply >= SUPPLY_DEGRADED_THRESHOLD and not state.model_supply_degraded:
            state.model_supply_degraded = True
            record_no_plan_recovery_transition(
                state_dir, cycle_id, "supply", state.consecutive_supply, "model_supply_degraded",
            )
    else:
        state.consecutive_planner += 1
        if state.consecutive_planner >= PLANNER_STOP_THRESHOLD and not state.stopped:
            state.stopped = True
            record_no_plan_recovery_transition(
                state_dir, cycle_id, "planner", state.consecutive_planner, "stopped",
            )
        elif state.consecutive_planner >= PLANNER_DEGRADED_THRESHOLD and not state.planner_degraded:
            state.planner_degraded = True
            state.minimal_mode = True
            record_no_plan_recovery_transition(
                state_dir, cycle_id, "planner", state.consecutive_planner, "planner_degraded",
            )

    _save_state(state_dir, state)
    return state


def resume(state_dir: "Path", cycle_id: str) -> RecoveryState:
    """Operator resume (ADR-035 rule 1, point 3): clears ``stopped`` only.

    Counters and ``minimal_mode``/``planner_degraded`` are untouched -- they
    only reset on a produced plan (:func:`record_outcome` with
    ``outcome="integrated"``), so a resumed session still runs in minimal
    mode until it actually produces a plan.
    """
    from nanobot.runtime.cycle_ledger import record_no_plan_recovery_transition

    state = load_state(state_dir)
    if state.stopped:
        state.stopped = False
        _save_state(state_dir, state)
        record_no_plan_recovery_transition(state_dir, cycle_id, "planner", state.consecutive_planner, "normal")
    return state


def defer_or_bound_verdicts(
    state_dir: "Path", new_verdict_ids: list[str], *, limit: int = 5,
) -> tuple[list[str], int]:
    """ADR-035 rule 3's backlog bound, aware of minimal mode (rule 1's
    "the duty to handle new verdicts is deferred, not dropped").

    In minimal mode, every id in ``new_verdict_ids`` is appended to the
    carried ``deferred_verdict_ids`` and NONE are handled this session.
    Otherwise, the carried deferred ids plus ``new_verdict_ids`` (oldest
    first) are handled up to ``limit``; the remainder stays carried.

    Returns ``(handle_now_ids, deferred_count)``. Never returns a selection
    among them -- which ids to act on first, beyond "oldest", is the
    planning session's call, not this function's.
    """
    state = load_state(state_dir)
    pending = list(state.deferred_verdict_ids) + [v for v in new_verdict_ids if v not in state.deferred_verdict_ids]

    if state.minimal_mode:
        state.deferred_verdict_ids = pending
        _save_state(state_dir, state)
        return [], len(pending)

    handle_now = pending[:limit]
    remaining = pending[limit:]
    state.deferred_verdict_ids = remaining
    _save_state(state_dir, state)
    return handle_now, len(remaining)
