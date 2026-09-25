"""ADR-035 rule 3 (#1942) / ADR-031 rule 2, architect resolution 2026-09-25:
the open increment a supply-family interruption leaves behind.

A plan interrupted by a model-call error or a supplier pause during
execution (:func:`nanobot.runtime.bridge._classify_llm_error` returning
``"paused-supplier"``) is not retried and not queued (that queue is
retired -- ADR-035 rule 1). The harness records it as an **open increment**
with reason ``interrupted_supply`` and holds further sessions -- exactly
like :mod:`nanobot.runtime.planner_rest`'s pre-check (#1964): a snapshot of
one named input plus a deadline, "tests change, never value". This module
reuses :func:`planner_rest.snapshot_version`/``WakeCondition`` for that
version check, but keeps its OWN state file and OWN counters -- the
architect's resolution is explicit that held ticks here count apart from
both ``planner_rest`` (voluntary rest) and ``no_plan_recovery`` (the
planning session's own no-plan bound): this hold is the harness reacting to
an EXECUTION-time supply failure, not a planner decision.

Deadline is a doubling backoff: :data:`SUPPLY_RETRY_COOLDOWN_BASE_SECONDS`
(15 minutes) times two per CONSECUTIVE ``interrupted_supply`` outcome for
the same open increment (15 -> 30 -> 60), capped at
:data:`SUPPLY_RETRY_COOLDOWN_CAP_SECONDS` (60 minutes) -- architect addendum
2026-09-25: a fixed 15-minute retry would hammer a queue busy for hours.
:func:`record_model_call_completed` resets the streak (and so the backoff)
on ANY model call that finishes without a supply classification, whether it
produced a plan, failed for our own reasons, or came from the executor or
the planner -- proof the supplier is reachable again. Any change to the
snapshotted input, an unreadable input, or a passed deadline also lets the
next session run -- :func:`precheck` never judges whether the supplier is
actually healthy again; the session itself finds that out.

The pending open increment (:func:`pending_open_increment`) is the next
planning session's first item; the session must resolve it via
:func:`resolve` with one of ``keep``/``edit``/``delete`` (ADR-035 rule 3,
"I": "for its own previous plan: keep, edit or delete"). Three consecutive
``interrupted_supply`` outcomes for the SAME open increment raise an
operator-facing flag that :func:`resolve` never clears -- only
:func:`operator_clear_flag` does (mirrors
``no_plan_recovery.stopped``/``resume``).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime.planner_rest import WakeCondition, _parse_deadline, snapshot_version

_STATE_RELPATH = ("planner", "open_increment.json")
_SCHEMA = "open-increment-v1"

#: Base cooldown before the harness first lets a session test supplier
#: recovery after an ``interrupted_supply`` outcome; doubles per consecutive
#: interruption for the SAME open increment. Overridable for operators/tests.
SUPPLY_RETRY_COOLDOWN_BASE_SECONDS = int(
    os.environ.get("SUBAGENT_BRIDGE_SUPPLY_RETRY_COOLDOWN_BASE_SECONDS", str(15 * 60))
)
#: Backoff ceiling -- a queue that stays busy for hours must not push the
#: retry interval past this.
SUPPLY_RETRY_COOLDOWN_CAP_SECONDS = int(
    os.environ.get("SUBAGENT_BRIDGE_SUPPLY_RETRY_COOLDOWN_CAP_SECONDS", str(60 * 60))
)


def _backoff_seconds(consecutive: int, *, base: int, cap: int) -> int:
    """15 -> 30 -> 60 (minutes, at the defaults): base * 2**(consecutive-1),
    capped. ``consecutive`` is 1-indexed (the interruption just recorded)."""
    return min(base * (2 ** max(consecutive - 1, 0)), cap)

#: Consecutive interrupted_supply outcomes for the SAME open increment
#: (same retry_key) before the non-auto-clearing operator flag is raised.
OPERATOR_FLAG_THRESHOLD = 3

_VALID_DECISIONS = frozenset({"keep", "edit", "delete"})


class OpenIncrementDecisionError(ValueError):
    """An unrecognized keep/edit/delete decision was passed to :func:`resolve`."""


@dataclass
class OpenIncrementState:
    #: The interrupted plan itself, or None when nothing is pending.
    pending: "dict[str, Any] | None" = None
    #: The active precheck hold ({"wake_condition": ..., "deadline": ..., "snapshot": ...}), or None.
    hold: "dict[str, Any] | None" = None
    #: Ticks the precheck held on (no session at all) since the last one ran -- its own counter.
    held_ticks: int = 0
    #: Consecutive interrupted_supply outcomes for `pending`'s retry_key.
    consecutive_supply_interrupts: int = 0
    #: Non-auto-clearing operator-facing flag (architect resolution point 3).
    operator_flagged: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _state_path(state_dir: "Path") -> "Path":
    return Path(state_dir).joinpath(*_STATE_RELPATH)


def load_state(state_dir: "Path") -> OpenIncrementState:
    path = _state_path(state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return OpenIncrementState()
    known = set(OpenIncrementState.__dataclass_fields__)
    try:
        return OpenIncrementState(**{k: v for k, v in raw.items() if k in known})
    except Exception:
        return OpenIncrementState()


def _save_state(state_dir: "Path", state: OpenIncrementState) -> None:
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_supply_interruption(
    state_dir: "Path",
    cycle_id: str,
    *,
    retry_key: str,
    plan_text: str,
    candidate_id: "str | None",
    selfevo_repo: "Path | None" = None,
    wake_condition: "WakeCondition | None" = None,
    cooldown_base_seconds: int = SUPPLY_RETRY_COOLDOWN_BASE_SECONDS,
    cooldown_cap_seconds: int = SUPPLY_RETRY_COOLDOWN_CAP_SECONDS,
) -> OpenIncrementState:
    """The executor's LLM call died on a supplier-side classification
    (``paused-supplier``) mid-plan. Records the interrupted plan as the
    pending open increment (``reason: interrupted_supply``) and starts a
    rest-shaped hold on the harness's OWN counters -- never touching
    :mod:`planner_rest` or :mod:`no_plan_recovery`'s state.

    ``retry_key`` ties consecutive interruptions of the SAME underlying
    increment together (see ``bridge._retry_key_for``); a different
    retry_key restarts the streak at 1, matching the bounded-retry
    mechanism's own per-candidate keying.
    """
    from nanobot.runtime.cycle_ledger import append_event

    state = load_state(state_dir)
    same_increment = bool(state.pending) and state.pending.get("retry_key") == retry_key
    consecutive = (state.consecutive_supply_interrupts + 1) if same_increment else 1

    state.pending = {
        "retry_key": retry_key,
        "cycle_id": cycle_id or "",
        "plan_text": plan_text,
        "candidate_id": candidate_id or None,
        "reason": "interrupted_supply",
        "interrupted_at": _now_iso(),
    }
    state.consecutive_supply_interrupts = consecutive

    escalated = consecutive >= OPERATOR_FLAG_THRESHOLD and not state.operator_flagged
    if escalated:
        state.operator_flagged = True

    wc = wake_condition or WakeCondition(kind="main_commit")
    cooldown_seconds = _backoff_seconds(consecutive, base=cooldown_base_seconds, cap=cooldown_cap_seconds)
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = snapshot_version(state_dir, selfevo_repo, wc)
    state.hold = {
        "wake_condition": {"kind": wc.kind, "ref": wc.ref},
        "deadline": deadline,
        "snapshot": snapshot,
    }
    state.held_ticks = 0
    _save_state(state_dir, state)

    append_event(state_dir, {
        "phase": "open_increment",
        "cycle_id": cycle_id or "",
        "status": "interrupted_supply",
        "retry_key": retry_key,
        "consecutive_supply_interrupts": consecutive,
        "cooldown_seconds": cooldown_seconds,
        "deadline": deadline,
    })
    if escalated:
        append_event(state_dir, {
            "phase": "open_increment_operator_flag",
            "cycle_id": cycle_id or "",
            "retry_key": retry_key,
            "consecutive_supply_interrupts": consecutive,
            "note": (
                f"{consecutive} consecutive supply interruptions for the same open "
                "increment -- flagged for the operator; never auto-clears"
            ),
        })
    return state


def record_model_call_completed(state_dir: "Path") -> OpenIncrementState:
    """A model call (executor or planner) finished WITHOUT a supply
    classification -- proof the supplier is reachable, whatever the call's
    own content turned out to be. Resets the consecutive-interruption
    streak (and so the backoff) for the NEXT ``interrupted_supply``, if
    any; does not touch ``pending``/``hold``/``operator_flagged``, which
    only :func:`resolve`/:func:`operator_clear_flag` change. A cheap no-op
    write when the streak is already 0."""
    state = load_state(state_dir)
    if state.consecutive_supply_interrupts:
        state.consecutive_supply_interrupts = 0
        _save_state(state_dir, state)
    return state


def precheck(state_dir: "Path", selfevo_repo: "Path | None") -> "tuple[bool, str]":
    """Should the harness run a real session (and repository preparation,
    and create a provider) this tick? Same slot and same version-only shape
    as :func:`planner_rest.precheck` -- called alongside it, on separate
    state. No active hold -> run. Deadline passed / input changed / input
    unreadable -> run (the session itself tests supplier recovery). Else
    hold."""
    state = load_state(state_dir)
    hold = state.hold
    if not hold:
        return True, "no_active_hold"
    try:
        wc = WakeCondition(kind=hold["wake_condition"]["kind"], ref=hold["wake_condition"].get("ref", ""))
        deadline = hold["deadline"]
        snapshot = hold.get("snapshot")
    except Exception:
        return True, "corrupt_hold_state"
    try:
        if datetime.now(timezone.utc) >= _parse_deadline(deadline):
            return True, "deadline_passed"
    except Exception:
        return True, "corrupt_deadline"
    current = snapshot_version(state_dir, selfevo_repo, wc)
    if current is None:
        return True, "input_unreadable"
    if current != snapshot:
        return True, "input_changed"
    return False, "unchanged"


def render_open_increment_block(pending: "dict[str, Any] | None", consecutive_supply_interrupts: int) -> str:
    """The planning session's FIRST context block when an open increment is
    pending (ADR-035 rule 3, architect resolution 2026-09-25) -- empty
    string when there is none. Mandates a keep/edit/delete decision via
    ``open_increment_decision`` in the plan's final JSON before anything
    else, so the session cannot silently plan past an unresolved
    interruption."""
    if not pending:
        return ""
    return (
        "## Open increment -- interrupted (supply), decide first\n"
        "A prior plan was interrupted by a model-call/supplier failure "
        "(#1765) and never completed:\n\n"
        f"{pending.get('plan_text', '')}\n\n"
        f"Interrupted at: {pending.get('interrupted_at', '')} "
        f"(consecutive supply interruptions: {consecutive_supply_interrupts}).\n"
        "You must decide `keep`, `edit`, or `delete` for this open increment "
        "before planning anything else -- set `open_increment_decision` to one "
        "of those three values in your final plan JSON."
    )


def record_held_tick(state_dir: "Path", cycle_id: str) -> OpenIncrementState:
    """The precheck held this tick -- no session, no repository
    preparation, no provider. Counted on this module's own
    ``held_ticks`` -- never ``planner_rest.held_ticks_since_last_session``
    or any ``no_plan_recovery`` counter (architect resolution)."""
    from nanobot.runtime.cycle_ledger import append_event

    state = load_state(state_dir)
    state.held_ticks += 1
    _save_state(state_dir, state)
    append_event(state_dir, {
        "phase": "open_increment_held",
        "cycle_id": cycle_id or "",
        "held_ticks": state.held_ticks,
    })
    return state


def pending_open_increment(state_dir: "Path") -> "dict[str, Any] | None":
    """The unresolved open increment, if any -- the next planning session's
    first item, marked ``interrupted_supply``."""
    return load_state(state_dir).pending


def resolve(state_dir: "Path", cycle_id: str, decision: str, *, reason: str = "") -> OpenIncrementState:
    """The planning session's keep/edit/delete decision on the pending open
    increment (ADR-035 rule 3, "I"). Clears ``pending``/``hold``/
    ``held_ticks`` and resets the consecutive-interruption streak to 0 --
    but NEVER clears ``operator_flagged``; only :func:`operator_clear_flag`
    does (mirrors ``no_plan_recovery.resume`` leaving ``planner_degraded``
    untouched)."""
    from nanobot.runtime.cycle_ledger import append_event

    if decision not in _VALID_DECISIONS:
        raise OpenIncrementDecisionError(
            f"open-increment decision must be one of {sorted(_VALID_DECISIONS)}, got {decision!r}"
        )
    state = load_state(state_dir)
    retry_key = (state.pending or {}).get("retry_key", "")
    state.pending = None
    state.hold = None
    state.held_ticks = 0
    state.consecutive_supply_interrupts = 0
    _save_state(state_dir, state)
    append_event(state_dir, {
        "phase": "open_increment_resolved",
        "cycle_id": cycle_id or "",
        "retry_key": retry_key,
        "decision": decision,
        "reason": reason or None,
    })
    return state


def operator_clear_flag(state_dir: "Path", cycle_id: str) -> OpenIncrementState:
    """Operator action clearing :data:`OpenIncrementState.operator_flagged`
    (mirrors ``no_plan_recovery.resume``)."""
    from nanobot.runtime.cycle_ledger import append_event

    state = load_state(state_dir)
    if state.operator_flagged:
        state.operator_flagged = False
        _save_state(state_dir, state)
        append_event(state_dir, {"phase": "open_increment_operator_flag_cleared", "cycle_id": cycle_id or ""})
    return state
