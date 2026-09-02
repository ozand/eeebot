"""Bounded, fail-open access to rotated runtime state (#1174).

The reader returns status instead of collapsing unavailable input into empty data.
It is deliberately stdlib-only and has no provider/LLM dependency.
"""
from __future__ import annotations

import gzip
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_LEDGER_BYTES = 4 * 2**20  # 4 MiB bounds uncompressed host parsing.
_DEFAULT_ARTIFACT_FILES = 256  # bounded queue scan for the 2 GB host.
_DEFAULT_RETENTION_DAYS = 90  # matches cycle_ledger's rotation retention.
_DAY_RE = re.compile(r"^cycles-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")


@dataclass(frozen=True)
class Window:
    rows: tuple[dict, ...]
    status: str
    requested_from: str
    covered_from: str | None
    covered_to: str | None
    files_read: int
    files_skipped: int
    bytes_read: int
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Latest:
    path: Path | None
    age_s: float | None
    stale: bool
    status: str


@dataclass(frozen=True)
class Sidecar:
    data: Any
    status: str


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _phase_hint(line: str, phases: frozenset[str] | None) -> bool:
    if not phases:
        return True
    return any(f'"phase": "{phase}"' in line for phase in phases)


def _row_ts(row: dict[str, Any]) -> datetime | None:
    return _parse_ts(row.get("ts") or row.get("timestamp"))


def _ledger_sources(ledger_dir: Path, since: datetime) -> tuple[list[Path], list[str]]:
    notes: list[str] = []
    try:
        entries = list(ledger_dir.iterdir())
    except PermissionError:
        return [], ["permission"]
    except OSError:
        return [], ["io_error"]
    archives: list[tuple[datetime, Path]] = []
    for path in entries:
        if not path.name.startswith("cycles-") or not path.name.endswith(".jsonl.gz"):
            continue
        match = _DAY_RE.match(path.name)
        if not match:
            notes.append(f"invalid_archive:{path.name}")
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            notes.append(f"invalid_archive:{path.name}")
            continue
        archives.append((day, path))
    archives.sort(key=lambda item: item[0], reverse=True)
    return [ledger_dir / "cycles.jsonl"] + [path for day, path in archives if day + timedelta(days=1) >= since], notes


def _read_ledger_file(
    path: Path,
    *,
    since: datetime,
    phases: frozenset[str] | None,
    remaining: int,
) -> tuple[list[dict], int, str | None, str | None, bool, str | None]:
    """Read one source; return rows, bytes, coverage bounds, capped, error."""
    rows: list[dict] = []
    used = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    capped = False
    try:
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line_bytes = len(line.encode("utf-8", errors="replace"))
                if used + line_bytes > remaining:
                    capped = True
                    break
                used += line_bytes
                if not _phase_hint(line, phases):
                    continue
                try:
                    row = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                ts = _row_ts(row)
                if ts is not None:
                    earliest = ts if earliest is None or ts < earliest else earliest
                    latest = ts if latest is None or ts > latest else latest
                if ts is not None and ts < since:
                    continue
                if phases and row.get("phase") not in phases:
                    continue
                rows.append(row)
        return rows, used, _iso(earliest) if earliest else None, _iso(latest) if latest else None, capped, None
    except PermissionError:
        return [], used, None, None, False, "permission"
    except (OSError, EOFError, gzip.BadGzipFile):
        return [], used, None, None, False, f"gz_corrupt:{path.name}" if path.name.endswith(".gz") else "io_error"


def ledger_window(
    state_dir: str | Path,
    *,
    since_ts: str,
    phases: frozenset[str] | None = None,
    max_bytes: int = _DEFAULT_LEDGER_BYTES,
) -> Window:
    """Read active and dated ledger archives newest-first, never raising."""
    requested = _parse_ts(since_ts)
    if requested is None:
        return Window((), "unavailable", since_ts, None, None, 0, 0, 0, ("invalid_since",))
    ledger_dir = Path(state_dir) / "ledger"
    try:
        if not ledger_dir.is_dir():
            return Window((), "unavailable", _iso(requested), None, None, 0, 0, 0, ("dir_missing",))
        sources, notes = _ledger_sources(ledger_dir, requested)
    except PermissionError:
        return Window((), "unavailable", _iso(requested), None, None, 0, 0, 0, ("permission",))
    except OSError:
        return Window((), "unavailable", _iso(requested), None, None, 0, 0, 0, ("io_error",))
    rows: list[dict] = []
    bytes_read = 0
    files_read = 0
    files_skipped = 0
    capped = False
    covered: list[str] = []
    covered_to_values: list[str] = []
    for path in sources:
        if not path.is_file():
            if path.name == "cycles.jsonl":
                notes.append("dir_missing")
            continue
        if bytes_read >= max(0, max_bytes):
            capped = True
            break
        file_rows, used, file_covered, file_covered_to, file_capped, error = _read_ledger_file(
            path, since=requested, phases=phases, remaining=max_bytes - bytes_read
        )
        bytes_read += used
        if error:
            files_skipped += 1
            notes.append(error)
            if error == "permission" and files_read == 0:
                return Window((), "unavailable", _iso(requested), None, None, 0, files_skipped, bytes_read, tuple(notes))
            continue
        files_read += 1
        rows.extend(file_rows)
        if file_covered:
            covered.append(file_covered)
        if file_covered_to:
            covered_to_values.append(file_covered_to)
        if file_capped:
            capped = True
            notes.append("cap_bytes")
            break
        if file_covered and _parse_ts(file_covered) is not None and _parse_ts(file_covered) < requested:
            break
    retention_days = int(os.environ.get("SELFEVO_LEDGER_RETENTION_DAYS", str(_DEFAULT_RETENTION_DAYS)))
    if requested < datetime.now(timezone.utc) - timedelta(days=max(1, retention_days)):
        notes.append("beyond_retention")
    rows.sort(key=lambda row: _row_ts(row) or datetime.min.replace(tzinfo=timezone.utc))
    status = "partial" if capped or files_skipped or "beyond_retention" in notes else "complete"
    covered_from = _iso(requested) if status == "complete" else (min(covered) if covered else None)
    covered_to = max(covered_to_values) if covered_to_values else None
    return Window(tuple(rows), status, _iso(requested), covered_from, covered_to, files_read, files_skipped, bytes_read, tuple(dict.fromkeys(notes)))


def evidence_status(window: Window) -> str:
    """Status as a Class-A consumer must read it (#1173 D-2, #1175).

    ``unavailable`` because the ledger directory does not exist yet is a
    state with no history, not a failed read, and maps to ``complete`` (the
    window is genuinely empty). A ``partial`` read in which no source was
    read at all (every file skipped) carries no evidence and maps to
    ``unavailable``. Everything else is returned as reported. Callers then
    apply: complete → count; partial → lower bound, never lower a persisted
    counter; unavailable → keep the previous persisted verdict.
    """
    if window.status == "unavailable" and tuple(window.notes) == ("dir_missing",):
        return "complete"
    if window.status == "partial" and window.files_read == 0:
        return "unavailable"
    return window.status


def _artifact_dirs(state_dir: Path) -> Iterable[Path]:
    root = state_dir / "subagents"
    yield root / "results"
    yield root / "requests"
    yield root / "archive"


def artifacts(
    state_dir: str | Path,
    *,
    newest: int,
    max_age_hours: float | None = None,
    statuses: frozenset[str] | None = None,
) -> Window:
    """Return newest bounded JSON artifacts across live and archived flat dirs."""
    root = Path(state_dir) / "subagents"
    requested = _iso(datetime.now(timezone.utc) - timedelta(hours=max_age_hours)) if max_age_hours is not None else _iso(datetime.min.replace(tzinfo=timezone.utc))
    try:
        if not root.is_dir():
            return Window((), "unavailable", requested, None, None, 0, 0, 0, ("dir_missing",))
        paths: list[Path] = []
        for directory in _artifact_dirs(Path(state_dir)):
            if not directory.is_dir():
                continue
            paths.extend(p for p in directory.iterdir() if p.is_file() and p.suffix == ".json")
        paths = sorted(paths, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)[:_DEFAULT_ARTIFACT_FILES]
    except PermissionError:
        return Window((), "unavailable", requested, None, None, 0, 0, 0, ("permission",))
    except OSError:
        return Window((), "unavailable", requested, None, None, 0, 0, 0, ("io_error",))
    now = datetime.now(timezone.utc)
    selected: list[dict] = []
    skipped = 0
    for path in paths:
        try:
            age = now.timestamp() - path.stat().st_mtime
            if max_age_hours is not None and age > max_age_hours * 3600:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                skipped += 1
                continue
            status = str(data.get("status") or data.get("result_status") or "")
            if statuses and status not in statuses:
                continue
            selected.append(data)
            if len(selected) >= max(0, newest):
                break
        except PermissionError:
            skipped += 1
        except (OSError, ValueError, json.JSONDecodeError):
            skipped += 1
    return Window(tuple(selected), "partial" if skipped else "complete", requested, None, None, len(paths), skipped, 0, ())


def latest_file(directory: str | Path, pattern: str, *, max_age_s: float) -> Latest:
    """Return deterministic latest file; mtime ties are broken by name."""
    directory = Path(directory)
    try:
        if not directory.is_dir():
            return Latest(None, None, True, "dir_missing")
        candidates = [p for p in directory.iterdir() if p.is_file() and fnmatch(p.name, pattern)]
        if not candidates:
            return Latest(None, None, True, "empty")
        path = max(candidates, key=lambda p: (p.stat().st_mtime, p.name))
        age = max(0.0, datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)
        return Latest(path, age, age > max_age_s, "present")
    except PermissionError:
        return Latest(None, None, True, "permission")
    except OSError:
        return Latest(None, None, True, "io_error")


def sidecar(path: str | Path, *, default: Any, max_bytes: int) -> Sidecar:
    """Read one JSON sidecar and preserve absent/corrupt/oversize/permission."""
    path = Path(path)
    try:
        if not path.exists():
            return Sidecar(default, "absent")
        if not path.is_file():
            return Sidecar(default, "permission")
        if path.stat().st_size > max_bytes:
            return Sidecar(default, "oversize")
        return Sidecar(json.loads(path.read_text(encoding="utf-8")), "present")
    except PermissionError:
        return Sidecar(default, "permission")
    except (OSError, ValueError, json.JSONDecodeError):
        return Sidecar(default, "corrupt")
