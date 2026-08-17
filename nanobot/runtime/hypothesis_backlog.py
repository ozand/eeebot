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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOP_N = 5
MAX_SECTION_CHARS = 1200
STALE_AFTER_DAYS = 14
STALE_AFTER_UNTOUCHED_CYCLES = 50
# #878: how many "supported" hypotheses (newest verdict first) goal_review
# gets to cite as evidence per review — kept small like every other bounded
# evidence source that module reads (decay, goal-gaps).
SUPPORTED_TOP_N = 3

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


def _all_candidates(state_dir: Path) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for cand in _backlog_candidates(state_dir) + _research_candidates(state_dir):
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


def has_in_flight_experiment(state_dir: Path) -> bool:
    """True iff some ``active``-status hypothesis candidate has a
    ``'proposed'`` ledger row (``serves: hypothesis <ref>``) whose
    ``cycle_id`` has NOT YET produced a terminal ``'outcome'`` row — i.e. an
    experiment cycle for it is currently running or awaiting its verdict.

    Used by ``demand._hypothesis_items`` to enforce the #878 "at most one
    active hypothesis experiment" rule: while this is true, no NEW
    hypothesis-kind demand item is minted this cycle (the loop keeps
    presenting whatever else it has instead of stacking a second parallel
    experiment on top of the one already running). Deliberately does not
    consult ``exhausted``/``completed`` state — this is only about a cycle
    that is currently open, not about retry bookkeeping. Fail-open: ``False``
    on any error (never blocks demand collection)."""
    try:
        state_dir = Path(state_dir)
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
        proposed_cycle_ids: set[str] = set()
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
                    proposed_cycle_ids.add(cid)
                    break

        return any(cid not in outcome_cycle_ids for cid in proposed_cycle_ids)
    except Exception:
        return False
