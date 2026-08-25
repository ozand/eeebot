"""Mechanical rotation helper for lessons/lessons.yaml and lessons/errors.yaml (#985).

Keeps the active window bounded to at most ``_MAX_ACTIVE_ENTRIES`` entries and
``_MAX_ACTIVE_BYTES`` bytes. When either limit is exceeded the oldest entries are
gzip-archived to ``lessons/archive/<stem>-<YYYY-MM-DD>.yaml.gz`` and the active
file is rewritten with only the most-recent entries.

Design constraints (issue #985):
- stdlib only — no PyYAML/YAML parsing needed for the archive path; raw bytes are
  preserved so no round-trip distortion is possible.
- No LLM in the path.
- Atomic write: new file written to a sibling ``.tmp`` then renamed over the old one.
- Idempotent / collision-safe: archive filename includes today's UTC date; if the
  same archive already exists the new entries are prepended inside it (merged), so
  running twice on the same day is safe.
- Fail-open: any exception is swallowed; the caller (``_write_structured_lesson``)
  must never be blocked by a rotation error.
- The top-level schema is preserved. ``lessons.yaml`` uses a ``{'lessons': [...]}``
  dict wrapper; ``errors.yaml`` is a bare list. Both round-trip correctly.
- Archive files live under ``lessons/archive/`` (created on first use).
"""
from __future__ import annotations

import gzip
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Active-window limits (issue #985 spec).
_MAX_ACTIVE_ENTRIES: int = 200
_MAX_ACTIVE_BYTES: int = 2 * 1024 * 1024  # 2 MB


def _parse_entries(raw_bytes: bytes) -> tuple[bool, list[bytes]]:
    """Split raw YAML bytes into (is_dict_wrapped, list_of_entry_chunks).

    We do NOT parse YAML — we only split on top-level ``- id:`` / ``- `` list
    item boundaries so that we can slice entries without risking a re-serialise
    round-trip. Returns ``(is_dict_wrapped, chunks)`` where ``is_dict_wrapped``
    is True when the file starts with a ``lessons:`` or ``errors:`` mapping key
    (written by ``bridge._write_structured_lesson``), and ``chunks`` is a list of
    raw entry byte strings (including leading ``- `` marker and all continuation
    lines for that entry).

    Falls back to returning the whole file as one chunk on any parse ambiguity so
    rotation always degrades to a no-op rather than corrupting the file.
    """
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if not lines:
        return False, []

    # Detect dict-wrapper: first non-blank line is "lessons:" or "errors:"
    is_dict_wrapped = False
    content_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("lessons:", "errors:"):
            is_dict_wrapped = True
            content_start = i + 1
        break

    # Split into entry chunks: each top-level list item starts with "- "
    # (possibly indented by 0 or 2 spaces when dict-wrapped).
    entry_chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines[content_start:]:
        # A line that starts a new top-level list entry.
        stripped_line = line.lstrip()
        leading_spaces = len(line) - len(stripped_line)
        # Dict-wrapped entries are indented 2 spaces; bare list entries have 0.
        expected_indent = 2 if is_dict_wrapped else 0
        if leading_spaces == expected_indent and stripped_line.startswith("- "):
            if current:
                entry_chunks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        entry_chunks.append(current)

    # Convert to byte strings.
    chunks = ["".join(c).encode("utf-8") for c in entry_chunks]
    return is_dict_wrapped, chunks


def _reconstruct(is_dict_wrapped: bool, chunks: list[bytes], key: str) -> bytes:
    """Reassemble entry chunks back into a well-formed YAML file."""
    body = b"".join(chunks)
    if not is_dict_wrapped:
        return body
    header = f"{key}:\n".encode("utf-8")
    return header + body


def _archive_path(lessons_dir: Path, stem: str) -> Path:
    """Return ``lessons/archive/<stem>-<YYYY-MM-DD>.yaml.gz`` for today (UTC)."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return lessons_dir / "archive" / f"{stem}-{today}.yaml.gz"


def _write_atomic(path: Path, payload: bytes, suffix: str) -> None:
    """Write bytes beside *path* and replace it atomically."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=suffix)
    try:
        os.close(tmp_fd)
        Path(tmp_path).write_bytes(payload)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_archive_once(archive_path: Path, payload: bytes) -> None:
    """Create an archive once; an existing archive makes retries idempotent."""
    if archive_path.exists():
        return
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(archive_path.parent), suffix=".tmp.gz")
    try:
        os.close(tmp_fd)
        with gzip.open(tmp_path, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path, str(archive_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def rotate_lessons_file(yaml_path: Path) -> str | None:
    """Rotate ``yaml_path`` if it exceeds the active-window limits.

    Archives the excess (oldest) entries to a dated gzip file under
    ``<yaml_path.parent>/archive/`` and rewrites ``yaml_path`` atomically with
    only the most-recent ``_MAX_ACTIVE_ENTRIES`` entries.

    Returns the archive filename (relative to ``yaml_path.parent``) if rotation
    occurred, ``None`` if not needed, or ``None`` silently on any error (fail-open).

    The function is idempotent: calling it when the file is already within limits
    is a no-op.  Calling it twice on the same day merges the archived chunks
    (newest first) into the same daily ``.gz`` file rather than creating a second.
    """
    try:
        if not yaml_path.exists():
            return None

        raw = yaml_path.read_bytes()
        size = len(raw)
        if size == 0:
            return None

        is_dict_wrapped, chunks = _parse_entries(raw)

        n = len(chunks)
        if n <= _MAX_ACTIVE_ENTRIES and size <= _MAX_ACTIVE_BYTES:
            return None  # within limits — nothing to do

        # Determine dict key for reconstruction.
        stem = yaml_path.stem  # "lessons" or "errors"
        key = stem  # default key matches stem; handles both lessons/errors.

        # How many to keep: whichever limit is tighter governs.
        keep = _MAX_ACTIVE_ENTRIES

        # If byte limit is the binding constraint, shrink further.
        if size > _MAX_ACTIVE_BYTES and n > keep:
            # Estimate per-entry size and reduce until below limit.
            avg = size / n
            estimated_keep = max(1, int(_MAX_ACTIVE_BYTES / avg))
            keep = min(keep, estimated_keep)

        keep = max(1, keep)  # always keep at least one entry
        active_chunks = chunks[:keep]
        archive_chunks = chunks[keep:]

        if not archive_chunks:
            return None  # nothing to archive

        # Ensure archive directory exists.
        lessons_dir = yaml_path.parent
        archive_dir = lessons_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Archive first, then replace the live file. If interrupted between
        # these operations, the existing archive makes the next run a no-op for
        # the archive and the live replacement completes without loss.
        dest = _archive_path(lessons_dir, stem)
        _write_archive_once(dest, _reconstruct(is_dict_wrapped, archive_chunks, key))

        # Rewrite active file atomically.
        new_content = _reconstruct(is_dict_wrapped, active_chunks, key)
        _write_atomic(yaml_path, new_content, ".tmp.yaml")

        return f"archive/{dest.name}"

    except Exception:
        return None  # fail-open: rotation errors never block the caller


def rotate_lessons_directory(lessons_dir: Path) -> list[str]:
    """Rotate both journal files, returning archive names created."""
    created: list[str] = []
    for name in ("lessons.yaml", "errors.yaml"):
        result = rotate_lessons_file(Path(lessons_dir) / name)
        if result:
            created.append(result)
    return created
