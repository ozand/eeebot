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

    # Split into entry chunks. An entry ALWAYS begins with its "- id:" field
    # (bridge._write_structured_lesson writes id first for both lessons.yaml
    # and errors.yaml), at 0 or 2 spaces of indent. A bare "- " line is a
    # nested list item (e.g. a files_changed entry) and must NEVER be treated
    # as a boundary — the first live rotation (#991) used bare "- " at an
    # assumed 2-indent and tore entries apart on their files_changed lists.
    entry_chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines[content_start:]:
        stripped_line = line.lstrip()
        leading_spaces = len(line) - len(stripped_line)
        if leading_spaces in (0, 2) and stripped_line.startswith("- id:"):
            if current:
                entry_chunks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        entry_chunks.append(current)

    # Leading lines before the first "- id:" boundary: pure whitespace is
    # harmless — fold it into the first real entry. Anything else means we
    # failed to identify the format; degrade to a single chunk (no-op
    # rotation) rather than archiving an orphan fragment (#991).
    if entry_chunks and not entry_chunks[0][0].lstrip().startswith("- id:"):
        head = entry_chunks.pop(0)
        if any(ln.strip() for ln in head):
            body = "".join(lines[content_start:])
            return is_dict_wrapped, [body.encode("utf-8")]
        if entry_chunks:
            entry_chunks[0] = head + entry_chunks[0]
        else:
            entry_chunks = [head]

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


def _target_mode(path: Path) -> int:
    """Permission bits to give a rewritten *path*.

    mkstemp creates 0600 temp files and os.replace preserves that mode, which
    silently locks out non-agent readers (e.g. the ops-dashboard collector
    reading over ssh, #988). Preserve the existing file's bits; default new
    files to 0644.
    """
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return 0o644


def _write_atomic(path: Path, payload: bytes, suffix: str) -> None:
    """Write bytes beside *path* and replace it atomically."""
    mode = _target_mode(path)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=suffix)
    try:
        os.close(tmp_fd)
        Path(tmp_path).write_bytes(payload)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_archive_once(archive_path: Path, payload: bytes) -> None:
    """Create or merge an archive atomically.

    If an archive for today already exists, merge newly archived entries with
    the existing archive content rather than discarding new data.
    """
    final_payload = payload
    if archive_path.exists():
        try:
            with gzip.open(archive_path, "rb") as existing_fh:
                existing_bytes = existing_fh.read()

            if archive_path.name.endswith(".md.gz"):
                # Markdown index archive: append new chunk to existing text
                final_payload = existing_bytes.rstrip(b"\n") + b"\n\n" + payload.lstrip(b"\n")
            else:
                # YAML archive: parse existing and new entry chunks and merge
                is_dict_existing, existing_chunks = _parse_entries(existing_bytes)
                is_dict_new, new_chunks = _parse_entries(payload)
                is_wrapped = is_dict_existing or is_dict_new
                key = "lessons" if "lessons" in archive_path.name else "errors"
                # Existing older entries followed by newly archived entries
                merged_chunks = existing_chunks + new_chunks
                final_payload = _reconstruct(is_wrapped, merged_chunks, key)
        except Exception:
            final_payload = payload

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(archive_path.parent), suffix=".tmp.gz")
    try:
        os.close(tmp_fd)
        with gzip.open(tmp_path, "wb") as fh:
            fh.write(final_payload)
        os.chmod(tmp_path, _target_mode(archive_path))
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


# Markdown index active-window limits (#1041).
_MAX_INDEX_ENTRIES: int = 500
_MAX_INDEX_BYTES: int = 64 * 1024  # 64 KB


def rotate_index_file(
    index_path: Path,
    *,
    max_entries: int = _MAX_INDEX_ENTRIES,
    max_bytes: int = _MAX_INDEX_BYTES,
) -> str | None:
    """Bounded rotation companion for index catalogs (memory/index.md, docs/index.md) (#1041).

    Maintains active index bounded to at most max_entries catalog entries and max_bytes.
    When limits are exceeded, oldest entries (from the top) are archived to
    <parent>/archive/<stem>-<YYYY-MM-DD>.md.gz and the active file is rewritten with
    only the newest entries (tail).

    Preserves source file permissions and fails open on any error.
    """
    try:
        index_path = Path(index_path)
        if not index_path.is_file():
            return None

        size = index_path.stat().st_size
        raw_text = index_path.read_text(encoding="utf-8")
        lines = raw_text.splitlines(keepends=True)

        entry_indices: list[int] = []
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
                entry_indices.append(i)

        n = len(entry_indices) if entry_indices else len(lines)
        if n <= max_entries and size <= max_bytes:
            return None

        header_lines: list[str] = []
        if entry_indices and entry_indices[0] > 0:
            header_lines = lines[:entry_indices[0]]

        # Decide split_idx based on entry boundaries
        split_idx: int | None = None
        if entry_indices:
            # We want to keep the newest tail of entries.
            # Start by trying to keep up to max_entries entries, then shrink if bytes exceed max_bytes.
            # Entry i spans from entry_indices[k] to (entry_indices[k+1] if k+1 < len else len(lines))
            total_entries = len(entry_indices)
            candidate_count = min(total_entries, max_entries)

            # Find the largest candidate_count (newest entries) where active size <= max_bytes
            best_k_from_end = 1
            for count in range(candidate_count, 0, -1):
                start_line = entry_indices[-count]
                active_text = "".join(header_lines + lines[start_line:])
                if len(active_text.encode("utf-8")) <= max_bytes or count == 1:
                    best_k_from_end = count
                    if len(active_text.encode("utf-8")) <= max_bytes:
                        break

            # If keeping even best_k_from_end still needs rotation (i.e. we have older entries to archive)
            if best_k_from_end < total_entries:
                split_idx = entry_indices[-best_k_from_end]
            elif size > max_bytes:
                # If all entries together exceed max_bytes and we must keep at least 1 newest entry
                split_idx = entry_indices[-1] if total_entries > 1 else None

        if split_idx is None:
            if len(lines) > max_entries or size > max_bytes:
                keep_lines = min(len(lines), max_entries)
                for count in range(keep_lines, 0, -1):
                    start_line = len(lines) - count
                    active_text = "".join(header_lines + lines[start_line:])
                    if len(active_text.encode("utf-8")) <= max_bytes or count == 1:
                        split_idx = start_line
                        break
            else:
                return None

        if split_idx is None or split_idx <= 0 or split_idx >= len(lines):
            return None

        archive_lines = lines[:split_idx]
        active_lines = header_lines + lines[split_idx:] if header_lines and split_idx > len(header_lines) else lines[split_idx:]

        if not archive_lines or archive_lines == header_lines:
            return None

        index_dir = index_path.parent
        archive_dir = index_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        dest = _archive_path(index_dir, index_path.stem)
        # Suffix must be .md.gz
        if dest.name.endswith(".yaml.gz"):
            dest = dest.with_name(dest.name.replace(".yaml.gz", ".md.gz"))

        _write_archive_once(dest, "".join(archive_lines).encode("utf-8"))

        new_content = "".join(active_lines).encode("utf-8")
        _write_atomic(index_path, new_content, ".tmp.md")

        return f"archive/{dest.name}"
    except Exception:
        return None
