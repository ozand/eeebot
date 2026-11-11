"""Strict, bounded per-owner evidence for application-controlled attempts.

Callers own durable parent identity and retry/cost/cancellation budgets; this
module records admission, terminal returns, and explicit consumption without
invoking providers or tools.
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
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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
_ACTIVE_PARENT: ContextVar[tuple[Path, str] | None] = ContextVar("_intent_parent", default=None)
_ACTIVE_OPERATION: ContextVar["IntentOperation | None"] = ContextVar("_intent_operation", default=None)
_PARENT_SCHEMA = 1


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


def _route_owner_id(route: str) -> str:
    if route != "planner":
        raise ValueError("autonomous parent route is not approved")
    return str(uuid5(NAMESPACE_URL, "eeebot.intent-journal.parent.planner"))


def ensure_parent(state_root: Path, route: str) -> str:
    """Return the one persistent parent identity for an approved route."""
    owner_id = _route_owner_id(route)
    journal, lock = _owner_paths(state_root, owner_id)
    parent_path = journal.with_suffix(".parent.json")
    with _owner_lock(lock):
        if parent_path.exists():
            parent = _read_parent(parent_path, owner_id)
            if parent.get("active_operation_id") is not None:
                raise JournalUnavailableError("route parent has unresolved active operation")
            journal_records = _decode(journal, owner_id) if journal.exists() else []
            if any(event != "consumed" for event in _latest_attempt_events(journal_records).values()):
                raise JournalUnavailableError("route parent has unresolved attempts")
        else:
            value = {
                "version": 1, "owner_id": owner_id, "status": "active",
                "created_at": _now_iso(), "resolved_at": None,
                "active_operation_id": None, "last_operation_id": None,
            }
            _write_parent(parent_path, value)
            if _read_parent(parent_path, owner_id) != value:
                raise JournalUnavailableError("parent record read-back mismatch")
    return owner_id


def open_parent_context(state_root: Path, route: str) -> Token:
    owner_id = ensure_parent(state_root, route)
    return set_parent_context(state_root, owner_id)


def open_existing_parent_context(state_root: Path, owner_id: str) -> Token:
    return set_parent_context(state_root, owner_id)


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


@dataclass(frozen=True)
class IntentAttempt:
    owner_id: str
    operation_id: str
    attempt_id: str
    component: str
    target: str


@dataclass
class IntentOperation:
    state_root: Path
    owner_id: str
    operation_id: str
    component: str
    target: str
    _attempts: list[tuple[IntentAttempt, bool]] | None = None
    _closed: bool = False

    def admit(self) -> IntentAttempt:
        if self._closed:
            raise JournalUnavailableError("operation is already closed")
        attempt = IntentAttempt(self.owner_id, self.operation_id, str(uuid4()), self.component, self.target)
        admit_attempt(
            self.state_root, owner_id=self.owner_id, operation_id=self.operation_id,
            attempt_id=attempt.attempt_id, component=self.component, target=self.target,
        )
        if self._attempts is None:
            self._attempts = []
        self._attempts.append((attempt, False))
        return attempt

    def returned(self, attempt: IntentAttempt) -> None:
        record_returned(
            self.state_root, owner_id=self.owner_id, operation_id=self.operation_id,
            attempt_id=attempt.attempt_id, component=self.component, target=self.target,
        )

    def consume(self, attempt: IntentAttempt) -> None:
        _record_consumed(
            self.state_root, owner_id=self.owner_id, operation_id=self.operation_id,
            attempt_id=attempt.attempt_id, component=self.component, target=self.target,
        )
        if self._attempts is not None:
            self._attempts = [(item, True if item == attempt else done) for item, done in self._attempts]

    def consume_pending(self) -> None:
        for attempt, consumed in self._attempts or []:
            if not consumed:
                self.consume(attempt)

    def close(self) -> None:
        if self._closed:
            return
        _close_operation(self)
        self._closed = True


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_parent(state_root: Path, owner_id: str) -> None:
    owner_id = _uuid(owner_id, "owner_id")
    journal, lock = _owner_paths(state_root, owner_id)
    path = journal.with_suffix(".parent.json")
    with _owner_lock(lock):
        if path.exists():
            raise JournalUnavailableError("parent identity already exists")
        value = {
            "version": 1, "owner_id": owner_id, "status": "active",
            "created_at": _now_iso(), "resolved_at": None,
            "active_operation_id": None, "last_operation_id": None,
        }
        _write_parent(path, value)
        if _read_parent(path, owner_id) != value:
            raise JournalUnavailableError("parent record read-back mismatch")


def _read_parent(path: Path, owner_id: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise JournalUnavailableError("parent record missing or unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {
            "version", "owner_id", "status", "created_at", "resolved_at",
            "active_operation_id", "last_operation_id",
        }
        or value.get("version") != 1
        or value.get("owner_id") != owner_id
        or value.get("status") not in {"active", "resolved"}
    ):
        raise JournalCorruptError("parent record malformed")
    return value


def _write_parent(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    fd: int | None = None
    created = False
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        created = True
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = None
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        created = False
        _fsync_directory(path.parent)
    except OSError as exc:
        raise JournalUnavailableError("parent record write was not verified") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if created:
            try:
                temp.unlink()
            except OSError:
                pass


def set_parent_context(state_root: Path, owner_id: str) -> Token:
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        parent = _read_parent(journal.with_suffix(".parent.json"), owner_id)
        if parent["status"] != "active":
            raise JournalUnavailableError("parent is resolved")
        if parent.get("active_operation_id") is not None:
            raise JournalUnavailableError("parent already has an active or unresolved operation")
        records = _decode(journal, owner_id) if journal.exists() else []
        if any(event != "consumed" for event in _latest_attempt_events(records).values()):
            raise JournalUnavailableError("parent has unresolved attempts")
    return _ACTIVE_PARENT.set((Path(state_root), owner_id))


def reset_parent_context(token: Token) -> None:
    _ACTIVE_PARENT.reset(token)


def _latest_attempt_events(
    records: list[dict[str, Any]], operation_id: str | None = None,
) -> dict[tuple[str, str], str]:
    latest: dict[tuple[str, str], str] = {}
    for row in records:
        if operation_id is None or row["operation_id"] == operation_id:
            latest[(row["operation_id"], row["attempt_id"])] = row["event"]
    return latest


def _begin_operation(operation: IntentOperation) -> None:
    journal, lock = _owner_paths(operation.state_root, operation.owner_id)
    with _owner_lock(lock):
        parent_path = journal.with_suffix(".parent.json")
        parent = _read_parent(parent_path, operation.owner_id)
        if parent["status"] != "active" or parent.get("active_operation_id") is not None:
            raise JournalUnavailableError("parent already has an active or unresolved operation")
        records = _decode(journal, operation.owner_id) if journal.exists() else []
        if any(event != "consumed" for event in _latest_attempt_events(records).values()):
            raise JournalUnavailableError("parent has unresolved attempts")
        parent["active_operation_id"] = operation.operation_id
        _write_parent(parent_path, parent)
        if _read_parent(parent_path, operation.owner_id) != parent:
            raise JournalUnavailableError("active operation read-back mismatch")


def _close_operation(operation: IntentOperation) -> None:
    journal, lock = _owner_paths(operation.state_root, operation.owner_id)
    with _owner_lock(lock):
        records = _decode(journal, operation.owner_id) if journal.exists() else []
        selected = [row for row in records if row["operation_id"] == operation.operation_id]
        if any(row["component"] != operation.component or row["target"] != operation.target for row in selected):
            raise JournalUnavailableError("operation identity mismatch")
        if any(
            event != "consumed"
            for event in _latest_attempt_events(selected, operation.operation_id).values()
        ):
            raise JournalUnavailableError("operation has unresolved attempts")
        parent_path = journal.with_suffix(".parent.json")
        parent = _read_parent(parent_path, operation.owner_id)
        if parent.get("active_operation_id") != operation.operation_id:
            raise JournalUnavailableError("parent active operation does not match")
        # A returned provider/tool result is a consumed outcome for this
        # in-process planner turn. A crash before durable closure still leaves
        # active_operation_id set and therefore holds the parent.
        # The in-process parent consumed all operation results before this
        # durable closure. If the process stops before this write, recovery
        # sees the active operation and holds admission.
        parent["active_operation_id"] = None
        parent["last_operation_id"] = operation.operation_id
        _write_parent(parent_path, parent)
        if _read_parent(parent_path, operation.owner_id) != parent:
            raise JournalUnavailableError("parent operation closure read-back mismatch")


def current_operation() -> IntentOperation | None:
    return _ACTIVE_OPERATION.get()


@contextmanager
def operation_context(
    component: str, target: str, *, state_root: Path | None = None,
) -> Iterator[IntentOperation | None]:
    parent = _ACTIVE_PARENT.get()
    if state_root is not None:
        if parent is None or parent[0] != Path(state_root):
            yield None
            return
    if parent is None:
        yield None
        return
    if component not in _COMPONENTS or target not in _TARGETS:
        raise JournalUnavailableError("component or target is not allowlisted")
    state_root, owner_id = parent
    operation = IntentOperation(state_root, owner_id, str(uuid4()), component, target)
    _begin_operation(operation)
    token = _ACTIVE_OPERATION.set(operation)
    try:
        yield operation
    except BaseException:
        # Interruption or an unverified caller outcome keeps the active
        # operation held for explicit recovery.
        raise
    finally:
        _ACTIVE_OPERATION.reset(token)


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
        elif os.name != "nt":
            _fsync_directory(path.parent)
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
    states: dict[tuple[str, str], str] = {}
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
            states[key] = "intent"
        elif row["event"] == "terminal":
            if states.get(key) != "intent":
                raise JournalCorruptError("terminal has no unique unsettled intent")
            states[key] = "terminal"
        else:
            if states.get(key) != "terminal":
                raise JournalCorruptError("consumed record has no terminal")
            states[key] = "consumed"
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
    if row["event"] not in {"intent", "terminal", "consumed"}:
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
    reserved_terminal_records = 2 if row["event"] == "intent" else 1 if row["event"] == "terminal" else 0
    reserved_terminal_bytes = MAX_RECORD_BYTES * reserved_terminal_records
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
                  ts: str | None = None, _test_only: bool = False) -> None:
    """Durably record an intent before the caller permits the external effect."""
    row = _record(owner_id, operation_id, attempt_id, "intent", component, target, ts)
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        _ensure_journal(journal)
        records = _decode(journal, _uuid(owner_id, "owner_id"))
        parent = _ACTIVE_PARENT.get()
        operation = _ACTIVE_OPERATION.get()
        if not _test_only and (parent != (Path(state_root), row["owner_id"]) or operation is None or (
            operation.operation_id != row["operation_id"] or operation.component != row["component"]
            or operation.target != row["target"]
        )):
            raise JournalUnavailableError("attempt lacks matching active parent operation")
        if not _test_only:
            parent_state = _read_parent(journal.with_suffix(".parent.json"), row["owner_id"])
            if (parent_state["status"] != "active"
                    or parent_state.get("active_operation_id") != row["operation_id"]):
                raise JournalUnavailableError("parent operation is not active")
        if any(item["attempt_id"] == row["attempt_id"] for item in records):
            raise JournalUnavailableError("attempt identity already used")
        if records and _test_only and any(item["event"] != "consumed" for item in records):
            raise JournalUnavailableError("owner lacks durable parent-resolution evidence")
        other_operations = {
            key: event for key, event in _latest_attempt_events(records).items()
            if key[0] != row["operation_id"]
        }
        if not _test_only and any(event != "consumed" for event in other_operations.values()):
            raise JournalUnavailableError("owner lacks durable parent-resolution evidence")
        prior_same_operation = _latest_attempt_events(records, row["operation_id"])
        if prior_same_operation and not _test_only and any(
            event != "consumed" for event in prior_same_operation.values()
        ):
            raise JournalUnavailableError("parent operation must resolve previous attempt before retry")
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


def _record_consumed(state_root: Path, *, owner_id: str, operation_id: str,
                     attempt_id: str, component: str, target: str) -> None:
    row = _record(owner_id, operation_id, attempt_id, "consumed", component, target, None)
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        records = _decode(journal, _uuid(owner_id, "owner_id"))
        key = (row["operation_id"], row["attempt_id"])
        intents = [r for r in records if r["event"] == "intent" and (r["operation_id"], r["attempt_id"]) == key]
        terminals = [r for r in records if r["event"] == "terminal" and (r["operation_id"], r["attempt_id"]) == key]
        consumed = [r for r in records if r["event"] == "consumed" and (r["operation_id"], r["attempt_id"]) == key]
        if len(intents) != 1 or len(terminals) != 1 or consumed:
            raise JournalUnavailableError("consumption does not match a terminal")
        if intents[0]["component"] != row["component"] or intents[0]["target"] != row["target"]:
            raise JournalUnavailableError("consumption target differs from intent")
        _append(journal, row, records)


def read_owner_records(state_root: Path, owner_id: str) -> list[dict[str, Any]]:
    """Read validated metadata. Missing journal is distinct from an empty one."""
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        if not journal.exists():
            raise JournalUnavailableError("owner journal is missing")
        records = _decode(journal, _uuid(owner_id, "owner_id"))
        if any(path.name.startswith(f".{journal.name}.") for path in journal.parent.iterdir()):
            raise JournalUnavailableError("owner has an unresolved journal temp file")
        return records
