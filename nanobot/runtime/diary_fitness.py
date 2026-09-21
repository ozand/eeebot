"""ADR-028 rule 5: whether a cycle read today's diary, and at which
tool-call position -- derived from the recorded tool calls, not
self-reported by the loop.

Reuses the exact sidecar shape #939 Part C already proved for skills
(``nanobot.runtime.skill_fitness``): one unconditional marker row per
cycle in a bounded ``.jsonl``, plus a rolling-window read-rate census
computed from those rows. This module does not invent a second
scan-rate mechanism -- see the discussion on issue #1812, which names
``skill_fitness.py``'s ``record_cycle_skill_scan``/``census`` pair as
the pattern to reuse rather than re-derive.

The one real difference from the skill case: skills are many, named
artifacts, and the census asks "which of them got zero reads". The
diary is one artifact per day, and the question is simpler -- "was
*today's* diary read this cycle, and how early" -- so there is no
per-artifact join, no rename map, no birth-use guard. A cycle cannot
game this by writing its own diary before reading it the way a skill
author could game a same-cycle skill read: the obligation (ADR-028
rule 5) is to read the diary as the FIRST action, and the position
recorded here is exactly what lets that be checked later, not merely
asserted.

ADR-028 rule 4 (never in the prompt): this module is imported and
called only from the bridge, after the spawn window closes -- see
``nanobot.runtime.bridge``'s cycle-completion path, which calls
:meth:`nanobot.agent.subagent.SubagentManager.collect_day_file_reads`
for the raw read list and does the interpretation here. It has no
import of, and no reader in, ``nanobot.agent.context`` or
``nanobot.agent.subagent``'s prompt-assembly path -- rule 4 forbids
even this module's own name from appearing in either file's source.

Clock choice (ADR-029 / #1831): the "is this TODAY's diary" check in
:func:`_today` runs on UTC, matching ``day_diary.diary_relpath``'s own
default day boundary -- see :func:`_today`'s docstring for the single
place this is decided. The rolling read-RATE window in
:func:`diary_read_rate` (``_WINDOW_DAYS``) is a plain elapsed-time
cutoff over cycle timestamps (``now - timedelta(days=30)``), not a
calendar-day-bucketed window, so it carries no local-vs-UTC
day-boundary ambiguity of its own -- only :func:`_today` does.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "diary-fitness-v1"
CYCLE_SCAN_REL = "diary_fitness/cycle_scans.jsonl"  # state_dir-relative path
_MAX_CYCLE_SCANS = 2000
_WINDOW_DAYS = 30

CENSUS_SCHEMA = "diary-read-rate-v1"
CENSUS_REL = "demand/diary_read_rate.json"  # same demand/ home as the skill and lesson censuses


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _today() -> str:
    """The day boundary this module checks a read against.

    CLOCK CHOICE, visible in this one place: UTC. Mirrors
    ``nanobot.runtime.day_diary.diary_relpath``'s own default (the file
    the loop actually writes/reads is named on this same clock, at this
    module's line 46/75) -- the two must agree or "did the cycle read
    today's diary" would compare a UTC-keyed read against a locally-keyed
    file, or vice versa.

    ADR-029 / #1831: the day boundary is moving to LOCAL time
    system-wide (systemd timers and ``bridge.py``'s ``date.today()`` are
    local; ledger/telemetry/action-index rotation is UTC -- a 3-hour
    daily disagreement on which day it is). This function and
    ``day_diary.diary_relpath``'s default are both instances of that
    class and belong in #1831's census of readers to migrate together --
    changing one without the other would break the comparison this
    module depends on.
    """
    return datetime.now(timezone.utc).date().isoformat()


def _parse_ts(value: Any) -> "datetime | None":
    """Timezone-aware ISO-8601 timestamp, or None (naive, malformed, non-string)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _read_cycle_scans(state_dir: Path) -> "list[dict[str, Any]] | None":
    """The recorded rows, or None when the source is unavailable.

    Distinguishes "no reads recorded" (a valid, empty file) from "no data"
    (missing file, invalid JSON) -- only the first is evidence a cycle ran
    with this instrumentation and read nothing (#1342-class distinction).
    """
    path = Path(state_dir) / CYCLE_SCAN_REL
    try:
        if not path.is_file():
            return None
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        return rows
    except Exception:
        return None


def record_cycle_diary_read(
    state_dir: Path,
    *,
    cycle_id: str,
    reads: "list[dict[str, Any]] | None" = None,
    wrote: bool = False,
) -> dict[str, Any]:
    """Append one unconditional marker row for this cycle.

    *reads* is the raw list of ``{"day": str, "position": int}`` the
    subagent's ``read_file`` instrumentation collected this cycle (any
    day, not only today's) -- see
    :meth:`nanobot.agent.subagent.SubagentManager.collect_day_file_reads`.

    ``diary_read`` is True iff at least one of *reads* named TODAY's date
    (UTC, matching the day the diary itself is keyed by -- see
    ``day_diary.diary_relpath``'s default). ``tool_call_position`` is the
    smallest ``position`` among today's reads, i.e. how early in the
    cycle the obligation was met -- or ``None`` when it was not met at
    all this cycle.

    *wrote* (#1844) is whether TODAY's diary path appears among this
    cycle's own changed files -- the caller (the bridge, from the cycle's
    ``files_changed`` diff) decides this, the same way it decides
    ``diary_read`` from the raw read list; this module only records what
    it is told. Deliberately NOT gated on whether the cycle integrated:
    ``files_changed`` reflects the cycle's own commits regardless of the
    later verdict, and gating this on integration would make a rejected
    cycle's diary write indistinguishable from one that never attempted
    it -- the same reasoning the bridge already applies to ``diary_read``
    (see its call site's comment).

    A row is written every time this is called, whether or not anything
    was read or written -- the same unconditional-marker discipline as
    ``skill_fitness.record_cycle_skill_scan``, so a cycle that touched
    neither is distinguishable from a cycle where this was never called
    (instrumentation off, an older release, a crash).

    Returns the row written (empty dict on any error). Fail-open: an
    exception here must never break the cycle it is reporting on.
    """
    try:
        today = _today()
        today_positions = [
            int(r["position"])
            for r in (reads or [])
            if isinstance(r, dict) and r.get("day") == today and isinstance(r.get("position"), int)
        ]
        row: dict[str, Any] = {
            "cycle_id": str(cycle_id),
            "ts": _utc_now(),
            "day": today,
            "diary_read": bool(today_positions),
            "tool_call_position": min(today_positions) if today_positions else None,
            "days_read": sorted({str(r.get("day")) for r in (reads or []) if isinstance(r, dict) and r.get("day")}),
            "diary_written": bool(wrote),
        }
        path = Path(state_dir) / CYCLE_SCAN_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = _read_cycle_scans(state_dir) or []
        rows.append(row)
        rows = rows[-_MAX_CYCLE_SCANS:]
        tmp_path = path.with_suffix(".jsonl.tmp")
        tmp_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8",
        )
        tmp_path.replace(path)
        return row
    except Exception:
        return {}


def diary_read_rate(
    state_dir: Path, *, now: "datetime | None" = None, window_days: int = _WINDOW_DAYS
) -> dict[str, Any]:
    """Rolling-window read rate: what fraction of recorded cycles read
    today's diary as their first action, over the last *window_days*.

    ``ok: False`` with no rate published means "no data" (instrumentation
    never ran / file missing / corrupt) -- never rendered as a rate of 0,
    which would misreport "nobody reads it" as "we measured and nobody
    reads it" (#1342-class distinction, same fail-open contract as
    ``skill_fitness.census``).
    """
    try:
        now_dt = now or datetime.now(timezone.utc)
        cutoff_dt = now_dt - timedelta(days=window_days)
        rows = _read_cycle_scans(state_dir)
        if rows is None:
            return {
                "ok": False, "reason": "reads_unavailable",
                "window_days": window_days, "cycles_in_window": 0,
                "reads_in_window": 0, "rate": None,
            }
        in_window = []
        for row in rows:
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts > now_dt or ts < cutoff_dt:
                continue
            in_window.append(row)
        reads_in_window = sum(1 for r in in_window if r.get("diary_read") is True)
        cycles_in_window = len(in_window)
        return {
            "ok": True,
            "window_days": window_days,
            "cycles_in_window": cycles_in_window,
            "reads_in_window": reads_in_window,
            "rate": (reads_in_window / cycles_in_window) if cycles_in_window else None,
        }
    except Exception:
        return {
            "ok": False, "reason": "census_error",
            "window_days": window_days, "cycles_in_window": 0,
            "reads_in_window": 0, "rate": None,
        }


def diary_write_rate(
    state_dir: Path, *, now: "datetime | None" = None, window_days: int = _WINDOW_DAYS
) -> dict[str, Any]:
    """Rolling-window write rate: what fraction of recorded cycles added
    content to today's diary, over the last *window_days*.

    #1844: the write-side sibling of :func:`diary_read_rate` -- same
    rows, same window, same fail-open ``ok: False``-means-no-data
    contract (never a published rate of 0 standing in for "never
    measured").
    """
    try:
        now_dt = now or datetime.now(timezone.utc)
        cutoff_dt = now_dt - timedelta(days=window_days)
        rows = _read_cycle_scans(state_dir)
        if rows is None:
            return {
                "ok": False, "reason": "writes_unavailable",
                "window_days": window_days, "cycles_in_window": 0,
                "writes_in_window": 0, "write_rate": None,
            }
        in_window = []
        for row in rows:
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts > now_dt or ts < cutoff_dt:
                continue
            in_window.append(row)
        writes_in_window = sum(1 for r in in_window if r.get("diary_written") is True)
        cycles_in_window = len(in_window)
        return {
            "ok": True,
            "window_days": window_days,
            "cycles_in_window": cycles_in_window,
            "writes_in_window": writes_in_window,
            "write_rate": (writes_in_window / cycles_in_window) if cycles_in_window else None,
        }
    except Exception:
        return {
            "ok": False, "reason": "census_error",
            "window_days": window_days, "cycles_in_window": 0,
            "writes_in_window": 0, "write_rate": None,
        }


def write_diary_read_rate(
    state_dir: Path, *, now: "datetime | None" = None
) -> dict[str, Any]:
    """Write the rolling-window read AND write rate to
    ``demand/diary_read_rate.json``.

    Report-only, harness-side, written every cycle regardless of that
    cycle's own outcome -- the census reads the cycle-scan ledger
    directly, not the current cycle's own commit (same contract as
    ``skill_fitness.write_zero_read_census``).

    #1844: carries the write-rate fields (``writes_in_window``,
    ``write_rate``) alongside the read-rate fields in the same payload,
    rather than a second file -- one more unread artifact family is
    exactly the failure mode this system has already produced enough of.
    """
    result = diary_read_rate(state_dir, now=now)
    write_result = diary_write_rate(state_dir, now=now)
    payload = {
        "schema": CENSUS_SCHEMA,
        "written_at": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        **result,
        "writes_in_window": write_result.get("writes_in_window", 0),
        "write_rate": write_result.get("write_rate"),
    }
    path = Path(state_dir) / CENSUS_REL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        return {"ok": False, "written": False, "path": str(path)}
    return {"ok": result["ok"], "written": True, "path": str(path)}
