"""Deterministic, bounded goal-gap futility tracking (#996, #1166, #1175, #1184, #1211).

Stdlib-only, fail-open. Counts attempts against each scorecard gap and suppresses
only flat gaps after the threshold. Attempt unit (#1184/#1211): a gap with a
lever surface (``gap["surface"]`` from ``scorecard``: stale feed names,
registered held-out checkers, failing compile paths) counts every integrated
cycle from any lane except ``defect`` whose ``files_changed`` hits the surface
(``attempt_unit: lever_surface``). Other gaps count every terminal cycle linked
to their demand id (``attempt_unit: demand_id``), including suppressed attempts:
the live 79-suppression gap must not read as zero merely because the guards did
their job before integration. Rows come from ``state_access.ledger_window``
(#1175): a partial window may raise a persisted count, never lower it; an
unavailable window leaves the verdict as it was.
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
_PHASES = frozenset({"proposed", "outcome"})
_STATUS_RANK = {"complete": 0, "partial": 1, "unavailable": 2}
# Lanes whose integrations never count as attempts on a lever surface: a broken
# feed or checker script must stay repairable (#1184, measured on the host
# 2026-09-02: 0 of the 10 stale_feeds surface hits came from the defect lane;
# 5 of the 9 heldout_gap ones did, and those moved the metric).
_EXEMPT_LANES = frozenset({"defect"})
_MAX_ATTEMPT_SOURCES = 20
ATTEMPT_UNIT_SURFACE = "lever_surface"
ATTEMPT_UNIT_ID = "demand_id"

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

def _window(state_dir: Path, after: datetime):
    """``proposed``/``outcome`` rows since ``after`` across the live ledger and
    its rotated archives (``state_access.ledger_window``)."""
    from nanobot.runtime.state_access import ledger_window

    return ledger_window(Path(state_dir), since_ts=_iso(after), phases=_PHASES)

def _worse(left: str, right: str) -> str:
    return left if _STATUS_RANK.get(left, 2) >= _STATUS_RANK.get(right, 2) else right

def _evidence(
    state_dir: Path,
    after: datetime,
    ledger_rows: list[dict[str, Any]] | None = None,
    archive_rows: list[dict[str, Any]] | None = None,
    archive_status: str = "complete",
) -> tuple[list[dict[str, Any]], str]:
    """Rows since ``after`` plus the evidence status they were read under.

    ``ledger_rows`` is demand's shared window (``demand.LedgerRows`` carries
    its status; a plain list reads as complete); ``archive_rows`` is the
    horizon read ``futile_gap_ids`` performs once for all gaps. Without
    either, the module reads its own window.
    """
    from nanobot.runtime.state_access import evidence_status

    if ledger_rows is None:
        window = _window(state_dir, after)
        return list(window.rows), evidence_status(window)
    status = str(getattr(ledger_rows, "status", "complete") or "complete")
    if archive_rows is None:
        window = _window(state_dir, after)
        archive_rows, archive_status = list(window.rows), evidence_status(window)
    return list(archive_rows) + list(ledger_rows), _worse(status, archive_status)

def _lane(demand_id: str) -> str:
    """Demand lane = the id prefix (``reflection-…`` → ``reflection``)."""
    return demand_id.split("-", 1)[0] if demand_id else ""

def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./")

def surface_hits(surface: list[str], paths: list[Any]) -> bool:
    """True when any path equals or contains any surface entry (feed names are
    tokens such as ``host_metrics``; checker and compile entries are paths)."""
    entries = [_norm(entry) for entry in surface if _norm(entry)]
    return any(entry == path or entry in path for path in (_norm(p) for p in paths) if path for entry in entries)

def _demand_attempt_count(rows: list[dict[str, Any]], gap_id: str, after: datetime) -> int:
    """Terminal cycles after ``after`` whose proposal serves ``gap_id``.

    A demand attempt is capacity spent to a terminal outcome, not only an
    integration. Suppression remains correct and unchanged; counting it prevents
    repeated guarded attempts from laundering themselves into ``attempt_count=0``.
    Sets keep duplicate ledger rows from double-counting, while a proposal with
    no terminal outcome remains pending and does not count.
    """
    proposed: set[str] = set()
    terminal: set[str] = set()
    for row in rows:
        cycle = str(row.get("cycle_id") or "").strip()
        if not cycle:
            continue
        ts = _parse_ts(row.get("ts") or row.get("timestamp"))
        if ts is None or ts <= after:
            continue
        if row.get("phase") == "proposed" and str(row.get("demand_id") or "") == gap_id:
            proposed.add(cycle)
        elif row.get("phase") == "outcome" and row.get("outcome"):
            terminal.add(cycle)
    return len(proposed & terminal)

def _surface_attempts(rows: list[dict[str, Any]], surface: list[str], after: datetime) -> list[dict[str, str]]:
    """Integrated cycles after ``after`` whose ``files_changed`` hit ``surface``,
    from any lane except :data:`_EXEMPT_LANES`, oldest first, one per cycle. A
    cycle without a ``proposed`` row has no lane and is not counted."""
    demand_by_cycle: dict[str, str] = {}
    for row in rows:
        if row.get("phase") == "proposed" and row.get("cycle_id"):
            demand_by_cycle[str(row["cycle_id"]).strip()] = str(row.get("demand_id") or "").strip()
    attempts: dict[str, dict[str, str]] = {}
    for row in rows:
        cycle = str(row.get("cycle_id") or "").strip()
        if row.get("phase") != "outcome" or row.get("outcome") != "success" or cycle not in demand_by_cycle:
            continue
        if row.get("integrated", True) is False or _lane(demand_by_cycle[cycle]) in _EXEMPT_LANES:
            continue
        ts = _parse_ts(row.get("ts") or row.get("timestamp"))
        if ts is None or ts <= after or not surface_hits(surface, row.get("files_changed") or []):
            continue
        attempts[cycle] = {"cycle_id": cycle, "demand_id": demand_by_cycle[cycle], "ts": _iso(ts)}
    return sorted(attempts.values(), key=lambda item: item["ts"])

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
            "attempt_unit": record.get("attempt_unit", ATTEMPT_UNIT_ID),
            "metric_delta": record.get("metric_delta"),
            "futile": futile,
        })
    except Exception:
        pass

def _fresh(gap_id: str, gap: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "gap_id": gap_id, "metric": str(gap.get("metric") or ""), "first_seen_ts": _iso(now),
        "first_metric": gap.get("current"), "current_metric": gap.get("current"), "metric_delta": 0.0,
        "attempt_count": 0, "futile": False, "stale": False,
        "futility_status": "measured", "last_evaluated_ts": _iso(now),
    }

def _update(
    state_dir: Path,
    gap: dict[str, Any],
    records: dict[str, dict[str, Any]],
    now: datetime,
    ledger_rows: list[dict[str, Any]] | None = None,
    archive_rows: list[dict[str, Any]] | None = None,
    archive_status: str = "complete",
) -> bool:
    gap_id = str(gap.get("id") or "").strip()
    if not gap_id:
        return False
    record = records.get(gap_id)
    if not isinstance(record, dict):
        record = _fresh(gap_id, gap, now)
    record["stale"] = False
    record["futility_status"] = "measured"
    record["last_evaluated_ts"] = _iso(now)
    until = _parse_ts(record.get("futile_until"))
    if until is not None and now < until:
        record["current_metric"] = gap.get("current")
        record["metric_delta"] = _delta(record.get("first_metric"), gap.get("current"))
        records[gap_id] = record
        return True
    if until is not None and now >= until:
        record = _fresh(gap_id, gap, now)
    first_seen = _parse_ts(record.get("first_seen_ts")) or now
    record["metric"] = str(gap.get("metric") or record.get("metric") or "")
    record["current_metric"] = gap.get("current")
    record["metric_delta"] = _delta(record.get("first_metric"), gap.get("current"))
    rows, status = _evidence(state_dir, first_seen, ledger_rows, archive_rows, archive_status)
    record["window_status"] = status
    if status == "unavailable":
        # #1175 rule (3): no evidence this cycle; keep the persisted count and
        # verdict, only the metric view above was refreshed.
        records[gap_id] = record
        return False
    raw_surface = gap.get("surface")
    surface = [str(entry) for entry in raw_surface if str(entry).strip()] if isinstance(raw_surface, list) else []
    attempts = _surface_attempts(rows, surface, first_seen) if surface else []
    counted = len(attempts) if surface else _demand_attempt_count(rows, gap_id, first_seen)
    if status == "partial":
        # #1175 rule (2): a partial window is a lower bound — it may raise the
        # persisted count (and suppress on it), never lower it.
        counted = max(int(record.get("attempt_count") or 0), counted)
    record["attempt_count"] = counted
    record["attempt_unit"] = ATTEMPT_UNIT_SURFACE if surface else ATTEMPT_UNIT_ID
    record["surface"] = surface
    record["attempt_sources"] = attempts[-_MAX_ATTEMPT_SOURCES:]
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

def _mark_missing_records(records: dict[str, dict[str, Any]], gap_ids: set[str], now: datetime) -> None:
    for gap_id, record in records.items():
        if gap_id in gap_ids or not isinstance(record, dict):
            continue
        record["stale"] = True
        record["futility_status"] = "not_evaluated"
        record.setdefault("last_evaluated_ts", record.get("first_seen_ts"))
        record.setdefault("stale_at", _iso(now))


def futile_gap_ids(
    state_dir: Path,
    gaps: list[dict[str, Any]],
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Update current gaps and return the IDs currently suppressed."""
    try:
        from nanobot.runtime.state_access import evidence_status

        records = _load(state_dir)
        now = datetime.now(timezone.utc)
        if ledger_rows is not None:
            ledger_status = str(getattr(ledger_rows, "status", "complete") or "complete")
            if ledger_status == "unavailable":
                return set()
        archive_rows = None
        archive_status = "complete"
        if ledger_rows is not None:
            horizons = [
                _parse_ts(records.get(str(gap.get("id") or ""), {}).get("first_seen_ts"))
                for gap in gaps
            ]
            horizons = [value for value in horizons if value is not None]
            if horizons:
                # one horizon read for every gap; the counters filter per gap
                window = _window(state_dir, min(horizons))
                archive_rows, archive_status = list(window.rows), evidence_status(window)
        gap_ids = {str(gap.get("id") or "").strip() for gap in gaps}
        _mark_missing_records(records, gap_ids, now)
        result = {
            str(gap.get("id"))
            for gap in gaps
            if _update(
                state_dir, gap, records, now,
                ledger_rows=ledger_rows, archive_rows=archive_rows, archive_status=archive_status,
            )
        }
        _save(state_dir, records)
        return {item for item in result if item}
    except Exception:
        return set()

def futile_surfaces(state_dir: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    """Lever surfaces of the gaps currently suppressed (#1184): what demand and
    the proposer must stop aiming at. Records with an id-count unit have no
    surface and never appear here. Fail-open to ``[]``."""
    try:
        now = now or datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for record in _load(state_dir).values():
            if not isinstance(record, dict):
                continue
            until = _parse_ts(record.get("futile_until"))
            surface = record.get("surface")
            if record.get("futile") and until is not None and now < until and isinstance(surface, list) and surface:
                out.append({
                    key: record.get(key)
                    for key in (
                        "gap_id", "metric", "surface", "attempt_count",
                        "attempt_unit", "first_seen_ts", "metric_delta",
                    )
                })
        return out
    except Exception:
        return []

def futile_surface_for(state_dir: Path, path: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """The futile gap whose surface ``path`` hits, or ``None``."""
    return next((gap for gap in futile_surfaces(state_dir, now) if surface_hits(gap["surface"], [path])), None)

def futility_snapshot(state_dir: Path) -> dict[str, Any]:
    try:
        records = _load(state_dir)
        return {
            "futile_gap_ids": sorted(key for key, value in records.items() if value.get("futile")),
            "total_tracked": len(records),
            "stale_gap_ids": sorted(
                key for key, value in records.items() if isinstance(value, dict) and value.get("stale")
            ),
            "measured_gap_ids": sorted(
                key for key, value in records.items()
                if isinstance(value, dict) and value.get("futility_status") == "measured"
            ),
        }
    except Exception:
        return {
            "futile_gap_ids": [], "total_tracked": 0,
            "stale_gap_ids": [], "measured_gap_ids": [],
        }
