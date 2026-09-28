"""ADR-035 rest amendment (PR #1964, folded into #1942 B2): a third
planning-session outcome alongside a plan and ``no_plan``, plus the harness
pre-check that keeps it cheap.

**`rest`** is a structured decision, not prose: a **wake condition** naming
one observable input (a hypothesis verdict, an operator priority, a
candidate, a commit on ``main``, or a file/unit the harness can read) and a
**deadline** for the next look. Missing either is ``malformed`` -- see
:func:`parse_rest`, raising :class:`RestValidationError`.

**The pre-check tests change, never value** (:func:`precheck`). At a rest,
:func:`record_rest` snapshots the VERSION of the named input (a hash, an
mtime+size pair, a git sha -- whatever the input kind has cheaply). On
later ticks, before any model call, :func:`precheck` compares the CURRENT
version to that snapshot. Unchanged AND the deadline has not passed -> hold
(no session). Any change, an unreadable input, or a passed deadline ->
return the choice to the planner. This function never judges whether a
change matters -- only whether the version differs.

**Six rests in a row** raise a review signal (:data:`REVIEW_SIGNAL_THRESHOLD`)
-- a prompt to look, never proof of a defect. **Three counters stay
separate**: held ticks (pre-check held, no session at all), ``rest``
decisions, and ``no_plan`` (the latter tracked by
:mod:`nanobot.runtime.no_plan_recovery`, untouched by this module) -- see
:func:`record_rest`, :func:`record_held_tick`, :func:`record_non_rest_outcome`.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STATE_RELPATH = ("planner", "rest_state.json")
_SCHEMA = "planner-rest-v1"

#: The observable input kinds a wake condition may name. ``main_commit``
#: needs no ``ref`` (there is only one ``main``); every other kind requires
#: one (a hypothesis id, a priority number/title, a candidate id, or a path).
VALID_WAKE_KINDS = frozenset({
    "hypothesis_verdict", "operator_priority", "candidate", "main_commit", "file", "unit",
})

#: ADR-035 rest amendment: six rests in a row raises the review signal.
REVIEW_SIGNAL_THRESHOLD = 6


class RestValidationError(ValueError):
    """A planner `rest` object is missing its wake condition or deadline."""


@dataclass(frozen=True)
class WakeCondition:
    kind: str
    ref: str = ""


def _parse_deadline(deadline: str) -> datetime:
    parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_rest(raw: Any) -> tuple[WakeCondition, str]:
    """Validate a planner's ``rest`` object.

    Expected shape: ``{"wake_condition": {"kind": ..., "ref": ...},
    "deadline": "<ISO 8601 timestamp>"}``. Raises :class:`RestValidationError`
    naming what is missing/invalid -- a rest without both a wake condition
    and a deadline is ``malformed``, never a silently-accepted rest.
    """
    if not isinstance(raw, dict):
        raise RestValidationError(f"rest must be an object, got {type(raw).__name__}")
    wc_raw = raw.get("wake_condition")
    if not isinstance(wc_raw, dict):
        raise RestValidationError("rest missing wake_condition")
    kind = str(wc_raw.get("kind") or "").strip()
    if kind not in VALID_WAKE_KINDS:
        raise RestValidationError(f"rest wake_condition has unrecognized kind {kind!r}")
    ref = str(wc_raw.get("ref") or "").strip()
    if kind != "main_commit" and not ref:
        raise RestValidationError(f"rest wake_condition of kind {kind!r} requires a non-blank ref")
    deadline = raw.get("deadline")
    if not isinstance(deadline, str) or not deadline.strip():
        raise RestValidationError("rest missing deadline")
    try:
        _parse_deadline(deadline.strip())
    except Exception as exc:
        raise RestValidationError(f"rest deadline is not a valid timestamp: {exc}") from exc
    return WakeCondition(kind=kind, ref=ref), deadline.strip()


def snapshot_version(state_dir: "Path", selfevo_repo: "Path | None", wc: WakeCondition) -> "str | None":
    """A cheap version string for ``wc``'s input, or ``None`` if it cannot
    be read right now (a missing file, an absent repo, a vanished
    candidate/priority/hypothesis) -- :func:`precheck` treats ``None`` the
    same as a changed version: run the session.
    """
    state_dir = Path(state_dir)
    try:
        if wc.kind == "main_commit":
            if not selfevo_repo or not Path(selfevo_repo).is_dir():
                return None
            import subprocess

            out = subprocess.run(
                ["git", "-C", str(selfevo_repo), "rev-parse", "origin/main"],
                capture_output=True, text=True, timeout=10,
            )
            # Small item (ADR-035 Test Contract, #1962): a failing rev-parse
            # can still print something to stdout despite a nonzero exit
            # (e.g. an ambiguous-ref echo) -- checking the returncode, not
            # just stdout, is what keeps that read honestly "unknown"
            # rather than a stored literal that looks unchanged forever.
            if out.returncode != 0:
                return None
            sha = out.stdout.strip()
            return sha or None

        if wc.kind in ("file", "unit"):
            path = Path(wc.ref)
            if not path.is_absolute():
                path = state_dir / wc.ref
            if not path.exists():
                return None
            st = path.stat()
            return f"{st.st_mtime_ns}:{st.st_size}"

        if wc.kind == "hypothesis_verdict":
            lifecycle_path = state_dir / "hypotheses" / "lifecycle.json"
            if not lifecycle_path.is_file():
                return None
            data = json.loads(lifecycle_path.read_text(encoding="utf-8"))
            entries = data.get("hypotheses") if isinstance(data, dict) else None
            if not isinstance(entries, dict):
                entries = data.get("entries") if isinstance(data, dict) else None
            entry = entries.get(wc.ref) if isinstance(entries, dict) else None
            if entry is None:
                return None
            return json.dumps(entry.get("verdict"), sort_keys=True, ensure_ascii=False)

        if wc.kind == "operator_priority":
            from nanobot.runtime.operator_documents import resolve_operator_priorities

            res = resolve_operator_priorities(state_dir)
            for entry in res.open_entries:
                if str(entry.number) == wc.ref or entry.title == wc.ref:
                    return hashlib.sha256(
                        f"{entry.number}:{entry.title}:{entry.instructions}".encode("utf-8")
                    ).hexdigest()
            return None

        if wc.kind == "candidate":
            from nanobot.runtime import demand as _demand_mod

            items = _demand_mod.collect_demand(state_dir, selfevo_repo)
            for item in items:
                if item.get("id") == wc.ref:
                    return hashlib.sha256(
                        json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    ).hexdigest()
            return None
    except Exception:
        return None
    return None


@dataclass
class RestState:
    active_rest: "dict[str, Any] | None" = None
    consecutive_rests: int = 0
    review_signal: bool = False
    held_ticks_since_last_session: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _state_path(state_dir: "Path") -> "Path":
    return Path(state_dir).joinpath(*_STATE_RELPATH)


def load_state(state_dir: "Path") -> RestState:
    path = _state_path(state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return RestState()
    known = set(RestState.__dataclass_fields__)
    try:
        return RestState(**{k: v for k, v in raw.items() if k in known})
    except Exception:
        return RestState()


def _save_state(state_dir: "Path", state: RestState) -> None:
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


def precheck(state_dir: "Path", selfevo_repo: "Path | None") -> tuple[bool, str]:
    """Should the harness run a real session (and, ahead of it, repository
    preparation) this tick? Returns ``(run, reason)``.

    Version-only, never value: the only questions asked are "did the named
    input's version change", "did the deadline pass", and "can the input
    still be read" -- never "is this change worth waking up for".
    """
    state = load_state(state_dir)
    active = state.active_rest
    if not active:
        return True, "no_active_rest"
    try:
        wc = WakeCondition(
            kind=active["wake_condition"]["kind"], ref=active["wake_condition"].get("ref", ""),
        )
        deadline = active["deadline"]
        snapshot = active.get("snapshot")
    except Exception:
        return True, "corrupt_rest_state"
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


def record_rest(
    state_dir: "Path", cycle_id: str, wake_condition: WakeCondition, deadline: str,
    selfevo_repo: "Path | None",
) -> RestState:
    """Record a `rest` decision: snapshot the named input's current version,
    start (or replace) the active rest, and advance the consecutive-rest
    streak -- escalating to the review signal at
    :data:`REVIEW_SIGNAL_THRESHOLD`.
    """
    from nanobot.runtime.cycle_ledger import append_event

    state = load_state(state_dir)
    snapshot = snapshot_version(state_dir, selfevo_repo, wake_condition)
    state.active_rest = {
        "wake_condition": {"kind": wake_condition.kind, "ref": wake_condition.ref},
        "deadline": deadline,
        "snapshot": snapshot,
    }
    state.consecutive_rests += 1
    state.held_ticks_since_last_session = 0
    escalated = state.consecutive_rests >= REVIEW_SIGNAL_THRESHOLD and not state.review_signal
    if escalated:
        state.review_signal = True
    _save_state(state_dir, state)

    append_event(state_dir, {
        "phase": "planner_rest",
        "cycle_id": cycle_id or "",
        "consecutive_rests": state.consecutive_rests,
        "wake_condition": state.active_rest["wake_condition"],
        "deadline": deadline,
    })
    if escalated:
        append_event(state_dir, {
            "phase": "planner_rest_review_signal",
            "cycle_id": cycle_id or "",
            "consecutive_rests": state.consecutive_rests,
            "note": "six rests in a row -- a prompt to look, not proof of a defect",
        })
    return state


def record_non_rest_outcome(state_dir: "Path", cycle_id: str, outcome: str) -> RestState:
    """A real session ran and did NOT rest (a plan, `no_plan`, or any other
    outcome) -- clears the active rest condition and resets the rest streak
    and the held-ticks counter. ``outcome`` is accepted for symmetry with
    the ledger call sites and future review-signal wording, not branched on.
    """
    state = RestState()
    _save_state(state_dir, state)
    return state


def record_held_tick(state_dir: "Path", cycle_id: str) -> RestState:
    """The pre-check held this tick -- no session ran at all. Counted
    separately from `rest` decisions and `no_plan` (ADR-035 rest amendment:
    "three counts stay separate")."""
    from nanobot.runtime.cycle_ledger import append_event

    state = load_state(state_dir)
    state.held_ticks_since_last_session += 1
    _save_state(state_dir, state)
    append_event(state_dir, {
        "phase": "planner_rest_held",
        "cycle_id": cycle_id or "",
        "held_ticks_since_last_session": state.held_ticks_since_last_session,
    })
    return state
