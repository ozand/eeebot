"""Shared JSON/time IO helpers for nanobot.runtime modules.

Consolidates near-identical helper functions that had drifted into separate
copies across coordinator.py, autoevolve.py, promotion.py, local_ci.py, and
subagent_materializer.py (see issue #600). The read/format helpers are NOT
fully interchangeable — each historical copy had slightly different error
handling or normalization semantics — so this module keeps the distinct
variants under distinct names rather than silently unifying behavior.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now(now: datetime | None = None) -> datetime:
    """Normalize *now* to an aware UTC datetime; defaults to the current time.

    Replaces the identical ``_utc_now`` previously duplicated in
    coordinator.py and subagent_materializer.py.
    """
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def utc_iso(now: datetime | None = None) -> str:
    """Format *now* (default: current time) as a 'Z'-suffixed UTC ISO-8601 string.

    Normalizes via utc_now() first. Replaces the equivalent (modulo
    parameter defaulting) former ``_utc_iso`` in coordinator.py and
    promotion.py, both of which normalized naive/aware datetimes to UTC
    before formatting.
    """
    return utc_now(now).isoformat().replace("+00:00", "Z")


def utc_iso_raw(value: datetime) -> str:
    """Format *value* as ISO-8601 with 'Z' substituted for '+00:00'.

    Unlike utc_iso(), this does NOT normalize a naive datetime to UTC
    first — it formats exactly what it is given. Replaces
    subagent_materializer.py's former ``_utc_iso``, whose callers always
    pass an already-normalized (utc_now()-derived) datetime; preserved
    verbatim rather than folded into utc_iso() to avoid changing behavior
    for any caller that might pass a naive value.
    """
    return value.isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* as indented JSON to *path*, creating parent dirs.

    Identical implementation previously duplicated in autoevolve.py,
    promotion.py, and local_ci.py.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_json_atomic(path: Path, payload: Any, *, indent: int = 2) -> None:
    """Write *payload* as indented JSON to *path* atomically using tmp + os.replace.

    Creates parent directories if needed. Writes to a sibling temporary file
    and replaces *path* via ``os.replace`` so readers never see partial content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, indent=indent, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def read_json_strict(path: Path) -> dict[str, Any]:
    """Read and parse *path* as JSON, raising on missing/invalid input.

    Replaces promotion.py's former ``_read_json`` (no error handling —
    callers there rely on the exception propagating).
    """
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_safe(path: Path) -> dict[str, Any] | None:
    """Read and parse *path* as JSON, returning None on any error.

    Replaces coordinator.py's former ``_safe_read_json``.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_json_dict(path: Path) -> dict[str, Any] | None:
    """Read *path* as JSON, returning None if missing, invalid, or not a dict.

    Replaces autoevolve.py's former ``_load_json`` (explicit ``exists()``
    check plus a dict-type guard on the parsed result).
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None
