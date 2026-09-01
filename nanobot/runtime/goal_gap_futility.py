"""Deterministic goal-gap futility tracking (#996).

Stdlib-only and fail-open.  The demand layer supplies current scorecard gaps;
this module records bounded observations and suppresses a gap only after a
bounded number of successful integrated proposals without metric movement.

#1166: ledger reading is rotation-aware — the active ``cycles.jsonl`` is read
plus any rotated ``cycles-YYYY-MM-DD.jsonl.gz`` archives whose day falls at or
after the gap's ``first_seen_ts`` horizon. This is *bounded*: only as many
archives as the horizon requires are loaded, never an unbounded full scan.  The
oversized-active-file case is distinguishable from an empty file via
``_OVERSIZED`` (see :data:`_OVERSIZED`) so a stuck counter can be diagnosed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DEFAULT_THRESHOLD = 10
_DEFAULT_TTL_DAYS = 14
_EPSILON = 1e-6
_MAX_LEDGER_BYTES = 2 * 1024 * 1024

# Sentinel returned by _rows_active when the active ledger file exceeds
# _MAX_LEDGER_BYTES. Distinct from [] so callers can log/observe the
# oversize case instead of silently treating it as "nothing happened".
# (#1166 secondary defect fix.)
_OVERSIZED = object()


def _threshold() -> int:
    try:
        return max(1, int(os.environ.get("SELFEVO_GOAL_GAP_FUTILITY_THRESHOLD", "10")))
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD


def _ttl_days() -> int:
    try:
        return max(1, int(os.environ.get("SELFEVO_GOAL_GAP_FUTILITY_TTL_DAYS", "14")))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_DAYS


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sidecar(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "futility.json"


def _load(state_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_sidecar(state_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(state_dir: Path, records: dict[str, dict[str, Any]]) -> None:
    try:
        target = _sidecar(state_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(target)
    except Exception:
        pass


def _rows_active(state_dir: Path) -> list[dict[str, Any]] | object:
    """Read the active (un-rotated) ledger file.

    Returns a list of parsed dict rows, or the :data:`_OVERSIZED` sentinel
    when the file exceeds *_MAX_LEDGER_BYTES* (#1166: oversize is
    distinguishable from an empty file so callers can log rather than silently
    treat it as zero attempts).
    """
    try:
        path = Path(state_dir) / "ledger" / "cycles.jsonl"
        if not path.is_file():
            return []
        if path.stat().st_size > _MAX_LEDGER_BYTES:
            return _OVERSIZED  # type: ignore[return-value]
        result: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                result.append(row)
        return result
    except Exception:
        return []


def _rows_archives(state_dir: Path, horizon: datetime) -> list[dict[str, Any]]:
    """Read only rotated archives in the gap's own tracking horizon.

    The lower bound is the record's ``first_seen_ts`` rather than a fixed
    archive count.  This is the smallest window that can contain an attempt
    for this gap, while remaining bounded by that timestamp: archives before
    the horizon are never opened.  The active ledger is read separately.
    """
    import gzip as _gzip

    ledger_dir = Path(state_dir) / "ledger"
    if not ledger_dir.is_dir():
        return []
    horizon_day = horizon.astimezone(timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    result: list[dict[str, Any]] = []
    try:
        archives = sorted(ledger_dir.glob("cycles-*.jsonl.gz"), reverse=True)
    except Exception:
        return []
    for gz_path in archives:
        name = gz_path.name
        if not (name.startswith("cycles-") and name.endswith(".jsonl.gz")):
            continue
        day_text = name[len("cycles-"):-len(".jsonl.gz")]
        try:
            archive_day = datetime.strptime(day_text, "%Y-%m-%d").date()
        except ValueError:
            continue
        if archive_day > today:
            continue
        if archive_day < horizon_day:
            break
        try:
            with _gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(row, dict):
                        result.append(row)
        except Exception:
            continue
    return result


def _rows(state_dir: Path, horizon: datetime | None = None) -> list[dict[str, Any]]:
    """Read ledger rows for the futility path (#1166 rotation-aware).

    When *horizon* is given (a gap's ``first_seen_ts``), rotated ``.gz``
    archives whose filename day >= horizon's date are also read and prepended,
    so a ``proposed`` row written on a prior day and its matching
    ``outcome:success`` row in today's active file both appear in the result.
    The scan is bounded by *horizon* — no archive older than that date is
    read.

    The active file is always read first; if it exceeds *_MAX_LEDGER_BYTES*
    the :data:`_OVERSIZED` sentinel is noted and an empty active slice is used
    (fail-open: archives are still scanned normally).
    """
    active = _rows_active(state_dir)
    if active is _OVERSIZED:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "goal_gap_futility: active ledger exceeds %d bytes; "
            "active rows skipped for futility counting (oversized, not empty)",
            _MAX_LEDGER_BYTES,
        )
        active_rows: list[dict[str, Any]] = []
    else:
        active_rows = active  # type: ignore[assignment]
    if horizon is None:
        return active_rows
    archive_rows = _rows_archives(state_dir, horizon)
    return archive_rows + active_rows


def _integrated_count(
    state_dir: Path,
    gap_id: str,
    after: datetime,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> int:
    proposed: set[str] = set()
    successful: set[str] = set()
    # #1166: when ledger_rows is provided (from demand.py's collect_demand,
    # active file only), supplement with rotation-aware archive rows so a
    # proposed→success pair split by midnight rotation is counted correctly.
    # When ledger_rows is None, _rows handles both active and archives.
    if ledger_rows is not None:
        rows = _rows_archives(state_dir, after) + ledger_rows
    else:
        rows = _rows(state_dir, horizon=after)
    for row in rows:
        cycle = str(row.get("cycle_id") or "").strip()
        if not cycle:
            continue
        ts = _parse_ts(row.get("ts") or row.get("timestamp"))
        if ts is None or ts <= after:
            continue
        if row.get("phase") == "proposed" and str(row.get("demand_id") or "") == gap_id:
            proposed.add(cycle)
        elif row.get("phase") == "outcome" and row.get("outcome") == "success":
            # Current bridge success is an integrated cycle.  Legacy explicit
            # integrated=False remains excluded if present.
            if row.get("integrated", True) is not False:
                successful.add(cycle)
    return len(proposed & successful)


def _delta(first: Any, current: Any) -> float | None:
    try:
        return round(float(current) - float(first), 12)
    except (TypeError, ValueError):
        return None


def _improved(direction: str, first: Any, current: Any) -> bool:
    try:
        delta = float(current) - float(first)
    except (TypeError, ValueError):
        return False
    if direction == "max":
        return delta < -_EPSILON
    if direction == "min":
        return delta > _EPSILON
    return False


def _emit(state_dir: Path, record: dict[str, Any], futile: bool) -> None:
    try:
        from nanobot.runtime.cycle_ledger import append_event
        append_event(state_dir, {
            "phase": "goal_gap_futile",
            "gap_id": record.get("gap_id", ""),
            "metric": record.get("metric", ""),
            "attempt_count": record.get("attempt_count", 0),
            "metric_delta": record.get("metric_delta"),
            "futile": futile,
        })
    except Exception:
        pass


def _update(
    state_dir: Path,
    gap: dict[str, Any],
    records: dict[str, dict[str, Any]],
    now: datetime,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> bool:
    gap_id = str(gap.get("id") or "").strip()
    if not gap_id:
        return False
    record = records.get(gap_id)
    if not isinstance(record, dict):
        record = {
            "gap_id": gap_id,
            "metric": str(gap.get("metric") or ""),
            "first_seen_ts": _iso(now),
            "first_metric": gap.get("current"),
            "current_metric": gap.get("current"),
            "metric_delta": 0.0,
            "attempt_count": 0,
            "futile": False,
        }
    until = _parse_ts(record.get("futile_until"))
    if until is not None and now < until:
        record["current_metric"] = gap.get("current")
        record["metric_delta"] = _delta(record.get("first_metric"), gap.get("current"))
        records[gap_id] = record
        return True
    if until is not None and now >= until:
        record = {
            "gap_id": gap_id,
            "metric": str(gap.get("metric") or ""),
            "first_seen_ts": _iso(now),
            "first_metric": gap.get("current"),
            "current_metric": gap.get("current"),
            "metric_delta": 0.0,
            "attempt_count": 0,
            "futile": False,
        }
    first_seen = _parse_ts(record.get("first_seen_ts")) or now
    record["metric"] = str(gap.get("metric") or record.get("metric") or "")
    record["current_metric"] = gap.get("current")
    record["metric_delta"] = _delta(record.get("first_metric"), gap.get("current"))
    record["attempt_count"] = _integrated_count(state_dir, gap_id, first_seen, ledger_rows=ledger_rows)
    now_futile = (
        record["attempt_count"] >= _threshold()
        and not _improved(str(gap.get("direction") or ""), record.get("first_metric"), gap.get("current"))
    )
    was_futile = bool(record.get("futile")) and until is not None and now < until
    record["futile"] = now_futile
    if now_futile:
        record["futile_until"] = _iso(now + timedelta(days=_ttl_days()))
    else:
        record.pop("futile_until", None)
    records[gap_id] = record
    if now_futile != was_futile:
        _emit(state_dir, record, now_futile)
    return now_futile


def futile_gap_ids(
    state_dir: Path,
    gaps: list[dict[str, Any]],
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Update current gaps and return the IDs currently suppressed."""
    try:
        records = _load(state_dir)
        now = datetime.now(timezone.utc)
        result = {
            str(gap.get("id"))
            for gap in gaps
            if _update(state_dir, gap, records, now, ledger_rows=ledger_rows)
        }
        _save(state_dir, records)
        return {item for item in result if item}
    except Exception:
        return set()


def futility_snapshot(state_dir: Path) -> dict[str, Any]:
    try:
        records = _load(state_dir)
        return {
            "futile_gap_ids": sorted(key for key, value in records.items() if value.get("futile")),
            "total_tracked": len(records),
        }
    except Exception:
        return {"futile_gap_ids": [], "total_tracked": 0}
