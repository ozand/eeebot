"""Promotions directory rotation and retention pruning (#1039).

Rotates historical promotion candidate JSON files in ``state/promotions/``:
- Active records are JSON files (e.g. ``promotion-runtime-cycle-*.json`` or custom candidate IDs).
- Files modified before the current calendar day (UTC) are compressed to
  ``state/promotions/archive/promotions-YYYY-MM-DD.jsonl.gz``.
- Archives older than ``PROMOTIONS_RETENTION_DAYS`` (default 90) are pruned.
- ``latest.json`` is preserved directly in ``state/promotions/`` and not archived.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:  # pragma: no cover - fcntl is POSIX-only; the host is always Linux
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

PROMOTIONS_RETENTION_DAYS_DEFAULT = 90
_ARCHIVE_NAME_PATTERN = re.compile(r"^promotions-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")


class _NullLock:
    """Sentinel when fcntl is unavailable."""

    def close(self) -> None:
        pass


def _acquire_promotions_lock(promotions_dir: Path):
    """Acquire exclusive flock on promotions_dir / '.rotation.lock'."""
    if fcntl is None:
        return _NullLock()
    try:
        lock_path = promotions_dir / ".rotation.lock"
        handle = open(lock_path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle
    except OSError:
        return _NullLock()


def _promotions_retention_days() -> int:
    raw = os.environ.get("EEEBOT_PROMOTIONS_RETENTION_DAYS", "")
    if raw.isdigit():
        val = int(raw)
        if val > 0:
            return val
    return PROMOTIONS_RETENTION_DAYS_DEFAULT


def rotate_promotions(
    promotions_dir: Path,
    *,
    today: str | None = None,
    retention_days: int | None = None,
) -> dict[str, int]:
    """Rotate prior-day promotion candidate files into dated gzip archives and prune old archives.

    Returns stats dict: {"archived_files": int, "pruned_archives": int}.
    """
    promotions_dir = Path(promotions_dir)
    if not promotions_dir.exists() or not promotions_dir.is_dir():
        return {"archived_files": 0, "pruned_archives": 0}

    now_utc = datetime.now(timezone.utc)
    if today is None:
        today = now_utc.strftime("%Y-%m-%d")
    if retention_days is None:
        retention_days = _promotions_retention_days()

    archive_dir = promotions_dir / "archive"
    archived_count = 0
    pruned_count = 0

    lock = _acquire_promotions_lock(promotions_dir)
    try:
        # 1. Group candidate files by prior-day modification date
        # Candidates are .json files directly under promotions_dir, except latest.json
        day_groups: dict[str, list[Path]] = {}
        try:
            for entry in promotions_dir.iterdir():
                if not entry.is_file():
                    continue
                if not entry.name.endswith(".json"):
                    continue
                if entry.name == "latest.json":
                    continue
                try:
                    mtime_utc = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
                    file_day = mtime_utc.strftime("%Y-%m-%d")
                except OSError:
                    continue

                if file_day < today:
                    day_groups.setdefault(file_day, []).append(entry)
        except OSError:
            pass

        # 2. Append/write to dated gzip archive and remove original files
        if day_groups:
            archive_dir.mkdir(parents=True, exist_ok=True)
            for day_str, file_paths in sorted(day_groups.items()):
                gz_path = archive_dir / f"promotions-{day_str}.jsonl.gz"
                records: list[dict] = []
                for fpath in sorted(file_paths):
                    try:
                        content = fpath.read_text(encoding="utf-8")
                        data = json.loads(content)
                        if isinstance(data, dict):
                            records.append(data)
                    except Exception:
                        # If corrupt/unreadable json, wrap raw text as fallback
                        try:
                            records.append({"raw_filename": fpath.name, "raw_content": fpath.read_text(encoding="utf-8", errors="replace")})
                        except Exception:
                            pass

                if records:
                    # Read existing records if archive already exists
                    existing_lines: list[bytes] = []
                    if gz_path.exists():
                        try:
                            with gzip.open(gz_path, "rb") as gz_in:
                                existing_lines = gz_in.readlines()
                        except Exception:
                            existing_lines = []

                    # Atomic write of combined archive
                    tmp_fd, tmp_path_str = tempfile.mkstemp(
                        prefix="promotions_gz_",
                        suffix=".tmp",
                        dir=archive_dir,
                    )
                    try:
                        with os.fdopen(tmp_fd, "wb") as raw_out:
                            with gzip.open(raw_out, "wb") as gz_out:
                                for eline in existing_lines:
                                    gz_out.write(eline)
                                for rec in records:
                                    line_bytes = (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8")
                                    gz_out.write(line_bytes)
                        os.replace(tmp_path_str, gz_path)
                    except Exception:
                        if os.path.exists(tmp_path_str):
                            try:
                                os.unlink(tmp_path_str)
                            except OSError:
                                pass
                        continue

                    for fpath in file_paths:
                        try:
                            fpath.unlink(missing_ok=True)
                            archived_count += 1
                        except OSError:
                            pass

        # 3. Prune old archives beyond retention_days
        if archive_dir.exists() and archive_dir.is_dir():
            try:
                today_dt = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                for entry in archive_dir.iterdir():
                    if not entry.is_file():
                        continue
                    match = _ARCHIVE_NAME_PATTERN.match(entry.name)
                    if not match:
                        continue
                    file_day_str = match.group(1)
                    try:
                        file_dt = datetime.strptime(file_day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        age_days = (today_dt - file_dt).days
                        if age_days > retention_days:
                            entry.unlink(missing_ok=True)
                            pruned_count += 1
                    except Exception:
                        continue
            except Exception:
                pass
    finally:
        try:
            lock.close()
        except Exception:
            pass

    return {"archived_files": archived_count, "pruned_archives": pruned_count}

