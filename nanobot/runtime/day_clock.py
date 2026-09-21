"""ADR-026 ("the day is a cycle"): the one shared definition of the day
boundary, plus the harness-computed facts that make the step/cycle/day
clocks visible to the executor (#1793).

Single source, per ADR-026 decision 4 and #1793 item 4: the day boundary
used here (:data:`DAY_BOUNDARY_HOUR_LOCAL`, :func:`day_start`) is the same
definition a future deep-sleep job (not built by this issue -- see the
Non-goals in #1793) must import rather than re-derive, so the block's
"hours to deep sleep" and the job's own trigger can never drift apart the
way ``WINDOW_TOKENS`` did before #1776.

Action, not verdict (ADR-026 decision 2, ADR-023): :func:`day_actions`
reports commit counts and file paths only -- mechanical facts about what
happened -- never a score, rating, or quality claim about whether the work
was good. Reflecting the loop's own commits back to it adds no authority
they did not already have; a verdict would need to pass ADR-023's
provenance test (every input outside the loop's writable surface), which a
"was this good" judgement about the loop's own commits structurally
cannot.

Fail-open throughout, matching every other harness-owned measurement in
this codebase (#1766, #1773): an unreadable ledger reports
``status: "unavailable"``, never a fabricated zero.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime.state_access import evidence_status, ledger_window

#: The nightly cluster is scheduled in the host's local clock: the curator's
#: ``OnCalendar=daily`` fires at local midnight, followed by action index at
#: 00:05 and the later narrator at 03:30. Keep this live boundary on that
#: local clock. This is deliberately separate from #1831's stored-day-key
#: migration; no persisted key is read or rewritten here.
DAY_BOUNDARY_HOUR_LOCAL = 0

#: Length of a day in this ontology (ADR-026 decision 1's "day" clock).
DAY_HOURS = 24

#: ADR-026 decision 3's three deliverable stages, in order. No pipeline
#: exists yet (#1793 non-goal: building it is a separate issue), so
#: :func:`deliverable_stage` always returns the first element today --
#: never inferred, never a fabricated higher stage.
DELIVERABLE_STAGES = ("none", "rendered", "published", "observed")

#: Bounded list of files named in the day-actions block, matching the
#: bounded-list precedent elsewhere in this codebase (e.g. the skills
#: catalogue truncation, #1732) -- an unbounded file list could grow the
#: block without limit across a very active day.
_MAX_FILES_LISTED = 10

#: Words that would turn an action report into a verdict (ADR-026 decision
#: 2). Used only by this module's own tests to prove the rendered text
#: never contains one -- never referenced by application logic, since the
#: guarantee comes from what the code chooses to emit, not from filtering
#: it after the fact.
VERDICT_WORDS = (
    "good", "great", "excellent", "valuable", "quality", "score", "rating",
    "success rate", "well done", "impressive", "poor", "bad", "failure rate",
)


def day_start(now: "datetime | None" = None) -> datetime:
    """Host-local midnight on or before *now* -- the live day boundary."""
    now = (now or datetime.now().astimezone()).astimezone()
    return now.replace(hour=DAY_BOUNDARY_HOUR_LOCAL, minute=0, second=0, microsecond=0)


def day_position(now: "datetime | None" = None) -> dict[str, Any]:
    """Hours elapsed since the day started and hours remaining to deep
    sleep -- the day half of ADR-026 decision 1's three clocks."""
    now = (now or datetime.now().astimezone()).astimezone()
    start = day_start(now)
    elapsed_hours = (now - start).total_seconds() / 3600.0
    remaining_hours = max(0.0, DAY_HOURS - elapsed_hours)
    return {
        "hours_elapsed": round(elapsed_hours, 1),
        "hours_to_deep_sleep": round(remaining_hours, 1),
    }


def deliverable_stage() -> str:
    """ADR-026 decision 3: rendered / published / observed, honestly
    staged. No stage is ever inferred from a side effect (a render
    completing, a publish call returning success) -- only recorded
    evidence would move this past "none", and no such evidence source
    exists yet. Returns the earliest stage until one does."""
    return DELIVERABLE_STAGES[0]


def day_actions(state_dir: "Path | str", now: "datetime | None" = None) -> dict[str, Any]:
    """Today's integrated commits and the files they touched -- actions,
    never a verdict (ADR-026 decision 2). ``status`` is
    :func:`state_access.evidence_status` for the day's ledger window:
    ``'complete'``/``'partial'``/``'unavailable'`` -- an unreadable ledger
    is reported as unavailable, never folded into a zero count (the same
    discipline #1773's ``integration_class_counts`` applies).
    """
    now = (now or datetime.now().astimezone()).astimezone()
    start = day_start(now)
    since_ts = start.isoformat().replace("+00:00", "Z")
    try:
        window = ledger_window(Path(state_dir), since_ts=since_ts, phases=frozenset({"outcome"}))
    except Exception:
        return {
            "status": "unavailable", "commits_integrated_today": 0,
            "files_touched_today": [], "files_touched_today_total": 0,
        }
    commits = 0
    files: list[str] = []
    seen: set[str] = set()
    for event in window.rows:
        outcome = str(event.get("outcome") or "").strip().lower()
        if outcome not in ("success", "pushed_late"):
            continue
        ts = _parse_ts(event.get("ts"))
        if ts is None or ts < start:
            continue
        commits += 1
        for path in event.get("files_changed") or []:
            if isinstance(path, str) and path and path not in seen:
                seen.add(path)
                files.append(path)
    files.sort()
    return {
        "status": evidence_status(window),
        "commits_integrated_today": commits,
        "files_touched_today": files[:_MAX_FILES_LISTED],
        "files_touched_today_total": len(files),
    }


def _parse_ts(value: Any) -> "datetime | None":
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
