"""Bridge-native hypothesis backlog snapshot (#913).

The decommissioned coordinator (#900/#910) used to regenerate
``state_dir/hypotheses/backlog.json`` every cycle via
``cycle_persist._build_hypothesis_backlog_snapshot``. With the coordinator's
systemd timer removed, that file is frozen — live consumers
(``nanobot.runtime.hypothesis_backlog`` for the #751 "hypothesis backlog"
prompt section and the #878 hypothesis->verdict loop, plus
``nanobot.runtime.demand``'s hypothesis-kind demand items and
``nanobot.runtime.existence_index``'s duplicate-suspect index) would only
ever see staler and staler data.

This module gives the LIVE bridge cycle (``nanobot.runtime.bridge``) a
narrow, self-contained way to regenerate that same file every run, from
state files the bridge already has on hand — WITHOUT depending on
``cycle_persist`` (which is deleted wholesale in #916) or on any
coordinator-only payload (task_plan/experiment/generated_candidates).

Candidate sources (each independently fail-open — a missing/corrupt input
just means that source contributes zero entries, never an exception):

1. ``state_dir/subagents/requests/*.json`` — the live bridge request queue
   (the same directory ``bridge.find_pending_request`` reads) — but ONLY
   requests that are still actually pending: ``request_status`` must be
   ``queued``/``pending`` AND the request must not already have a
   ``handled_*.txt`` marker under ``state_dir/subagent_bridge`` (mirrors
   ``bridge.find_pending_request``'s ``real_handled`` marker check —
   ``requests/`` is an execution queue, not an archive, so an unfiltered
   read would surface already-EXECUTED task titles as "backlog"
   candidates). Bounded to the ``_MAX_REQUEST_CANDIDATES`` most recently
   modified files younger than ``_MAX_REQUEST_AGE_DAYS``, both applied in
   the SAME stat pass the sort needs anyway — see ``_recent_request_paths``.
2. ``state_dir/goals/goal_text.json`` — the operator canon's ``goal_id``
   (#1222; the coordinator's ``registry.json`` froze on 2026-08-22). Its
   ``current_task_id`` went with the coordinator: the bridge queue is FIFO,
   nothing pre-selects a task, so every candidate is ``backlog``.

The scoring formulas (``_task_effort_weight``/``_bounded_priority_score``/
``_wsjf_components``) are adapted, not imported, from
``cycle_persist._build_hypothesis_backlog_snapshot`` — copied here (stdlib,
pure functions) so this module survives #916's deletion of that module
unchanged and keeps scores roughly continuous with what the coordinator
used to produce.

Output shape stays a superset of what every live reader actually consumes:
``entries[].hypothesis_id``/``task_title``/``task_id`` (existence_index,
hypothesis_backlog), plus ``entries[].evidence``/``metric``/``acceptance``
(demand's evidence gate). ``research/hypotheses.json`` (the append-only
research-feed log) is intentionally NOT folded in here — its writer
(``cycle_planning._write_research_feed``) needs coordinator-only
``generated_candidates`` input that has no bridge-side equivalent; folding
it in would not be a small extraction, so it is left out of scope (#913
recon note).
"""
from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "hypothesis-backlog-bridge-v1"

# #913: bounded scan — a stuck/never-draining request queue must never turn
# this into an unbounded directory walk. Every *.json still gets stat()'d
# once (unavoidable — that's how the age cutoff and sort key are known),
# but the SORT only ever runs over the survivors of that one pass (files
# younger than _MAX_REQUEST_AGE_DAYS), and the result is capped again at
# _MAX_REQUEST_CANDIDATES — see _recent_request_paths.
_MAX_REQUEST_CANDIDATES = 200
_MAX_REQUEST_AGE_DAYS = 14
# #913 review: the READ itself is bounded (collections.deque(maxlen=...)
# while iterating the file line-by-line), not just the parse loop over an
# already-fully-read list — see _last_cycle_id.
_MAX_LEDGER_LINES = 2000


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _task_effort_weight(task: dict[str, Any]) -> int:
    """Adapted from cycle_persist._task_effort_weight — unchanged formula."""
    weight = 1
    if isinstance(task.get("command"), str) and task["command"].strip():
        weight += 1
    if isinstance(task.get("file_action"), dict):
        weight += 1
    if task.get("status") == "done":
        weight = 1
    return weight


def _bounded_priority_score(task: dict[str, Any], *, selected: bool) -> int:
    """Adapted from cycle_persist._bounded_priority_score, simplified: the
    original's ``task_class``/``feedback_decision`` inputs come from the
    coordinator's task-plan payload, which the bridge does not have — those
    terms are dropped, leaving status + selection-bonus + effort, still on
    the original's 0-100 scale."""
    status_value = {"active": 9, "pending": 6, "queued": 6, "done": 2}.get(
        str(task.get("status") or ""), 4
    )
    selected_bonus = 5 if selected else 0
    effort = _task_effort_weight(task)
    raw_score = ((status_value + selected_bonus) * 10) / effort
    return max(0, min(100, round(raw_score)))


def _wsjf_components(task: dict[str, Any], *, selected: bool) -> dict[str, Any]:
    """Adapted from cycle_persist._wsjf_components, simplified the same way
    as ``_bounded_priority_score`` above (no feedback_decision input)."""
    user_business_value = {"active": 8, "pending": 5, "queued": 5, "done": 1}.get(
        str(task.get("status") or ""), 3
    )
    time_criticality = 8 if selected else 4
    job_size = max(1, _task_effort_weight(task))
    score = round((user_business_value + time_criticality) / job_size, 2)
    return {
        "user_business_value": user_business_value,
        "time_criticality": time_criticality,
        "job_size": job_size,
        "score": score,
    }


def _acceptance_for(task: dict[str, Any], goal_id: str) -> str:
    acceptance = task.get("acceptance")
    if isinstance(acceptance, str) and acceptance.strip():
        return acceptance
    command = task.get("command")
    if isinstance(command, str) and command.strip():
        return f"`{command}` completes successfully"
    file_action = task.get("file_action") if isinstance(task.get("file_action"), dict) else None
    if isinstance(file_action, dict):
        summary = file_action.get("summary") or "complete the file action"
        path = file_action.get("path")
        return f"{summary} at {path}" if path else str(summary)
    backlog_instructions = task.get("backlog_instructions")
    if isinstance(backlog_instructions, str) and backlog_instructions.strip():
        return backlog_instructions
    title = task.get("task_title") or task.get("title") or "task"
    return f"{title} advances goal {goal_id}" if goal_id else f"{title} is completed"


def _active_goal_id(state_dir: Path) -> str:
    """#1222: from the operator's ``goals/goal_text.json`` (see
    :func:`goal_review.active_goal_id`), not the coordinator's frozen
    ``registry.json``. Fail-open to ``""``."""
    try:
        from nanobot.runtime.goal_review import active_goal_id

        return active_goal_id(state_dir)
    except Exception:
        return ""


def _last_cycle_id(state_dir: Path) -> str:
    """Best-effort: cycle_id of the last ledger row. Purely cosmetic —
    never gates the write.

    #913 review: the READ is bounded, not just the parse — lines are
    streamed into a ``deque(maxlen=_MAX_LEDGER_LINES)`` one at a time, so
    memory use is capped at the tail window even if ``cycles.jsonl`` is
    unexpectedly large, instead of reading the whole file into a list
    first and slicing it afterward."""
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return ""
    try:
        tail: 'deque[str]' = deque(maxlen=_MAX_LEDGER_LINES)
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                tail.append(line)
    except Exception:
        return ""
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("cycle_id"):
            return str(rec["cycle_id"])
    return ""


def _recent_request_paths(req_dir: Path) -> list[Path]:
    """One pass over ``req_dir``: stat every ``*.json`` once, drop entries
    older than ``_MAX_REQUEST_AGE_DAYS`` right there, and only THEN sort the
    survivors by mtime — the sort's input is the bounded, age-filtered set,
    not the raw (potentially unbounded) directory listing. Capped again at
    ``_MAX_REQUEST_CANDIDATES`` after sorting. Fail-open: ``[]`` on any
    directory-level error; a single file's stat() failing just drops that
    one file."""
    cutoff = time.time() - (_MAX_REQUEST_AGE_DAYS * 86400)
    dated: list[tuple[float, Path]] = []
    try:
        candidates = req_dir.glob("*.json")
    except Exception:
        return []
    for p in candidates:
        try:
            if not p.is_file():
                continue
            mtime = p.stat().st_mtime
        except Exception:
            continue
        if mtime < cutoff:
            continue
        dated.append((mtime, p))
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [p for _, p in dated[:_MAX_REQUEST_CANDIDATES]]


def _handled_request_markers(bridge_state_dir: Path) -> set[str]:
    """Mirror the marker half of ``bridge.find_pending_request``'s
    ``real_handled`` set (bridge.py's "Also check bridge's own handled
    markers" block): the sanitized-id stem of every ``handled_*.txt``
    marker, plus its recorded content (the original request path string
    written by ``handled_marker.write_text(str(req_path))``).

    Duplicated rather than imported — ``bridge.py`` imports THIS module
    (``write_backlog_snapshot``), so importing back from here would be
    circular; this module stays import-free of bridge.py entirely, per its
    module docstring. Fail-open: a missing/unreadable marker dir or file
    contributes fewer marks, never an exception — worst case a just-handled
    request briefly still counts as a candidate, never a crash."""
    handled: set[str] = set()
    if not bridge_state_dir.is_dir():
        return handled
    try:
        markers = list(bridge_state_dir.glob("handled_*.txt"))
    except Exception:
        return handled
    for marker in markers:
        handled.add(marker.stem[len("handled_"):])
        try:
            content = marker.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if content:
            handled.add(content)
    return handled


def _request_candidates(state_dir: Path, *, goal_id: str) -> list[dict[str, Any]]:
    req_dir = state_dir / "subagents" / "requests"
    if not req_dir.is_dir():
        return []
    paths = _recent_request_paths(req_dir)
    handled = _handled_request_markers(state_dir / "subagent_bridge")

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for path in paths:
        req = _read_json(path, None)
        if not isinstance(req, dict):
            continue
        status = str(req.get("request_status") or req.get("status") or "queued").lower()
        if status not in ("queued", "pending"):
            continue
        # #913 review (MAJOR): requests/ is an EXECUTION queue, not an
        # archive — nothing deletes a request file once handled, only
        # bridge's separate handled_*.txt marker records that. Without this
        # check every already-executed request would resurface here as a
        # "backlog" hypothesis candidate forever (stale-wrong steering into
        # the #751 prompt section and a possible `serves: hypothesis <id>`
        # leak into the #878 verdict loop). rid mirrors
        # bridge.find_pending_request's exact formula (request_id ->
        # verification_task_id -> the path itself) so the sanitized-stem
        # and raw-content comparisons against _handled_request_markers line
        # up with what bridge.py itself would match.
        rid = str(req.get("request_id") or req.get("verification_task_id") or path)
        safe_rid = rid.replace("/", "_")[:120]
        if rid in handled or safe_rid in handled or str(path) in handled:
            continue
        task_id = req.get("request_id") or req.get("semantic_task_id") or path.stem
        task_id = str(task_id)
        if task_id in seen_ids:
            continue
        task_title = str(req.get("task_title") or req.get("semantic_task_id") or "").strip()
        if not task_title:
            continue
        seen_ids.add(task_id)
        # #1222: nothing pre-selects a queued request any more (the
        # coordinator's current_task_id is gone); the keys stay for schema
        # stability, always "backlog".
        selected = False
        task_for_scoring = {**req, "status": status}
        acceptance = _acceptance_for(req, goal_id)
        evidence = req.get("evidence") or req.get("metric")
        entries.append(
            {
                "hypothesis_id": f"hypothesis-{task_id}",
                "task_id": task_id,
                "task_title": task_title,
                "task_status": status,
                "selected": selected,
                "selection_status": "selected" if selected else "backlog",
                "bounded_priority_score": _bounded_priority_score(task_for_scoring, selected=selected),
                "wsjf": _wsjf_components(task_for_scoring, selected=selected),
                "acceptance": acceptance,
                "evidence": evidence,
                "hadi": {
                    "hypothesis": task_title,
                    "action": acceptance,
                    "data": {"goal_id": goal_id, "source": "bridge_request_queue"},
                    "insights": [f"task_status={status}"],
                },
                "execution_spec": {
                    "goal": goal_id,
                    "task_title": task_title,
                    "acceptance": acceptance,
                },
            }
        )
    return entries


def _build_snapshot(state_dir: Path) -> dict[str, Any] | None:
    goal_id = _active_goal_id(state_dir)
    entries = _request_candidates(state_dir, goal_id=goal_id)

    selected_entry = next((e for e in entries if e.get("selected")), None)

    return {
        "schema_version": SCHEMA_VERSION,
        "model": "HADI",
        "generated_by": "bridge",
        "cycle_id": _last_cycle_id(state_dir),
        "goal_id": goal_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "selected_hypothesis_id": selected_entry.get("task_id") if selected_entry else None,
        "selected_hypothesis_title": selected_entry.get("task_title") if selected_entry else None,
        "selected_hypothesis_score": selected_entry.get("bounded_priority_score") if selected_entry else None,
        "entry_count": len(entries),
        "entries": entries,
    }


def write_backlog_snapshot(state_dir: Path, selfevo_repo: 'Path | None' = None) -> bool:
    """Regenerate ``state_dir/hypotheses/backlog.json`` from live bridge
    state. Returns True iff the file was written. Never raises — any
    exception (corrupt/missing input, IO error) yields False and leaves any
    existing file untouched (no partial write).

    ``selfevo_repo`` is accepted for interface symmetry with other
    bridge-side snapshot helpers and future extensions, but is not
    currently read — every input this function needs lives under
    ``state_dir``.
    """
    try:
        state_dir = Path(state_dir)
        snapshot = _build_snapshot(state_dir)
        if not isinstance(snapshot, dict):
            return False
        target_dir = state_dir / "hypotheses"
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(snapshot, indent=2, ensure_ascii=False)
        tmp_path = target_dir / "backlog.json.tmp"
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(target_dir / "backlog.json")
        return True
    except Exception:
        return False
