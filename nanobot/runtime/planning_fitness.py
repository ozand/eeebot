"""ADR-031 rule 7 (#1852): the planning session's overhead ratio.

Planning costs 20 ticks whatever happens. The design pays for itself only
when the execution half grows to use more of its own box -- so the ratio
(planning ticks / total ticks) is reported from day one, the same
unconditional-marker-row-plus-rolling-census shape ``diary_fitness`` and
``skill_fitness`` already established, so a withdrawal decision (ADR-031
rule 7, ADR-030 rule 1) is read from a number instead of an impression.

``planning_iterations``/``execution_iterations`` for one row both come from
the same place: the ``context_usage.iterations`` list length in each
subagent's own telemetry JSON (``state/subagents/<task_id>.json``) -- the
bridge reads both after the cycle finishes and hands them here. Neither
this module nor its caller invents a second iteration-counting mechanism.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CYCLE_SCAN_REL = "planning_fitness/cycle_scans.jsonl"  # state_dir-relative path
_MAX_CYCLE_SCANS = 2000
_WINDOW_DAYS = 30

CENSUS_SCHEMA = "planning-overhead-v1"
CENSUS_REL = "demand/planning_overhead.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _parse_ts(value: Any) -> "datetime | None":
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _read_cycle_scans(state_dir: Path) -> "list[dict[str, Any]] | None":
    """The recorded rows, or None when the source is unavailable -- the same
    missing/invalid-vs-empty distinction ``diary_fitness`` makes (#1342-class)."""
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


def record_cycle_overhead(
    state_dir: Path,
    *,
    cycle_id: str,
    planning_ran: bool,
    planning_iterations: "int | None",
    execution_iterations: "int | None",
) -> dict[str, Any]:
    """Append one unconditional marker row for this bridge invocation.

    ``planning_ran`` is False for a cycle where the planning session never
    ran or failed before producing any ticks -- distinct from
    ``planning_iterations == 0``, which would mean it ran and used none.
    ``ratio`` is ``planning_iterations / (planning_iterations +
    execution_iterations)`` when both are known integers, else ``None`` --
    never a fabricated 0.

    Returns the row written (empty dict on any error). Fail-open: an
    exception here must never break the cycle it is reporting on.
    """
    try:
        p_iters = planning_iterations if isinstance(planning_iterations, int) else None
        e_iters = execution_iterations if isinstance(execution_iterations, int) else None
        total = (p_iters or 0) + (e_iters or 0) if p_iters is not None and e_iters is not None else None
        row: dict[str, Any] = {
            "cycle_id": str(cycle_id),
            "ts": _utc_now(),
            "planning_ran": bool(planning_ran),
            "planning_iterations": p_iters,
            "execution_iterations": e_iters,
            "ratio": (p_iters / total) if total and p_iters is not None else None,
        }
        path = Path(state_dir) / CYCLE_SCAN_REL
        rows = _read_cycle_scans(state_dir) or []
        rows.append(row)
        rows = rows[-_MAX_CYCLE_SCANS:]
        _atomic_write(path, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        return row
    except Exception:
        return {}


def planning_overhead_rate(
    state_dir: Path, *, now: "datetime | None" = None, window_days: int = _WINDOW_DAYS
) -> dict[str, Any]:
    """Rolling-window mean overhead ratio over the last *window_days*.

    ``ok: False`` means no data (never rendered as a ratio of 0), the same
    fail-open contract every sibling census in this system uses.
    """
    try:
        now_dt = now or datetime.now(timezone.utc)
        cutoff_dt = now_dt - timedelta(days=window_days)
        rows = _read_cycle_scans(state_dir)
        if rows is None:
            return {
                "ok": False, "reason": "scans_unavailable",
                "window_days": window_days, "cycles_in_window": 0,
                "planning_ran_in_window": 0, "mean_ratio": None,
            }
        in_window = []
        for row in rows:
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts > now_dt or ts < cutoff_dt:
                continue
            in_window.append(row)
        ratios = [r["ratio"] for r in in_window if isinstance(r.get("ratio"), (int, float))]
        planning_ran = sum(1 for r in in_window if r.get("planning_ran") is True)
        return {
            "ok": True,
            "window_days": window_days,
            "cycles_in_window": len(in_window),
            "planning_ran_in_window": planning_ran,
            "mean_ratio": (sum(ratios) / len(ratios)) if ratios else None,
        }
    except Exception:
        return {
            "ok": False, "reason": "census_error",
            "window_days": window_days, "cycles_in_window": 0,
            "planning_ran_in_window": 0, "mean_ratio": None,
        }


def write_planning_overhead_rate(state_dir: Path, *, now: "datetime | None" = None) -> dict[str, Any]:
    """Write the rolling overhead-ratio census to ``demand/planning_overhead.json``.

    Report-only, harness-side, written every cycle regardless of that
    cycle's own outcome -- same contract as ``diary_fitness.write_diary_read_rate``.
    """
    result = planning_overhead_rate(state_dir, now=now)
    payload = {
        "schema": CENSUS_SCHEMA,
        "written_at": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        **result,
    }
    path = Path(state_dir) / CENSUS_REL
    try:
        _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    except Exception:
        return {"ok": False, "written": False, "path": str(path)}
    return {"ok": result["ok"], "written": True, "path": str(path)}
