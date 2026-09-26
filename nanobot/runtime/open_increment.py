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
    #: D2 (ADR-035 Test Contract, #1942 B2): the in-flight attempt
    #: registration ({"cycle_id", "branch", "attempt", "started_at",
    #: "resumed_by"}), written BEFORE the executor can be killed and
    #: cleared ONLY by a terminal outcome (record_attempt_finished) --
    #: never by resolve()/keep, which only annotates it. A registration
    #: still standing for a DIFFERENT cycle_id than the one about to run
    #: is exactly the durable proof a hard kill needs: the process that
    #: wrote it died before reaching a terminal outcome.
    running: "dict[str, Any] | None" = None

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
    branch: str = "",
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

    ``branch`` (ADR-035 keep-work, #1942 B2) is the cycle branch
    (``selfevo/cycle-<cycle_id>``) the interrupted attempt's checkpoint
    commits live on -- the caller already computed it via
    ``bridge._setup_cycle_branch`` and passes it straight through, so this
    module never needs its own branch-naming logic. A *keep* decision
    (:func:`resolve`) reads it back before clearing ``pending`` so the next
    executor spawn can resume the SAME branch instead of branching a fresh
    one off ``origin/main``. ``opening_entry_written`` is always set True
    here: the interrupted attempt's own opening diary entry was already
    written before it started executing (ADR-028 rule 2), so a *keep*
    resume must never write a second one for the same increment.
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
        "branch": branch or "",
        "opening_entry_written": True,
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
    increment (ADR-035 rule 3, "I").

    ``edit``/``delete`` clear ``pending``/``hold``/``held_ticks`` and reset
    the consecutive-interruption streak to 0, same as before B2 -- but
    NEVER clear ``operator_flagged``; only :func:`operator_clear_flag` does
    (mirrors ``no_plan_recovery.resume`` leaving ``planner_degraded``
    untouched).

    D2 (ADR-035 Test Contract, #1942 B2): ``keep`` does NOT clear
    ``pending`` -- it is the only durable proof this increment still needs
    a terminal outcome. Clearing it here, before the resumed attempt has
    even re-registered itself (:func:`record_attempt_started`), would make
    a kill in that exact hand-off gap invisible to the next cycle -- the
    77ca scenario B2 exists for. ``pending`` is instead annotated
    ``resumed_by`` with the deciding cycle; only :func:`record_attempt_finished`
    (a REAL terminal outcome for this increment's ``cycle_id``) removes it.
    """
    from nanobot.runtime.cycle_ledger import append_event

    if decision not in _VALID_DECISIONS:
        raise OpenIncrementDecisionError(
            f"open-increment decision must be one of {sorted(_VALID_DECISIONS)}, got {decision!r}"
        )
    state = load_state(state_dir)
    retry_key = (state.pending or {}).get("retry_key", "")
    if decision == "keep":
        if state.pending is not None:
            state.pending = {**state.pending, "resumed_by": cycle_id or ""}
        state.hold = None
        state.held_ticks = 0
        state.consecutive_supply_interrupts = 0
    else:
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


# --- D2 (ADR-035 Test Contract, #1942 B2): register before the execution
# can be killed ---------------------------------------------------------


def record_attempt_started(
    state_dir: "Path", cycle_id: str, branch: str, attempt: int = 1,
) -> OpenIncrementState:
    """Register the in-flight attempt BEFORE the executor can be killed --
    called once cycle-branch setup succeeds, before the subagent spawns.
    This is the durable proof a kill needs: if the process dies before a
    terminal outcome is ever recorded for ``cycle_id``
    (:func:`record_attempt_finished`, wired into
    :func:`nanobot.runtime.cycle_ledger.record_cycle_outcome` so every
    exit path is covered), the next cycle's :func:`check_running_for_kill`
    finds this registration still standing and converts it into a pending
    open increment (``reason: interrupted_kill``) instead of losing the
    attempt silently. ``task_id`` starts unset -- the subagent's own id
    does not exist yet at this point; see :func:`record_attempt_task_id`."""
    from nanobot.runtime.cycle_ledger import append_event

    state = load_state(state_dir)
    state.running = {
        "cycle_id": cycle_id or "",
        "branch": branch or "",
        "attempt": int(attempt),
        "started_at": _now_iso(),
        "resumed_by": None,
        "task_id": None,
    }
    _save_state(state_dir, state)
    append_event(state_dir, {
        "phase": "open_increment_running_registered",
        "cycle_id": cycle_id or "",
        "branch": branch or "",
        "attempt": int(attempt),
    })
    return state


def record_attempt_task_id(state_dir: "Path", cycle_id: str, task_id: str) -> OpenIncrementState:
    """Attach the subagent's own telemetry ``task_id`` to the running
    registration once it exists (right after ``spawn()`` returns -- the id
    is minted inside it, so it cannot be known at
    :func:`record_attempt_started` time). A kill in the narrow window
    before this call leaves ``task_id`` unset, same as no telemetry at
    all -- :func:`check_running_for_kill` treats both identically. Only
    updates the registration when it still belongs to THIS ``cycle_id``,
    so a race with a concurrent, already-superseded registration can
    never attach the wrong task_id."""
    state = load_state(state_dir)
    if state.running and state.running.get("cycle_id") == (cycle_id or ""):
        state.running["task_id"] = task_id
        _save_state(state_dir, state)
    return state


def _read_executor_terminal_status(state_dir: "Path", task_id: str) -> "str | None":
    """The executor's own terminal telemetry ``status`` for ``task_id``
    (``ok``/``bounded_stop``/``blocked``/``cancelled``/``error``/``running``),
    or ``None`` when there is no task_id yet (a kill before
    :func:`record_attempt_task_id` ran) or its telemetry file is
    unreadable/absent (a kill before the executor wrote ANY row). Both
    ``None`` cases are equally "no evidence the executor ever finished" --
    :func:`check_running_for_kill` treats them the same as any other
    non-error status.

    Delegates to :func:`nanobot.runtime.bridge._subagent_own_status`
    (#1546) rather than re-reading the same telemetry file with its own
    parsing -- the asymmetric-readers class of defect (two call sites
    independently reading the same "did the subagent finish" question,
    one of them drifting) is exactly what a second, divergent
    implementation here would risk."""
    if not task_id:
        return None
    from nanobot.runtime.bridge import _subagent_own_status

    return _subagent_own_status(state_dir, task_id) or None


def record_attempt_finished(state_dir: "Path", cycle_id: str) -> OpenIncrementState:
    """A terminal outcome was recorded for ``cycle_id``
    (:func:`nanobot.runtime.cycle_ledger.record_cycle_outcome` calls this
    for every cycle, so this is the ONE place that clears the running
    registration and any pending increment for the SAME lineage --
    whatever the many outcome-writing call sites upstream are). A
    registration/pending record for a DIFFERENT, not-yet-resolved
    cycle_id is left untouched; only its own terminal outcome removes
    it."""
    state = load_state(state_dir)
    changed = False
    if state.running and state.running.get("cycle_id") == (cycle_id or ""):
        state.running = None
        changed = True
    if state.pending and state.pending.get("cycle_id") == (cycle_id or ""):
        state.pending = None
        state.hold = None
        state.held_ticks = 0
        state.consecutive_supply_interrupts = 0
        changed = True
    if changed:
        _save_state(state_dir, state)
    return state


def check_running_for_kill(state_dir: "Path", current_cycle_id: str) -> "dict[str, Any] | None":
    """Called once per bridge tick, before THIS tick's own attempt is
    registered (and before the planning session reads
    :func:`pending_open_increment`, so a detected kill is surfaced to it
    like any other open increment). Returns the stale running record
    (with an added ``executor_status`` key -- see
    :func:`_read_executor_terminal_status`) when it belongs to a
    DIFFERENT cycle_id than the one about to run -- proof the process
    that wrote it died before a terminal outcome -- or ``None`` when
    there is nothing stale, or nothing THIS function should classify.

    Full mapping (architect resolution, #1979 external-review followups):
    the executor's own terminal telemetry status, whatever it is, does
    NOT by itself mean the cycle finished -- the GATE (smoke tests, the
    integration decision) never rendered a verdict for this cycle_id
    either way, or :func:`record_attempt_finished` would have cleared
    this registration. ``ok``/``bounded_stop``/``blocked``/``cancelled``,
    or no telemetry at all (unwritten, or ``task_id`` never attached) --
    all become ``interrupted_kill`` here. The ONE exception is
    ``error``: a telemetry-confirmed executor error is classified as
    supply/defect by the existing LLM-error classifier, synchronously,
    in the SAME tick it happened -- this function must never
    re-classify it as a kill. In practice that classification already
    guards this: it sets ``pending`` before this tick ends, so the
    ``state.pending`` check below already skips it on the next tick. The
    explicit ``status == "error"`` check here is a second, independent
    guard for the narrower race where the executor wrote its ``error``
    row but the process died before the classifier ran -- ``running`` is
    left standing for that case, on purpose, exactly like any other
    not-yet-classified attempt, rather than being converted to
    ``interrupted_kill`` out from under the classifier that owns it."""
    state = load_state(state_dir)
    if state.pending:
        return None
    running = state.running
    if not running or running.get("cycle_id") == (current_cycle_id or ""):
        return None
    executor_status = _read_executor_terminal_status(state_dir, running.get("task_id") or "")
    if executor_status == "error":
        return None
    return {**running, "executor_status": executor_status}


def record_kill_interruption(
    state_dir: "Path",
    cycle_id: str,
    *,
    retry_key: str,
    plan_text: str,
    candidate_id: "str | None",
    branch: str = "",
    executor_status: "str | None" = None,
) -> OpenIncrementState:
    """The running-attempt registration for ``cycle_id`` (see
    :func:`record_attempt_started`) had no terminal outcome by the time a
    fresh cycle started -- a hard kill, or an exact imitation of one.
    Recorded as the pending open increment with ``reason:
    interrupted_kill`` -- same keep/edit/delete contract as
    ``interrupted_supply``, but with NO backoff hold: a kill is not
    evidence the supplier is unavailable, so the very next session may
    resolve it immediately. ``executor_status`` (architect resolution,
    #1979 external-review followups) is the evidence :func:`check_running_for_kill`
    read from the executor's own telemetry (``None`` when there was
    none) -- carried into the record so the operator/planner can see
    WHAT the executor last reported, even though the gate never acted on
    it. Does not touch ``running`` -- :func:`resolve` (``keep``) and
    :func:`record_attempt_finished` own its lifecycle from here."""
    from nanobot.runtime.cycle_ledger import append_event

    state = load_state(state_dir)
    state.pending = {
        "retry_key": retry_key,
        "cycle_id": cycle_id or "",
        "plan_text": plan_text,
        "candidate_id": candidate_id or None,
        "reason": "interrupted_kill",
        "interrupted_at": _now_iso(),
        "branch": branch or "",
        "opening_entry_written": True,
        "executor_status": executor_status,
    }
    state.hold = None
    state.held_ticks = 0
    _save_state(state_dir, state)
    append_event(state_dir, {
        "phase": "open_increment",
        "cycle_id": cycle_id or "",
        "status": "interrupted_kill",
        "retry_key": retry_key,
        "branch": branch or "",
        "executor_status": executor_status,
    })
    return state
