"""Deterministic, bounded experiment-result ledger (#1426).

This sidecar is intentionally separate from the cycle ledger: experiment rows
survive cycle checkout/reset and are never inferred from cycle history.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COLUMNS = ("matrix_cell", "arm", "what_changed", "measured_delta", "verdict")
LEDGER_RELATIVE_PATH = Path("experiments") / "results.jsonl"
_MAX_ROW_BYTES = 32_768
_MAX_ROWS = 2_000


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / LEDGER_RELATIVE_PATH


def empty_ledger() -> dict[str, Any]:
    return {"status": "empty", "columns": list(COLUMNS), "rows": [], "truncated": False}


def read_experiment_ledger(state_dir: Path) -> dict[str, Any]:
    """Read rows with explicit missing/empty/present/unavailable states; never raises.

    ``truncated`` is a separate flag, not a status: hitting the row cap on an
    append-only ledger is expected with age and must not blank the rows that
    were read. A cap that turns its own artifact unreadable is a silent
    off-switch, not a bound (#1183, #1178, #1166).
    """
    path = ledger_path(state_dir)
    if not path.is_file():
        return {"status": "missing", "columns": list(COLUMNS), "rows": [], "truncated": False}
    expected = frozenset(COLUMNS)
    try:
        rows: list[dict[str, Any]] = []
        truncated = False
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                if len(line.encode("utf-8")) > _MAX_ROW_BYTES:
                    return {
                        "status": "unavailable", "columns": list(COLUMNS),
                        "rows": [], "truncated": False,
                    }
                item = json.loads(line)
                # Key ORDER is a serialization detail, not a schema violation:
                # compare the key set so a valid row written by another encoder
                # is not misreported as an unreadable ledger.
                if not isinstance(item, dict) or frozenset(item.keys()) != expected:
                    return {
                        "status": "unavailable", "columns": list(COLUMNS),
                        "rows": [], "truncated": False,
                    }
                if len(rows) >= _MAX_ROWS:
                    truncated = True
                    break
                rows.append(item)
        return {
            "status": "empty" if not rows else "present",
            "columns": list(COLUMNS),
            "rows": rows,
            "truncated": truncated,
        }
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return {"status": "unavailable", "columns": list(COLUMNS), "rows": [], "truncated": False}


def append_experiment_result(
    state_dir: Path,
    *,
    matrix_cell: str,
    arm: str,
    what_changed: str,
    measured_delta: str,
    verdict: str,
) -> bool:
    """Append exactly one deterministic row; fail-open for cycle callers."""
    row = {
        "matrix_cell": str(matrix_cell),
        "arm": str(arm),
        "what_changed": str(what_changed),
        "measured_delta": str(measured_delta),
        "verdict": str(verdict),
    }
    encoded = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_ROW_BYTES:
        return False
    try:
        path = ledger_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(encoded)
        return True
    except (OSError, UnicodeError):
        return False
