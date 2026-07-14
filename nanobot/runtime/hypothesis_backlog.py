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

            if key in answered_keys and entry.get("status") != "answered":
                entry["status"] = "answered"
                entry["answered_evidence"] = answered_keys[key]
                entry["answered_at"] = now_iso

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
