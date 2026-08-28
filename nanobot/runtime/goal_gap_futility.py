"""Deterministic goal-gap futility tracking (#996).

Stdlib-only and fail-open.  The demand layer supplies current scorecard gaps;
this module records bounded observations and suppresses a gap only after a
bounded number of successful integrated proposals without metric movement.
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


def _rows(state_dir: Path) -> list[dict[str, Any]]:
    try:
        path = Path(state_dir) / "ledger" / "cycles.jsonl"
        if not path.is_file() or path.stat().st_size > _MAX_LEDGER_BYTES:
            return []
        result = []
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


def _integrated_count(
    state_dir: Path,
    gap_id: str,
    after: datetime,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> int:
    proposed: set[str] = set()
    successful: set[str] = set()
    rows = ledger_rows if ledger_rows is not None else _rows(state_dir)
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
