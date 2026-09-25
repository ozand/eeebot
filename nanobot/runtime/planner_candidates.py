"""ADR-035 rules 1 and 2 (#1942): the ranked candidate list the planning
session reads, and the INPUT-side trust-order guarantees this record
requires.

``demand.collect_demand`` already assembles candidates in the trust order
ADR-035 rule 1 says is "kept exactly as today" -- operator priority >
defect > goal-gap > skill-candidate > hypothesis > decay > reflection.
Nothing in this module reorders that list; :func:`render_candidates_block`
renders it, unchanged in order, for the planner's context.

**New-priority marking** (rule 1: "a new or changed operator priority is
shown to the planner in the next cycle, marked *new*"): :func:`mark_new_priority_items`
tracks which ``kind == "priority"`` item ids the planner has already been
shown, in ``state_dir/planner/seen_priority_ids.json``. An item's id
(``demand.item_id("priority", summary)``, where ``summary`` embeds the
priority's number and title) is stable across cycles as long as the
priority's number and title do not change, and changes when either does --
so an edited priority is "new" again, matching the ADR's "new or changed".

**Defect-decline escalation** (rule 1: "a defect declined three sessions
running is raised to the operator"): :func:`record_defect_declines` takes
the set of defect item ids the planner explicitly declined THIS session
(each with a non-blank reason -- the ADR requires the defect be *named* and
*why*), tracks CONSECUTIVE per-defect decline counts (any cycle where a
previously-declined defect is absent from the new decline set resets its
counter -- "three declines running" means consecutive, not cumulative), and
escalates at 3.

Wiring the planner's OUTPUT to actually populate ``declined`` (a
``roles/planner.md`` prompt change + ``_parse_planner_final_response``
schema addition) is a separate step -- prompt wording needs sign-off before
it is committed (see this PR's body). This module's counter/escalation
logic is independent of that and fully exercised by
:func:`record_defect_declines`'s own tests today.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_SEEN_PRIORITIES_RELPATH = ("planner", "seen_priority_ids.json")
_DEFECT_DECLINES_RELPATH = ("planner", "defect_declines.json")

#: ADR-035 rule 1: three consecutive declines of the same defect raise it
#: to the operator.
DEFECT_DECLINE_ESCALATION_THRESHOLD = 3


def _read_json(path: "Path") -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except Exception:
        return None


def _write_json(path: "Path", data: Any) -> None:
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


def mark_new_priority_items(state_dir: "Path", items: list[dict[str, str]]) -> set[str]:
    """Return the ids of ``kind == "priority"`` items in ``items`` not
    previously shown to the planner, and persist the updated seen-set so
    they read as "not new" on the next call.

    An item absent from ``items`` this call (e.g. a priority marked
    completed) is simply not re-persisted as newly-unseen -- the seen-set
    only grows, so a priority that reappears unchanged (same id) never
    re-reads as new; one that reappears CHANGED (new id, since the id is
    derived from number+title) does.
    """
    path = Path(state_dir).joinpath(*_SEEN_PRIORITIES_RELPATH)
    seen = _read_json(path)
    seen_ids: set[str] = set(seen.get("ids") or []) if isinstance(seen, dict) else set()

    priority_ids = {it["id"] for it in items if it.get("kind") == "priority" and it.get("id")}
    new_ids = priority_ids - seen_ids

    _write_json(path, {"schema": "planner-seen-priorities-v1", "ids": sorted(seen_ids | priority_ids)})
    return new_ids


def merge_proposer_candidates(
    items: list[dict[str, str]], proposer_items: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Splice ``proposer_items`` (kind ``"proposer"``,
    :func:`nanobot.runtime.llm_proposer.proposer_candidate_items`) into
    ``items`` (:func:`nanobot.runtime.demand.collect_demand`'s own output)
    at the position ADR-035 rule 1's trust order puts them: right after the
    contiguous run of ``kind == "defect"`` items (which always sit
    immediately after ``priority`` items in ``collect_demand``'s own
    ordering), before goal-gap and everything else. If there are no defect
    items, they go right after any priority items instead; if there are
    neither, at the front.

    ``items`` itself is never reordered -- only where the proposer's items
    are inserted is decided here.
    """
    if not proposer_items:
        return list(items)
    insert_at = 0
    for idx, item in enumerate(items):
        if item.get("kind") in ("priority", "defect"):
            insert_at = idx + 1
        else:
            break
    return items[:insert_at] + list(proposer_items) + items[insert_at:]


def render_candidates_block(
    items: list[dict[str, str]], new_priority_ids: "set[str] | None" = None, *, limit: int = 20,
) -> str:
    """Render ``items`` (as :func:`nanobot.runtime.demand.collect_demand`
    returns them -- order preserved, never re-sorted here) for the
    planner's context, tagging new priorities.

    Each line is ``N. [kind] summary (new)?`` -- the item's own already-
    capped ``summary`` (see ``demand._make_item``), never the fuller
    ``evidence``/instructions body a candidate might carry.
    """
    new_priority_ids = new_priority_ids or set()
    lines = ["## Ranked candidates (trust order: priority > defect > goal-gap > rest)"]
    for idx, item in enumerate(items[:limit], start=1):
        tag = " (new)" if item.get("kind") == "priority" and item.get("id") in new_priority_ids else ""
        lines.append(f"{idx}. [{item.get('kind', '?')}] {item.get('summary', '')}{tag}")
    if len(items) > limit:
        lines.append(f"... and {len(items) - limit} more, not shown")
    return "\n".join(lines)


def _defect_declines_path(state_dir: "Path") -> "Path":
    return Path(state_dir).joinpath(*_DEFECT_DECLINES_RELPATH)


def _load_declines(state_dir: "Path") -> dict[str, int]:
    raw = _read_json(_defect_declines_path(state_dir))
    if not isinstance(raw, dict) or not isinstance(raw.get("counts"), dict):
        return {}
    return {str(k): int(v) for k, v in raw["counts"].items() if isinstance(v, (int, float))}


def record_defect_declines(
    state_dir: "Path", cycle_id: str, declined: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Apply one session's defect declines (ADR-035 rule 1).

    ``declined`` maps defect item id -> the planner's stated reason; every
    reason must be non-blank (raises :class:`ValueError` naming the first
    offending id otherwise -- "must name the declined defect and why").

    Returns ``{defect_id: {"consecutive": int, "escalated": bool}}`` for
    every defect touched this call (declined now, or previously tracked and
    now reset). A defect's counter resets to 0 the first cycle it is absent
    from ``declined`` -- "three declines running" is consecutive.
    """
    for defect_id, reason in declined.items():
        if not reason or not reason.strip():
            raise ValueError(f"decline of {defect_id!r} has no reason")

    from nanobot.runtime.cycle_ledger import append_event

    counts = _load_declines(state_dir)
    result: dict[str, dict[str, Any]] = {}

    for defect_id in set(counts) | set(declined):
        if defect_id in declined:
            counts[defect_id] = counts.get(defect_id, 0) + 1
        else:
            counts[defect_id] = 0
        escalated = counts[defect_id] >= DEFECT_DECLINE_ESCALATION_THRESHOLD
        result[defect_id] = {"consecutive": counts[defect_id], "escalated": escalated}
        if escalated:
            append_event(state_dir, {
                "phase": "defect_decline_escalated",
                "cycle_id": cycle_id or "",
                "defect_id": defect_id,
                "consecutive": counts[defect_id],
            })

    counts = {k: v for k, v in counts.items() if v > 0}
    _write_json(_defect_declines_path(state_dir), {"schema": "planner-defect-declines-v1", "counts": counts})
    return result
