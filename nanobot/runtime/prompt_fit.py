"""Bounded prompt-fit telemetry shared by scorecard and dashboard readers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from nanobot.runtime import state_access

PROMPT_FIT_SCHEMA = "prompt-fit-v1"
PROMPT_FIT_WINDOW_ROWS = 25
PROMPT_FIT_WINDOW_DAYS = 7
PROMPT_FIT_MAX_BYTES = 4 * 2**20
PROMPT_FIT_SECTION_CAP = 20


def _metric(items: Any, *, field: str = "section") -> dict[str, Any]:
    """Project one row's drop/trim list without collapsing its state."""
    if items is None:
        return {"status": "missing", "count": None, "chars": None, "sections": None}
    if not isinstance(items, list):
        return {"status": "malformed", "count": None, "chars": None, "sections": None}
    if not all(isinstance(item, dict) for item in items):
        return {"status": "malformed", "count": None, "chars": None, "sections": None}

    chars: list[int] = []
    for item in items:
        value = item.get("chars")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return {"status": "malformed", "count": None, "chars": None, "sections": None}
        chars.append(int(value))
    sections = [str(item[field]) for item in items if item.get(field)]
    return {
        "status": "empty" if not items else "measured",
        "count": len(items),
        "chars": sum(chars),
        "sections": sections[:PROMPT_FIT_SECTION_CAP],
    }


def _source_status(ledger_path: Path, window: state_access.Window) -> str:
    """Keep absent source distinct from an unreadable/partial source."""
    try:
        if not ledger_path.parent.is_dir() or not ledger_path.is_file():
            return "missing"
    except PermissionError:
        return "permission"
    except OSError:
        return "unreadable"
    return window.status


def summarize_prompt_fit_rows(
    rows: Iterable[dict[str, Any]],
    *,
    source_status: str = "complete",
    source_notes: Iterable[str] = (),
    requested_from: str | None = None,
    covered_from: str | None = None,
    covered_to: str | None = None,
    window_rows: int = PROMPT_FIT_WINDOW_ROWS,
) -> dict[str, Any]:
    """Summarize the newest bounded ``phase=system_prompt`` rows.

    The scorecard passes its already-bounded ledger window here; the dashboard
    uses :func:`read_prompt_fit_summary`. Both consumers therefore share this
    aggregation and the same 25-system-prompt-row denominator by default.
    """
    prompt_rows = [
        row for row in rows
        if isinstance(row, dict) and row.get("phase") == "system_prompt"
    ]
    prompt_rows.sort(key=lambda row: str(row.get("ts") or row.get("timestamp") or ""))
    selected = prompt_rows[-max(0, window_rows):]
    has_malformed_row = any(
        (row.get("dropped") is not None and not isinstance(row.get("dropped"), list))
        or (row.get("trimmed") is not None and not isinstance(row.get("trimmed"), list))
        for row in selected
    )
    notes = list(source_notes)

    prompt_times = [
        str(row.get("ts") or row.get("timestamp"))
        for row in selected
        if row.get("ts") or row.get("timestamp")
    ]
    prompt_covered_from = prompt_times[0] if prompt_times else None
    prompt_covered_to = prompt_times[-1] if prompt_times else None

    if not selected:
        status = "valid-empty" if source_status == "complete" else source_status
        return {
            "schema_version": PROMPT_FIT_SCHEMA,
            "source_status": status,
            "reader_status": source_status,
            "latest": None,
            "rows_considered": 0,
            "rows_with_drops": 0,
            "rows_with_trims": 0,
            "window_rows": window_rows,
            "window_kind": "newest_system_prompt_rows",
            "window_days": PROMPT_FIT_WINDOW_DAYS,
            "requested_from": requested_from,
            "covered_from": covered_from,
            "covered_to": covered_to,
            "prompt_covered_from": prompt_covered_from,
            "prompt_covered_to": prompt_covered_to,
            "notes": notes,
        }

    latest = selected[-1]
    if has_malformed_row:
        source_status = "malformed"
    dropped = _metric(latest.get("dropped"))
    trimmed = _metric(latest.get("trimmed"))
    published_status = "valid" if source_status == "complete" else source_status
    return {
        "schema_version": PROMPT_FIT_SCHEMA,
        "source_status": published_status,
        "reader_status": source_status,
        "latest": {
            "chars": latest.get("chars"),
            "cap": latest.get("cap"),
            "overflow": bool(latest.get("overflow")),
            "over_by": latest.get("over_by"),
            "rung": latest.get("rung"),
            "sections": latest.get("sections") if isinstance(latest.get("sections"), dict) else {},
            "dropped": dropped,
            "trimmed": trimmed,
            # Compatibility fields consumed by the current runtime dashboard.
            # Preserve its historical rendering while the nested objects above
            # keep missing, measured-empty, and malformed distinct for new
            # consumers.
            "dropped_count": dropped["count"] if dropped["count"] is not None else 0,
            "dropped_chars": dropped["chars"] if dropped["chars"] is not None else 0,
            "dropped_sections": dropped["sections"] if dropped["sections"] is not None else [],
            "trimmed_count": trimmed["count"],
            "trimmed_chars": trimmed["chars"],
            "trimmed_sections": trimmed["sections"],
            "cycle_id": latest.get("cycle_id"),
            "ts": latest.get("ts") or latest.get("timestamp"),
        },
        "rows_considered": len(selected),
        "rows_with_drops": sum(
            1 for row in selected
            if isinstance(row.get("dropped"), list) and bool(row["dropped"])
        ),
        "rows_with_trims": sum(
            1 for row in selected
            if isinstance(row.get("trimmed"), list) and bool(row["trimmed"])
        ),
        "window_rows": window_rows,
        "window_kind": "newest_system_prompt_rows",
        "window_days": PROMPT_FIT_WINDOW_DAYS,
        "requested_from": requested_from,
        "covered_from": covered_from,
        "covered_to": covered_to,
        "prompt_covered_from": prompt_covered_from,
        "prompt_covered_to": prompt_covered_to,
        "notes": notes,
    }


def read_prompt_fit_summary(
    state_dir: str | Path,
    *,
    now: datetime | None = None,
    window_rows: int = PROMPT_FIT_WINDOW_ROWS,
) -> dict[str, Any]:
    """Read and summarize prompt-fit rows through the bounded state reader."""
    now = now or datetime.now(timezone.utc)
    requested_from = (now - timedelta(days=PROMPT_FIT_WINDOW_DAYS)).astimezone(timezone.utc)
    requested_text = requested_from.isoformat().replace("+00:00", "Z")
    ledger_path = Path(state_dir) / "ledger" / "cycles.jsonl"
    window = state_access.ledger_window(
        state_dir,
        since_ts=requested_text,
        phases=frozenset({"system_prompt"}),
        max_bytes=PROMPT_FIT_MAX_BYTES,
        now=now,
    )
    source_status = _source_status(ledger_path, window)
    if source_status == "complete" and not window.rows:
        try:
            has_content = False
            with ledger_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.strip():
                        has_content = True
                        import json

                        json.loads(line)
            if has_content:
                source_status = "malformed"
        except (OSError, UnicodeError):
            source_status = "unreadable"
        except (TypeError, ValueError, json.JSONDecodeError):
            source_status = "malformed"
    if source_status == "missing":
        rows_status = "missing"
    elif source_status in {"unreadable", "permission", "malformed"}:
        rows_status = source_status
    elif window.status == "unavailable":
        rows_status = "unavailable"
    else:
        rows_status = window.status
    return summarize_prompt_fit_rows(
        window.rows,
        source_status=rows_status,
        source_notes=window.notes,
        requested_from=window.requested_from,
        covered_from=window.covered_from,
        covered_to=window.covered_to,
        window_rows=window_rows,
    )
