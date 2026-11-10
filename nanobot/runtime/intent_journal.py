"""Strict, bounded per-owner evidence for application-controlled attempts.

This module owns only one-attempt-per-owner journal admission and terminal
receipts. Because it has no parent-consumption/closure evidence API yet, an
owner with any prior intent remains held. Callers own stable parent identity
and retry/cost/cancellation budgets; no provider or tool is invoked here.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows development host; production contract requires flock.
    fcntl = None  # type: ignore[assignment]
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 4096
MAX_OWNER_RECORDS = 256
MAX_OWNER_BYTES = 1_048_576
_COMPONENTS = frozenset({"agent", "subagent", "memory", "heartbeat", "evaluator", "proposer", "bridge"})
_TARGETS = frozenset({
    "provider.chat", "provider.embedding", "tool.dispatch", "tool.read_file",
    "tool.write_file", "subagent.spawn", "memory.write", "heartbeat.run",
    "evaluator.run", "proposer.run", "bridge.cycle",
})


class JournalUnavailableError(RuntimeError):
    """The owner cannot safely admit or settle an attempt."""


class JournalCorruptError(JournalUnavailableError):
    """The owner journal is malformed or inconsistent; preserve it unchanged."""


def _uuid(value: str, field: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    canonical = str(parsed)
    if canonical != value:
        raise ValueError(f"{field} must be a canonical lowercase UUID")
    return canonical


def _owner_paths(state_root: Path, owner_id: str) -> tuple[Path, Path]:
    owner = _uuid(owner_id, "owner_id")
    root = Path(state_root)
    if not root.is_dir():
        raise JournalUnavailableError("state root must already exist")
    directory = root / "intent-journal"
    created = False
    try:
        try:
            directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise OSError("journal path is not a real directory")
        if os.name != "nt":
            os.chmod(directory, 0o700)
            if created:
                _fsync_directory(root)
                _fsync_directory(directory)
    except OSError as exc:
        raise JournalUnavailableError("cannot prepare journal directory durably") from exc
    return directory / f"{owner}.jsonl", directory / f"{owner}.lock"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        raise OSError("directory fsync is unsupported on this platform")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _owner_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise JournalUnavailableError("exclusive flock is unsupported")
    fd: int | None = None
    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("owner lock is not a regular file")
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        lock_file = os.fdopen(fd, "a+b", closefd=True)
        fd = None  # ownership transferred to lock_file
        with lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                # The lock file and its directory entry must survive restart;
                # keep this inode/path permanently (never unlink on cleanup).
                os.fsync(lock_file.fileno())
                _fsync_directory(path.parent)
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except JournalUnavailableError:
        raise
    except (OSError, AttributeError) as exc:
        raise JournalUnavailableError("cannot acquire or sync supported owner lock") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _ensure_journal(path: Path) -> None:
    fd: int | None = None
    created = False
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("owner journal is not a regular file")
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        if created:
            os.fsync(fd)
    except OSError as exc:
        raise JournalUnavailableError("cannot verify or sync owner journal") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if created:
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            raise JournalUnavailableError("cannot durably publish owner journal") from exc


def _decode(path: Path, owner_id: str) -> list[dict[str, Any]]:
    fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("owner journal is not a regular file")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = None
            raw = stream.read(MAX_OWNER_BYTES + 1)
    except OSError as exc:
        raise JournalUnavailableError("cannot read owner journal") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if len(raw) > MAX_OWNER_BYTES:
        raise JournalCorruptError("owner journal exceeds byte limit")
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise JournalCorruptError("owner journal has a truncated final record")
    records: list[dict[str, Any]] = []
    states: dict[tuple[str, str], bool] = {}
    for line in raw.splitlines():
        if not line or len(line) > MAX_RECORD_BYTES:
            raise JournalCorruptError("owner journal has an empty or oversized record")
        try:
            row = json.loads(line)
            _validate_record(row, owner_id)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise JournalCorruptError("owner journal contains an invalid record") from exc
        if len(records) >= MAX_OWNER_RECORDS:
            raise JournalCorruptError("owner journal exceeds record limit")
        key = (row["operation_id"], row["attempt_id"])
        if row["event"] == "intent":
            if key in states:
                raise JournalCorruptError("duplicate attempt identity")
            states[key] = False
        else:
            if key not in states or states[key]:
                raise JournalCorruptError("terminal has no unique unsettled intent")
            states[key] = True
        records.append(row)
    return records


def _validate_record(row: Any, owner_id: str) -> None:
    common = {"version", "owner_id", "operation_id", "attempt_id", "event", "component", "target", "ts"}
    if not isinstance(row, dict) or set(row) != common:
        raise ValueError("record schema mismatch")
    if (type(row["version"]) is not int or row["version"] != SCHEMA_VERSION
            or row["owner_id"] != owner_id):
        raise ValueError("record version or owner mismatch")
    _uuid(row["operation_id"], "operation_id")
    _uuid(row["attempt_id"], "attempt_id")
    if row["event"] not in {"intent", "terminal"}:
        raise ValueError("invalid event")
    if row["component"] not in _COMPONENTS:
        raise ValueError("component is not allowlisted")
    if row["target"] not in _TARGETS:
        raise ValueError("target is not allowlisted")
    if not isinstance(row["ts"], str) or len(row["ts"]) > 40:
        raise ValueError("invalid timestamp")
    from datetime import datetime
    try:
        parsed_ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed_ts.tzinfo is None:
        raise ValueError("timestamp must include a timezone")


def _append(path: Path, row: dict[str, Any], records: list[dict[str, Any]]) -> None:
    encoded = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_RECORD_BYTES:
        raise JournalUnavailableError("journal record exceeds byte limit")
    reserved_terminal_records = 1 if row["event"] == "intent" else 0
    reserved_terminal_bytes = MAX_RECORD_BYTES if row["event"] == "intent" else 0
    try:
        original = path.read_bytes()
    except OSError as exc:
        raise JournalUnavailableError("cannot read owner journal before append") from exc
    if (len(records) + 1 + reserved_terminal_records > MAX_OWNER_RECORDS
            or len(original) + len(encoded) + reserved_terminal_bytes > MAX_OWNER_BYTES):
        raise JournalUnavailableError("owner journal capacity exhausted")
    # Stage one complete replacement at a deterministic per-owner temp path,
    # validate it, then atomically replace the directory entry. A stale temp
    # left by process death is preserved and blocks further writes for this owner.
    temp_path = path.with_name(f".{path.name}.tmp")
    fd: int | None = None
    created_temp = False
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp_path, flags, 0o600)
        created_temp = True
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = None
            stream.write(original)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _decode(temp_path, row["owner_id"])
        os.replace(temp_path, path)
        created_temp = False
        _fsync_directory(path.parent)
    except (OSError, JournalCorruptError) as exc:
        # If replace succeeded but directory fsync failed, the API still denies
        # effect admission. A visible complete intent keeps the owner held.
        raise JournalUnavailableError("journal append was not verified") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if created_temp:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _record(owner_id: str, operation_id: str, attempt_id: str, event: str,
            component: str, target: str, ts: str | None) -> dict[str, Any]:
    from datetime import datetime, timezone

    row = {
        "version": SCHEMA_VERSION,
        "owner_id": _uuid(owner_id, "owner_id"),
        "operation_id": _uuid(operation_id, "operation_id"),
        "attempt_id": _uuid(attempt_id, "attempt_id"),
        "event": event,
        "component": component,
        "target": target,
        "ts": ts or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _validate_record(row, row["owner_id"])
    return row


# @lat: [[architecture#Intent-before-effect evidence]]
def admit_attempt(state_root: Path, *, owner_id: str, operation_id: str,
                  attempt_id: str, component: str, target: str,
                  ts: str | None = None) -> None:
    """Durably record an intent before the caller permits the external effect."""
    row = _record(owner_id, operation_id, attempt_id, "intent", component, target, ts)
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        _ensure_journal(journal)
        records = _decode(journal, _uuid(owner_id, "owner_id"))
        if records:
            # A terminal only proves the attempt returned to this process; it
            # does not prove same-owner parent consumption or closure. Until a
            # caller-owned durable resolution seam exists, never release the
            # owner for another operation/attempt, even after a terminal.
            raise JournalUnavailableError("owner lacks durable parent-resolution evidence")
        if any(item["attempt_id"] == row["attempt_id"] for item in records):
            raise JournalUnavailableError("attempt identity already used")
        _append(journal, row, records)


# @lat: [[architecture#Intent-before-effect evidence]]
def record_returned(state_root: Path, *, owner_id: str, operation_id: str,
                    attempt_id: str, component: str, target: str,
                    ts: str | None = None) -> None:
    """Record a returned call; this is not proof that its parent consumed it."""
    row = _record(owner_id, operation_id, attempt_id, "terminal", component, target, ts)
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        if not journal.exists():
            raise JournalUnavailableError("owner journal is missing")
        records = _decode(journal, _uuid(owner_id, "owner_id"))
        key = (_uuid(operation_id, "operation_id"), _uuid(attempt_id, "attempt_id"))
        intents = [r for r in records if r["event"] == "intent" and (r["operation_id"], r["attempt_id"]) == key]
        terminals = [r for r in records if r["event"] == "terminal" and (r["operation_id"], r["attempt_id"]) == key]
        if len(intents) != 1 or terminals:
            raise JournalUnavailableError("terminal does not match one unsettled intent")
        if intents[0]["component"] != row["component"] or intents[0]["target"] != row["target"]:
            raise JournalUnavailableError("terminal target differs from intent")
        _append(journal, row, records)


def read_owner_records(state_root: Path, owner_id: str) -> list[dict[str, Any]]:
    """Read validated metadata. Missing journal is distinct from an empty one."""
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        if not journal.exists():
            raise JournalUnavailableError("owner journal is missing")
        return _decode(journal, _uuid(owner_id, "owner_id"))
