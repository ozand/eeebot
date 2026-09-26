"""ADR-035 rule 3: the planning session's hypothesis, in ADR-030's form.

Distinct from the legacy strategist-authored ``state_dir/hypotheses/durable.json``
(:mod:`nanobot.runtime.hypothesis_backlog`, ``append_hypotheses`` -- its
timer was retired by #1852; its entries stay readable as frozen candidates,
per ADR-035's decommission map). This module is the NEW writer: one
hypothesis per planning-session increment, stored in
``state_dir/hypotheses/planner_entries.json``, with a shape ADR-030 requires
a hypothesis to have and the legacy schema never enforced:

    statement            -- what will become true
    measure              -- how it will be measured
    refutation_condition -- what result would refute it

"A statement without a measure and a refutation condition is not a
hypothesis and is rejected at parse time" (ADR-035 rule 3) -- see
:func:`parse_hypothesis` and :class:`HypothesisValidationError`.

**Verdicts are out of scope here.** The harness computes verdicts
exclusively from measured sidecars (#878, :mod:`nanobot.runtime.
hypothesis_verdict`) -- "the agent never writes the verdict" (ADR-032 rule
5, restated at ADR-035 rule 3). No function in this module accepts or sets a
``verdict`` field; :func:`revise_hypothesis` and :func:`drop_hypothesis`
copy every other field forward but leave ``verdict`` (if a caller's stored
entry somehow carries one) untouched. Wiring a planner-authored
``hypothesis_id`` into the harness verdict pipeline itself -- so
``hypothesis_verdict``/``hypothesis_backlog.reconcile`` can compute a verdict
for one of these entries -- is deferred; see this ADR-035 B2 PR's body for
why (the pipeline's own docstring calls it a trust boundary that wants a
dedicated review, not a same-PR add-on).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = "planner-hypothesis-v1"
_RELPATH = ("hypotheses", "planner_entries.json")


class HypothesisValidationError(ValueError):
    """Raised at parse time when a candidate hypothesis lacks a measure or
    a refutation condition (ADR-035 rule 3)."""


@dataclass(frozen=True)
class ParsedHypothesis:
    statement: str
    measure: str
    refutation_condition: str


def parse_hypothesis(raw: "dict[str, Any] | Any") -> ParsedHypothesis:
    """Validate a candidate hypothesis dict against ADR-030's form.

    Raises :class:`HypothesisValidationError` if ``raw`` is not a dict, or
    if ``statement``, ``measure``, or ``refutation_condition`` is missing,
    not a string, or blank. This is the parse-time rejection ADR-035 rule 3
    requires -- "the plans of 2026-09-25 carried implementation assumptions
    ... that no verdict can ever settle" is exactly what a missing
    ``refutation_condition`` allows through if unchecked.
    """
    if not isinstance(raw, dict):
        raise HypothesisValidationError(f"hypothesis must be an object, got {type(raw).__name__}")
    fields: dict[str, str] = {}
    for key in ("statement", "measure", "refutation_condition"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise HypothesisValidationError(f"hypothesis missing required field {key!r}")
        fields[key] = value.strip()
    return ParsedHypothesis(**fields)


def _stable_id(parsed: ParsedHypothesis) -> str:
    digest = hashlib.sha256(
        f"{parsed.statement}\n{parsed.measure}\n{parsed.refutation_condition}".encode("utf-8")
    ).hexdigest()[:16]
    return f"planh-{digest}"


def _entries_path(state_dir: "Path") -> "Path":
    return Path(state_dir).joinpath(*_RELPATH)


def _read_all(state_dir: "Path") -> dict[str, Any]:
    path = _entries_path(state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except Exception:
        raw = None
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        return {"schema": _SCHEMA, "entries": []}
    return raw


def _write_all(state_dir: "Path", data: dict[str, Any]) -> None:
    path = _entries_path(state_dir)
    tmp_name: "str | None" = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as fh:
            tmp_name = fh.name
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
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


def _find(entries: list[dict[str, Any]], hypothesis_id: str) -> "dict[str, Any] | None":
    for entry in entries:
        if isinstance(entry, dict) and entry.get("hypothesis_id") == hypothesis_id:
            return entry
    return None


def store_hypothesis(state_dir: "Path", cycle_id: str, raw: "dict[str, Any]") -> dict[str, Any]:
    """Validate and store one planning-session hypothesis.

    Raises :class:`HypothesisValidationError` on an invalid ``raw`` --
    the caller decides what a rejected hypothesis means for the cycle
    (ADR-035 leaves that fail-open, same as every other planning-session
    failure); this function never silently drops the required fields.
    """
    parsed = parse_hypothesis(raw)
    hypothesis_id = _stable_id(parsed)
    data = _read_all(state_dir)
    entries: list[dict[str, Any]] = data["entries"]

    existing = _find(entries, hypothesis_id)
    if existing is not None:
        return existing

    entry = {
        "hypothesis_id": hypothesis_id,
        "statement": parsed.statement,
        "measure": parsed.measure,
        "refutation_condition": parsed.refutation_condition,
        "status": "open",
        "version": 1,
        "supersedes": None,
        "revised_by": None,
        "verdict": None,
        "origin_cycle_id": cycle_id or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "insight_decisions": [],
    }
    entries.append(entry)
    _write_all(state_dir, data)
    return entry


def revise_hypothesis(
    state_dir: "Path", hypothesis_id: str, *, reason: str, cycle_id: str,
    statement: "str | None" = None, measure: "str | None" = None,
    refutation_condition: "str | None" = None,
) -> dict[str, Any]:
    """Create a new linked version of ``hypothesis_id`` (ADR-035 rule 3:
    "revise creates a new hypothesis version with a link to the original;
    the original's measure and verdict stay as they were").

    Raises :class:`ValueError` if ``hypothesis_id`` does not exist, or if
    ``reason`` is blank -- a revision without a stated reason is exactly
    the silent self-evaluation rule 3's constraints forbid.
    """
    if not reason or not reason.strip():
        raise ValueError("revise_hypothesis requires a non-blank reason")
    data = _read_all(state_dir)
    entries: list[dict[str, Any]] = data["entries"]
    original = _find(entries, hypothesis_id)
    if original is None:
        raise ValueError(f"unknown hypothesis_id {hypothesis_id!r}")

    parsed = parse_hypothesis({
        "statement": statement if statement is not None else original["statement"],
        "measure": measure if measure is not None else original["measure"],
        "refutation_condition": (
            refutation_condition if refutation_condition is not None else original["refutation_condition"]
        ),
    })
    new_id = _stable_id(parsed)
    new_entry = {
        "hypothesis_id": new_id,
        "statement": parsed.statement,
        "measure": parsed.measure,
        "refutation_condition": parsed.refutation_condition,
        "status": "open",
        "version": int(original.get("version") or 1) + 1,
        "supersedes": hypothesis_id,
        "revised_by": None,
        "verdict": None,
        "origin_cycle_id": cycle_id or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "insight_decisions": [],
        "revision_reason": reason.strip(),
    }
    # The original is closed, never mutated on its own measure/verdict --
    # only `status` and the forward link change.
    original["status"] = "revised"
    original["revised_by"] = new_id
    entries.append(new_entry)
    _write_all(state_dir, data)
    return new_entry


def drop_hypothesis(state_dir: "Path", hypothesis_id: str, *, reason: str, cycle_id: str) -> dict[str, Any]:
    """Close ``hypothesis_id`` as dropped, keeping its negative evidence
    visible (ADR-035 rule 3: "drop closes the line and keeps its negative
    evidence visible; it never deletes a refutation").

    Raises :class:`ValueError` if unknown or ``reason`` is blank.
    """
    if not reason or not reason.strip():
        raise ValueError("drop_hypothesis requires a non-blank reason")
    data = _read_all(state_dir)
    entries: list[dict[str, Any]] = data["entries"]
    entry = _find(entries, hypothesis_id)
    if entry is None:
        raise ValueError(f"unknown hypothesis_id {hypothesis_id!r}")
    entry["status"] = "dropped"
    entry["drop_reason"] = reason.strip()
    entry["dropped_in_cycle"] = cycle_id or ""
    _write_all(state_dir, data)
    return entry


def record_insight_decision(
    state_dir: "Path", cycle_id: str, hypothesis_id: str, decision: str, *, note: str = "",
) -> dict[str, Any]:
    """Attach one insight decision (continue/revise/drop) to the hypothesis
    entry and mirror it to the cycle ledger (ADR-035 rule 3 + Attribution).

    Raises :class:`ValueError` for an unknown ``hypothesis_id`` or an
    unrecognized ``decision`` -- both would otherwise silently read as
    "continue" downstream.
    """
    from nanobot.runtime.cycle_ledger import VALID_INSIGHT_DECISIONS, record_insight_decision as _ledger_record

    if decision not in VALID_INSIGHT_DECISIONS:
        raise ValueError(f"unrecognized insight decision {decision!r}")
    data = _read_all(state_dir)
    entries: list[dict[str, Any]] = data["entries"]
    entry = _find(entries, hypothesis_id)
    if entry is None:
        raise ValueError(f"unknown hypothesis_id {hypothesis_id!r}")
    entry.setdefault("insight_decisions", []).append({
        "cycle_id": cycle_id or "",
        "decision": decision,
        "note": note or "",
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _write_all(state_dir, data)
    _ledger_record(state_dir, cycle_id, hypothesis_id, decision, note=note)
    return entry


def pending_insight_hypotheses(
    entries: list[dict[str, Any]], verdicted_ids: list[str],
) -> list[str]:
    """Of ``verdicted_ids`` (hypotheses with a new harness verdict this
    session), return those from ``entries`` that have NO insight decision
    yet, oldest (by ``created_at``) first -- the ordering
    :func:`nanobot.runtime.no_plan_recovery.defer_or_bound_verdicts` bounds
    to 5 per session (ADR-035 rule 3's backlog bound).
    """
    by_id = {e.get("hypothesis_id"): e for e in entries if isinstance(e, dict)}
    pending = [
        hid for hid in verdicted_ids
        if hid in by_id and not by_id[hid].get("insight_decisions")
    ]
    pending.sort(key=lambda hid: str(by_id[hid].get("created_at") or ""))
    return pending
