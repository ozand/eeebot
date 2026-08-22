"""Stale subagent request archiving — minimal closure kept for scripts/archive_subagent_requests.py.

Extracted from the now-deleted `subagent_materializer.py` (issue #916):
`archive_stale_requests` is the only piece of that module a still-live
operator script (`scripts/archive_subagent_requests.py`, wired to the
`eeebot-archive-subagent-requests` systemd timer) actually calls, together
with its two small private helpers (`_extract_request_id`, `_is_stale_
request`) and the id-matching patterns they use. No behavior change from the
move.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.runtime._io import utc_iso_raw as _utc_iso
from nanobot.runtime._io import utc_now as _utc_now

_REQUEST_ID_RE = re.compile(r"^(subagent-|request-)?([a-f0-9]{8,32})(?:-[\w-]+)?$")
_TASK_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _extract_request_id(path: Path) -> str | None:
    """Extract a normalized request ID from a file path."""
    stem = path.stem
    match = _REQUEST_ID_RE.match(stem)
    if match:
        return match.group(2)
    return stem if _TASK_ID_RE.match(stem) else None


def _is_stale_request(path: Path, cutoff_seconds: float = 86400, now: datetime | None = None) -> bool:
    """Check if a subagent request is stale (older than cutoff)."""
    current = _utc_now(now)
    try:
        mtime = path.stat().st_mtime
        age = (current.timestamp() - mtime)
        return age > cutoff_seconds
    except Exception:
        return False


def archive_stale_requests(
    workspace: Path,
    state_root: Path | None = None,
    cutoff_seconds: float = 86400,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Archive subagent requests older than cutoff_seconds.

    Args:
        workspace: The workspace root path.
        state_root: Optional state root override.
        cutoff_seconds: Age threshold in seconds (default 24h).
        now: Optional timestamp override for testing.

    Returns:
        Summary of archived requests.
    """
    current = _utc_now(now)
    root = state_root if state_root is not None else workspace / "state"
    subagents_dir = root / "subagents"
    archive_dir = subagents_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    pending_dir = subagents_dir / "pending"
    requests_dir = subagents_dir / "requests"

    archived: list[str] = []

    for directory in [pending_dir, requests_dir]:
        if not directory.exists():
            continue

        for path in directory.glob("*.json"):
            if _is_stale_request(path, cutoff_seconds=cutoff_seconds, now=current):
                request_id = _extract_request_id(path)
                if request_id:
                    archive_path = archive_dir / path.name
                    try:
                        path.rename(archive_path)
                        archived.append(request_id)
                    except Exception:
                        pass

    summary = {
        "schema_version": "subagent-archive-summary-v1",
        "archived_count": len(archived),
        "archived_ids": archived,
        "cutoff_seconds": cutoff_seconds,
        "archived_at_utc": _utc_iso(current),
    }

    # Write summary
    archive_latest_path = subagents_dir / "archive_latest.json"
    archive_latest_path.parent.mkdir(parents=True, exist_ok=True)
    archive_latest_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    return summary
