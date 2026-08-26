"""Hypotheses -> priorities reader (#751).

The deterministic planner (retired in #739) used to be the only reader of
``state_dir/hypotheses/backlog.json`` (written every self-evolving cycle by
``nanobot.runtime.coordinator``/``cycle_persist._build_hypothesis_backlog_snapshot``)
and ``state_dir/research/hypotheses.json`` (an append-only cycle-snapshot log
written by ``cycle_planning._write_research_feed``). With the planner off,
nothing on the live path read either file — the "hypothesis -> priority"
chain existed only on paper. This module gives the LLM proposer
(``nanobot.runtime.llm_proposer``) a bounded, fail-open read of both files as
candidate ``serves: hypothesis <id>`` targets, plus a small lifecycle
(active -> answered/stale) so the context doesn't re-offer resolved or
abandoned candidates forever.

Design note — why lifecycle state is NOT stored inside ``backlog.json``
itself (a deviation from the most literal reading of #751's brief): that
file is fully REGENERATED every self-evolving cycle by
``cycle_persist._build_hypothesis_backlog_snapshot`` (a plain
``path.write_text(json.dumps(...))``, not a read-modify-write) — any status
key this module added to an entry would be silently wiped by the very next
cycle's snapshot. Persisting lifecycle status in that file would therefore
require invasive changes to the coordinator's cycle-persistence path, well
outside this change's scope. Instead, this module owns a small sidecar file,
``state_dir/hypotheses/lifecycle.json``, keyed by a stable candidate key
(the snapshot's own ``hypothesis_id`` when present, else a slug of the
title) that it reads/writes exclusively — additive-only, never dropping
unknown fields, satisfying the same spirit (state survives across cycles)
without fighting the existing writer's overwrite semantics.

Everything here is fail-open: a missing/corrupt file, or any unexpected
shape, degrades to "no candidates" / "no lifecycle change" rather than
raising. Nothing here ever touches ``backlog.json`` or
``research/hypotheses.json`` themselves — both remain owned by their
existing writers.

#878 (closing the hypothesis -> experiment -> verdict loop): ``reconcile``
now also computes a harness-MEASURED verdict (:mod:`hypothesis_verdict`) the
same pass it first marks a candidate ``answered``, persisting ``verdict``/
``verdict_evidence``/``verdict_at`` onto the SAME lifecycle entry.
:func:`supported_hypotheses` and :func:`lifecycle_counts` read the verdict
back out for, respectively, ``goal_review``'s evidence input and the
scorecard control-plane snapshot; :func:`has_in_flight_experiment` is the
read side of the "at most one active hypothesis experiment" rule
``demand._hypothesis_items`` enforces. See ``hypothesis_verdict``'s module
docstring for the full trust argument (verdict is steering, never a
verification gate).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOP_N = 5
MAX_SECTION_CHARS = 1200
DURABLE_MAX_ENTRIES = 20
STALE_AFTER_DAYS = 14
STALE_AFTER_UNTOUCHED_CYCLES = 50
# #878: how many "supported" hypotheses (newest verdict first) goal_review
# gets to cite as evidence per review — kept small like every other bounded
# evidence source that module reads (decay, goal-gaps).
SUPPORTED_TOP_N = 3
# #878 opus-review Y1 fix: a 'proposed' cycle with no terminal 'outcome' row
# is normally "still running" — but a cycle that crashed/was killed on the
# host (power loss, OOM-kill) never writes a terminal outcome at all, so
# without a timeout has_in_flight_experiment would read as permanently
# in-flight and freeze the whole hypothesis demand lane until the candidate
# ages into STALE_AFTER_DAYS (14 days). A proposed row older than this many
# days is treated as abandoned/dead rather than still-running, giving the
# lane a release path far short of the stale window.
IN_FLIGHT_TIMEOUT_DAYS = 3

_HYPOTHESIS_SERVES_RE = re.compile(r"^hypothesis\s+(.+)$", re.IGNORECASE)


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_ledger_rows(state_dir: Path) -> list[dict[str, Any]]:
    path = Path(state_dir) / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:60]


def _candidate_key(entry: dict[str, Any]) -> str:
    hid = str(entry.get("hypothesis_id") or "").strip()
    if hid:
        return hid
    title = str(entry.get("task_title") or entry.get("title") or "").strip()
    return f"slug-{_slug(title)}" if title else ""


def _backlog_candidates(state_dir: Path) -> list[dict[str, str]]:
    """Primary source: ``hypotheses/backlog.json``'s ``entries`` list
    (``cycle_persist._build_hypothesis_backlog_snapshot``'s shape)."""
    data = _read_json(Path(state_dir) / "hypotheses" / "backlog.json", None)
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []
    out: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = _candidate_key(entry)
        title = str(entry.get("task_title") or entry.get("title") or "").strip()
        if not key or not title:
            continue
        out.append({"key": key, "title": title, "source": "backlog"})
    return out


def _research_candidates(state_dir: Path) -> list[dict[str, str]]:
    """Secondary source: ``research/hypotheses.json`` — an append-only list
    of cycle snapshots (``cycle_planning._write_research_feed``'s shape),
    each with a ``candidates`` list of ``{title, hypothesis, acceptance}``."""
    data = _read_json(Path(state_dir) / "research" / "hypotheses.json", None)
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for snapshot in data:
        if not isinstance(snapshot, dict):
            continue
        candidates = snapshot.get("candidates")
        if not isinstance(candidates, list):
            continue
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            title = str(cand.get("title") or cand.get("hypothesis") or "").strip()
            if not title:
                continue
            key = f"slug-{_slug(title)}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"key": key, "title": title, "source": "research"})
    return out


def _durable_candidates(state_dir: Path) -> list[dict[str, str]]:
    data = _read_json(Path(state_dir) / "hypotheses" / "durable.json", None)
    entries = data.get("entries") if isinstance(data, dict) else []
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            key = _candidate_key(entry)
            title = str(entry.get("task_title") or entry.get("title") or "").strip()
            if key and title:
                out.append({"key": key, "title": title, "source": "durable"})
    return out


def _all_candidates(state_dir: Path) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for cand in _durable_candidates(state_dir) + _backlog_candidates(state_dir) + _research_candidates(state_dir):
        if cand["key"] in seen:
            continue
        seen.add(cand["key"])
        out.append(cand)
    return out


def _load_lifecycle(state_dir: Path) -> dict[str, Any]:
    data = _read_json(Path(state_dir) / "hypotheses" / "lifecycle.json", None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"schema_version": "hypothesis-lifecycle-v1", "entries": {}}
    return data


def _save_lifecycle(state_dir: Path, data: dict[str, Any]) -> None:
    _write_json(Path(state_dir) / "hypotheses" / "lifecycle.json", data)


def _serves_hypothesis_ref(serves: str) -> str | None:
    match = _HYPOTHESIS_SERVES_RE.match((serves or "").strip())
    if not match:
        return None
    return match.group(1).strip()


def _apply_verdict(state_dir: Path, entry: dict[str, Any], key: str, cycle_id: str, now_iso: str) -> None:
    """#878: compute + persist the harness-measured verdict on a hypothesis
    entry the instant it is first marked ``answered`` (same reconciliation
    pass, no separate hook). Additive to ``entry`` in place: ``verdict``,
    ``verdict_evidence``, ``verdict_at``. Also appends one
    ``{"phase": "hypothesis", "reason": "verdict", ...}`` ledger row.
    Best-effort — a verdict failure must never break ``reconcile``'s
    existing answered/stale bookkeeping."""
    verdict = None
    evidence: dict[str, Any] = {}
    try:
        from nanobot.runtime.hypothesis_verdict import classify_hypothesis_verdict

        verdict, evidence = classify_hypothesis_verdict(state_dir, cycle_id, entry.get("title") or "")
        entry["verdict"] = verdict
        entry["verdict_evidence"] = evidence
        entry["verdict_at"] = now_iso
    except Exception:
        return
    try:
        from nanobot.runtime.cycle_ledger import append_event

        append_event(
            state_dir,
            {
                "phase": "hypothesis",
                "reason": "verdict",
                "hypothesis_ref": key,
                "verdict": verdict,
                "cycle_id": str(cycle_id),
                "evidence": evidence,
            },
        )
    except Exception:
        pass


def _maybe_upgrade_inconclusive_verdict(
    state_dir: Path, entry: dict[str, Any], key: str, now_iso: str
) -> None:
    """#878 opus-review N1 fix: re-evaluate an already-answered hypothesis's
    verdict on a LATER reconcile pass if it is still ``"inconclusive"`` —
    and, per #894, also if it has NO verdict at all (``None``), which is
    the shape of any ``answered`` entry that predates #878 entirely and so
    never got a first classify pass.

    ``_apply_verdict`` originally only ran once, at the exact moment a
    candidate flips to ``answered`` — day 0. At that instant the
    confirmed-usage source (:func:`hypothesis_verdict._confirmed_usage_verdict`)
    is structurally ALWAYS either absent (no completed entry yet) or still
    inside its confirm window, so without this re-check the "supported once
    later confirmed" and "refuted once the window elapses unconfirmed"
    confirmed-usage branches could never fire in production — only
    microbench ever produced a real (non-inconclusive) verdict. This makes
    them reachable: any reconcile pass over an ``answered`` entry whose
    stored ``verdict`` is still ``"inconclusive"`` re-runs
    :func:`hypothesis_verdict.classify_hypothesis_verdict` against the SAME
    serving ``cycle_id`` (``answered_evidence``); if a measured source now
    resolves to ``supported``/``refuted``, the entry and a
    ``{"phase": "hypothesis", "reason": "verdict"}`` ledger event are
    updated exactly like the original transition. If it is STILL
    inconclusive, nothing is written and no event is appended — a
    steady-state inconclusive entry costs one classify call per reconcile
    pass, not a growing write/event history. Best-effort — a failure here
    must never break the rest of reconciliation."""
    cycle_id = entry.get("answered_evidence")
    if not cycle_id:
        return
    try:
        from nanobot.runtime.hypothesis_verdict import classify_hypothesis_verdict

        verdict, evidence = classify_hypothesis_verdict(state_dir, cycle_id, entry.get("title") or "")
    except Exception:
        return
    if verdict == "inconclusive":
        return  # unchanged — no write, no event
    entry["verdict"] = verdict
    entry["verdict_evidence"] = evidence
    entry["verdict_at"] = now_iso
    try:
        from nanobot.runtime.cycle_ledger import append_event

        append_event(
            state_dir,
            {
                "phase": "hypothesis",
                "reason": "verdict",
                "hypothesis_ref": key,
                "verdict": verdict,
                "cycle_id": str(cycle_id),
                "evidence": evidence,
            },
        )
    except Exception:
        pass


def _ref_matches_candidate(ref: str, cand: dict[str, str]) -> bool:
    ref_low = ref.lower().strip()
    if not ref_low:
        return False
    key_low = cand["key"].lower()
    title_low = cand["title"].lower()
    return ref_low == key_low or ref_low in key_low or ref_low in title_low


def reconcile(state_dir: Path, *, now: datetime | None = None) -> None:
    """Lazy lifecycle reconciliation (#751 design choice — no invasive hook
    into where cycle outcomes are recorded; this runs instead as a side
    effect of every :func:`top_candidates`/:func:`context_section` call, i.e.
    once per proposer cycle via ``llm_proposer.build_context``).

    Marks a candidate ``answered`` (with the resolving ``cycle_id`` as
    evidence) the first time a ledger ``'proposed'`` row whose ``serves``
    names it (``hypothesis <ref>``) is later followed by a same-``cycle_id``
    ``'outcome'`` row with ``outcome == "success"``. Demotes any still-
    ``active`` candidate to ``stale`` once it has gone unreferenced by any
    ``serves: hypothesis ...`` proposal for :data:`STALE_AFTER_UNTOUCHED_CYCLES`
    reconciliation passes, or :data:`STALE_AFTER_DAYS` days have elapsed
    since it was first observed — whichever comes first. Fail-open: never
    raises; a partial/corrupt read degrades to "no reconciliation this
    pass" rather than crashing the caller.

    #878: the SAME pass that first flips a candidate to ``answered`` also
    computes its harness-measured verdict (:func:`_apply_verdict`); every
    LATER pass over an ``answered`` candidate whose verdict is still
    ``"inconclusive"`` re-checks it (:func:`_maybe_upgrade_inconclusive_verdict`)
    so a confirmed-usage signal that only arrives after day 0 can still
    resolve the verdict.
    """
    try:
        state_dir = Path(state_dir)
        now = now or datetime.now(timezone.utc)
        candidates = _all_candidates(state_dir)
        if not candidates:
            return

        lifecycle = _load_lifecycle(state_dir)
        entries: dict[str, Any] = lifecycle.setdefault("entries", {})

        rows = _load_ledger_rows(state_dir)
        proposed_by_cycle: dict[str, dict[str, Any]] = {}
        outcome_by_cycle: dict[str, dict[str, Any]] = {}
        for row in rows:
            cid = row.get("cycle_id")
            if not cid:
                continue
            phase = row.get("phase")
            if phase == "proposed":
                proposed_by_cycle[cid] = row
            elif phase == "outcome":
                outcome_by_cycle[cid] = row

        touched_keys: set[str] = set()
        answered_keys: dict[str, str] = {}
        for cid, prow in proposed_by_cycle.items():
            ref = _serves_hypothesis_ref(str(prow.get("serves") or ""))
            if not ref:
                continue
            for cand in candidates:
                if not _ref_matches_candidate(ref, cand):
                    continue
                touched_keys.add(cand["key"])
                outcome_row = outcome_by_cycle.get(cid)
                if outcome_row and outcome_row.get("outcome") == "success":
                    answered_keys[cand["key"]] = str(cid)

        now_iso = now.isoformat().replace("+00:00", "Z")
        for cand in candidates:
            key = cand["key"]
            entry = entries.get(key)
            if not isinstance(entry, dict):
                entry = {}
            entry.setdefault("status", "active")
            entry.setdefault("first_seen", now_iso)
            entry.setdefault("cycles_untouched", 0)
            entry.setdefault("title", cand["title"])

            if key in answered_keys and entry.get("status") != "answered":
                entry["status"] = "answered"
                entry["answered_evidence"] = answered_keys[key]
                entry["answered_at"] = now_iso
                # #878: compute + persist the harness-measured verdict the
                # SAME pass a hypothesis is first marked answered — no
                # separate hook into where cycle outcomes are recorded,
                # consistent with this module's existing lazy-reconciliation
                # design note above.
                _apply_verdict(state_dir, entry, key, answered_keys[key], now_iso)
            elif entry.get("status") == "answered" and entry.get("verdict") in (None, "inconclusive"):
                # #878 opus-review N1 fix: re-check on every LATER pass too —
                # see _maybe_upgrade_inconclusive_verdict's docstring for why
                # the confirmed-usage source needs this to ever fire.
                # #894: also catches legacy "answered" entries that predate
                # #878 entirely and so have NO verdict field at all (verdict
                # is None, not the string "inconclusive") — those never got
                # a first classify pass and would otherwise sit unevaluated
                # forever.
                _maybe_upgrade_inconclusive_verdict(state_dir, entry, key, now_iso)

            if entry.get("status") == "active":
                if key in touched_keys:
                    entry["cycles_untouched"] = 0
                    entry["last_touched"] = now_iso
                else:
                    entry["cycles_untouched"] = int(entry.get("cycles_untouched") or 0) + 1

                first_seen = _parse_ts(entry.get("first_seen")) or now
                age_days = (now - first_seen).total_seconds() / 86400.0
                if (
                    age_days >= STALE_AFTER_DAYS
                    or entry["cycles_untouched"] >= STALE_AFTER_UNTOUCHED_CYCLES
                ):
                    entry["status"] = "stale"
                    entry["stale_at"] = now_iso

            entries[key] = entry

        lifecycle["entries"] = entries
        _save_lifecycle(state_dir, lifecycle)
    except Exception:
        pass


def top_candidates(state_dir: Path, n: int = TOP_N) -> list[dict[str, str]]:
    """Top ``n`` still-``active`` candidates, backlog-order then research-
    order, reconciling lifecycle state first. Fail-open: returns ``[]`` on
    any error."""
    try:
        state_dir = Path(state_dir)
        reconcile(state_dir)
        candidates = _all_candidates(state_dir)
        if not candidates:
            return []
        lifecycle = _load_lifecycle(state_dir)
        entries = lifecycle.get("entries", {})
        out: list[dict[str, str]] = []
        for cand in candidates:
            status = "active"
            stored = entries.get(cand["key"])
            if isinstance(stored, dict):
                status = str(stored.get("status") or "active")
            if status != "active":
                continue
            out.append(cand)
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


def context_section(state_dir: Path) -> str:
    """Bounded ``## Hypothesis backlog (candidate value sources)`` body (no
    heading — the caller adds it), one ``- [<key>] <title>`` line per
    candidate. Fail-open: returns ``""`` on any error or when there are no
    active candidates, so the caller can omit the section entirely."""
    try:
        candidates = top_candidates(state_dir, TOP_N)
        if not candidates:
            return ""
        lines = [f"- [{cand['key']}] {cand['title']}" for cand in candidates]
        section = "\n".join(lines)
        if len(section) > MAX_SECTION_CHARS:
            section = section[:MAX_SECTION_CHARS]
        return section
    except Exception:
        return ""


# ─── #878: verdict-derived readers ──────────────────────────────────────────


def supported_hypotheses(state_dir: Path, n: int = SUPPORTED_TOP_N) -> list[dict[str, Any]]:
    """Hypotheses the harness-computed verdict (:mod:`hypothesis_verdict`)
    marked ``"supported"`` — newest ``verdict_at`` first, capped to ``n``.

    Each item is ``{"title": ..., "evidence": ...}`` where ``evidence`` is
    the SAME ``verdict_evidence`` dict persisted on the lifecycle entry
    (already sourced from a measured sidecar, never instance-authored
    text). Consumed by ``goal_review._collect_evidence`` as an additional
    citable evidence line, the same way decay/goal-gap evidence already is
    — a supported hypothesis still has to pass ``goal_review``'s normal
    fail-closed ``validate_priority`` before it can ever become a priority.
    Fail-open: ``[]`` on any error or when nothing is verdict-marked yet."""
    try:
        state_dir = Path(state_dir)
        lifecycle = _load_lifecycle(state_dir)
        entries = lifecycle.get("entries", {})
        if not isinstance(entries, dict):
            return []
        ranked: list[tuple[str, dict[str, Any]]] = []
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("verdict") or "") != "supported":
                continue
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            ranked.append(
                (str(entry.get("verdict_at") or ""), {"title": title, "evidence": entry.get("verdict_evidence") or {}})
            )
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in ranked[:n]]
    except Exception:
        return []


def lifecycle_counts(state_dir: Path) -> dict[str, int]:
    """``{active, answered, supported, refuted, inconclusive}`` counts over
    every lifecycle entry (#878) — read by
    ``scorecard._control_plane_snapshot`` for VISIBILITY ONLY, never fed
    into fitness/targets/gaps. ``status`` (``active``/``answered``) and
    ``verdict`` (``supported``/``refuted``/``inconclusive``) are independent
    fields on the same entry, so an answered entry contributes to both an
    ``answered`` count and (once verdict-marked) a verdict count. Fail-open
    to ``{}`` on any error."""
    try:
        state_dir = Path(state_dir)
        lifecycle = _load_lifecycle(state_dir)
        entries = lifecycle.get("entries", {})
        if not isinstance(entries, dict):
            return {}
        counts = {"active": 0, "answered": 0, "supported": 0, "refuted": 0, "inconclusive": 0}
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status") or "")
            if status in ("active", "answered"):
                counts[status] += 1
            verdict = str(entry.get("verdict") or "")
            if verdict in ("supported", "refuted", "inconclusive"):
                counts[verdict] += 1
        return counts
    except Exception:
        return {}


def has_in_flight_experiment(state_dir: Path, *, now: datetime | None = None) -> bool:
    """True iff some ``active``-status hypothesis candidate has a
    ``'proposed'`` ledger row (``serves: hypothesis <ref>``) whose
    ``cycle_id`` has NOT YET produced a terminal ``'outcome'`` row AND whose
    ``proposed`` row is younger than :data:`IN_FLIGHT_TIMEOUT_DAYS` — i.e.
    an experiment cycle for it is currently running or awaiting its
    verdict.

    Used by ``demand._hypothesis_items`` to enforce the #878 "at most one
    active hypothesis experiment" rule: while this is true, no NEW
    hypothesis-kind demand item is minted this cycle (the loop keeps
    presenting whatever else it has instead of stacking a second parallel
    experiment on top of the one already running). Deliberately does not
    consult ``exhausted``/``completed`` state — this is only about a cycle
    that is currently open, not about retry bookkeeping.

    #878 opus-review Y1 fix: a serving cycle that crashed on the host
    (power loss, OOM-kill, process kill) never writes a terminal
    ``'outcome'`` row at all — without a timeout this would read as
    permanently in-flight, freezing the whole hypothesis demand lane until
    the candidate separately ages into :data:`STALE_AFTER_DAYS` (14 days).
    A ``'proposed'`` row older than :data:`IN_FLIGHT_TIMEOUT_DAYS` is
    therefore treated as abandoned, not still-running, regardless of
    whether it ever gets an outcome — a row with a missing/unparseable
    ``ts`` is conservatively still counted as in-flight (cannot confirm
    it's timed out). Fail-open: ``False`` on any error (never blocks demand
    collection)."""
    try:
        state_dir = Path(state_dir)
        now = now or datetime.now(timezone.utc)
        candidates = _all_candidates(state_dir)
        if not candidates:
            return False
        lifecycle = _load_lifecycle(state_dir)
        entries = lifecycle.get("entries", {})
        active_keys = {
            cand["key"]
            for cand in candidates
            if str((entries.get(cand["key"]) or {}).get("status") or "active") == "active"
        }
        if not active_keys:
            return False

        rows = _load_ledger_rows(state_dir)
        outcome_cycle_ids: set[str] = set()
        proposed_cycle_ts: dict[str, datetime | None] = {}
        for row in rows:
            cid = row.get("cycle_id")
            if not cid:
                continue
            phase = row.get("phase")
            if phase == "outcome":
                outcome_cycle_ids.add(cid)
                continue
            if phase != "proposed":
                continue
            ref = _serves_hypothesis_ref(str(row.get("serves") or ""))
            if not ref:
                continue
            for cand in candidates:
                if cand["key"] in active_keys and _ref_matches_candidate(ref, cand):
                    proposed_cycle_ts[cid] = _parse_ts(row.get("ts"))
                    break

        for cid, prow_ts in proposed_cycle_ts.items():
            if cid in outcome_cycle_ids:
                continue
            if prow_ts is not None:
                age_days = (now - prow_ts).total_seconds() / 86400.0
                if age_days >= IN_FLIGHT_TIMEOUT_DAYS:
                    continue  # abandoned — no longer counts as in-flight
            return True
        return False
    except Exception:
        return False


def append_hypotheses(state_dir: Path | str, new_entries: list[dict[str, Any]]) -> int:
    """Append structured hypothesis entries to state_dir/hypotheses/backlog.json (#999).

    Preserves the existing schema of backlog.json (dict with schema, updated_at, entries).
    Writes atomically via tempfile + os.replace.
    Returns the number of valid entries actually appended.
    """
    if not new_entries:
        return 0
    state_dir = Path(state_dir)
    hypotheses_dir = state_dir / "hypotheses"
    hypotheses_dir.mkdir(parents=True, exist_ok=True)
    backlog_path = hypotheses_dir / "durable.json"

    raw_data = _read_json(backlog_path, None)
    if isinstance(raw_data, dict) and isinstance(raw_data.get("entries"), list):
        backlog_data = dict(raw_data)
        entries = list(backlog_data.get("entries") or [])
    else:
        backlog_data = {"schema": "hypothesis-durable-v1", "entries": []}
        entries = []

    # Map existing titles / keys to avoid duplicate additions
    existing_titles = {
        str(e.get("title") or e.get("task_title") or e.get("hypothesis") or "").strip().lower()
        for e in entries
        if isinstance(e, dict)
    }

    appended = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for item in new_entries:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("task_title") or "").strip()
        hypothesis_text = str(item.get("hypothesis") or "").strip()
        if not title and not hypothesis_text:
            continue
        lookup_key = (title or hypothesis_text).lower()
        if lookup_key in existing_titles:
            continue

        entry_record: dict[str, Any] = {
            "hypothesis_id": str(item.get("hypothesis_id") or f"hyp-{len(entries) + 1:04d}"),
            "task_title": title or hypothesis_text[:80],
            "title": title or hypothesis_text[:80],
            "hypothesis": hypothesis_text,
            "action": str(item.get("action") or "").strip(),
            "data_to_collect": str(item.get("data_to_collect") or "").strip(),
            "insight_criterion": str(item.get("insight_criterion") or "").strip(),
            "evidence": str(item.get("data_to_collect") or "").strip(),
            "priority": str(item.get("priority") or "medium").strip().lower(),
            "source": str(item.get("source") or "strategist"),
            "created_at": str(item.get("created_at") or now_iso),
        }
        entries.append(entry_record)
        existing_titles.add(lookup_key)
        appended += 1

    trimmed = len(entries) > DURABLE_MAX_ENTRIES
    if trimmed:
        entries = entries[-DURABLE_MAX_ENTRIES:]
        backlog_data["entries"] = entries

    if appended > 0 or trimmed:
        backlog_data["updated_at"] = now_iso
        backlog_data["entries"] = entries

        fd, tmp = tempfile.mkstemp(prefix=".backlog.", dir=str(hypotheses_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(backlog_data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, backlog_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    return appended
