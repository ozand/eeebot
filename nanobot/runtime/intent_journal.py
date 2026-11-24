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
import re
import stat
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 4096
MAX_OWNER_RECORDS = 256
MAX_OWNER_BYTES = 1_048_576
MAX_ROUTE_PARENTS = 128
_COMPONENTS = frozenset({"agent", "subagent", "memory", "heartbeat", "evaluator", "proposer", "goal_review", "bridge"})
_TARGETS = frozenset({
    "provider.chat", "provider.embedding", "tool.dispatch", "tool.read_file",
    "tool.write_file", "subagent.spawn", "memory.write", "heartbeat.run",
    "evaluator.run", "proposer.run", "bridge.cycle",
})
_ACTIVE_PARENT: ContextVar[tuple[Path, str] | None] = ContextVar("_intent_parent", default=None)
_ACTIVE_OPERATION: ContextVar["IntentOperation | None"] = ContextVar("_intent_operation", default=None)
_PARENT_SCHEMA = 3
_ROUTES = frozenset({"planner", "proposer", "bridge.cycle"})
_BRIDGE_CYCLE_PHASES = ("post_execution", "smoke_gate", "integration", "reflection")
_BRIDGE_CYCLE_ID_RX = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_BRIDGE_NO_EXECUTION_OUTCOMES = frozenset({"no_plan", "rest", "rest_unchanged"})


class JournalUnavailableError(RuntimeError):
    """The owner cannot safely admit or settle an attempt."""


class JournalCorruptError(JournalUnavailableError):
    """The owner journal is malformed or inconsistent; preserve it unchanged."""


@dataclass(frozen=True)
class RouteParentSnapshot:
    """Validated read-only lifecycle view for one selected autonomous parent."""

    route: str
    route_status: str
    owner_id: str
    parent_status: str
    parent_started: bool
    parent_consumed: bool
    active_operation_id: str | None
    last_operation_id: str | None
    no_execution_receipt: tuple[str, str] | None
    phase_receipts: tuple[tuple[str, str, str], ...]
    has_attempt_evidence: bool
    has_unresolved_attempts: bool


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
    if route not in _ROUTES:
        raise ValueError("autonomous parent route is not approved")
    return str(uuid5(NAMESPACE_URL, f"eeebot.intent-journal.parent.{route}"))


def _route_paths(state_root: Path, route: str) -> tuple[Path, Path]:
    if route not in _ROUTES:
        raise ValueError("autonomous parent route is not approved")
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
        raise JournalUnavailableError("cannot prepare route journal directory") from exc
    return directory / f"route-{route}.json", directory / f"route-{route}.lock"


def _read_route(path: Path, expected_route: str) -> dict[str, Any]:
    if path.with_name(f".{path.name}.tmp").exists():
        raise JournalUnavailableError("route record has an unresolved temporary file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise JournalUnavailableError("route-to-parent record is missing or unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "route", "status", "owner_id"}
        or value.get("version") != 1
        or value.get("route") != expected_route
        or value.get("status") not in {"provisioning", "active", "closed"}
    ):
        raise JournalCorruptError("route-to-parent record is malformed")
    try:
        _uuid(value["owner_id"], "owner_id")
    except ValueError as exc:
        raise JournalCorruptError("route-to-parent owner is malformed") from exc
    return value


def _write_route(path: Path, value: dict[str, Any]) -> None:
    _write_parent(path, value)
    if _read_route(path, value["route"]) != value:
        raise JournalUnavailableError("route-to-parent read-back mismatch")


def _new_parent_record(owner_id: str) -> dict[str, Any]:
    return {
        "version": _PARENT_SCHEMA, "owner_id": owner_id, "status": "active",
        "created_at": _now_iso(), "resolved_at": None,
        "active_operation_id": None, "last_operation_id": None,
        "parent_started": False, "parent_consumed": False,
    }


def _provision_route_owner(
    state_root: Path, owner_id: str, *, create_lock: bool = False,
) -> None:
    journal, lock = _owner_paths(state_root, owner_id)
    parent_path = journal.with_suffix(".parent.json")
    with _owner_lock(lock, create=create_lock):
        if parent_path.exists():
            parent = _read_parent(parent_path, owner_id)
            if not journal.exists():
                raise JournalUnavailableError("provisioning parent journal is missing")
            records = _decode(journal, owner_id)
            if (
                parent["status"] != "active" or parent["parent_started"]
                or parent["parent_consumed"] or parent.get("active_operation_id") is not None or records
            ):
                raise JournalUnavailableError("provisioning owner contains non-empty state")
            return
        if journal.exists() and _decode(journal, owner_id):
            raise JournalUnavailableError("provisioning journal is unexpectedly non-empty")
        _ensure_journal(journal)
        record = _new_parent_record(owner_id)
        _write_parent(parent_path, record)
        if _read_parent(parent_path, owner_id) != record:
            raise JournalUnavailableError("new parent record read-back mismatch")


def read_route_parent_state(
    state_root: Path, route: str,
) -> RouteParentSnapshot | None:
    """Read validated lifecycle/evidence for the selected owner without writes.

    ``None`` means the route has never been initialized. Once route metadata
    or its lock exists, missing files are unavailable, never evidence of a
    fresh owner. Lock ordering matches close/provision: route, then owner.
    """
    if route not in _ROUTES:
        raise ValueError("autonomous parent route is not approved")
    root = Path(state_root)
    if not root.is_dir():
        raise JournalUnavailableError("state root must already exist")
    directory = root / "intent-journal"
    if not directory.exists():
        return None
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise OSError("journal path is not a real directory")
    except OSError as exc:
        raise JournalUnavailableError("cannot inspect route journal directory") from exc

    route_path = directory / f"route-{route}.json"
    route_lock = directory / f"route-{route}.lock"
    route_temp = route_path.with_name(f".{route_path.name}.tmp")
    if route_temp.exists():
        raise JournalUnavailableError("route record has an unresolved temporary file")
    if not route_path.exists():
        legacy_journal = directory / f"{_route_owner_id(route)}.jsonl"
        if route_lock.exists():
            raise JournalUnavailableError("initialized route record is missing")
        if legacy_journal.with_suffix(".parent.json").exists():
            raise JournalUnavailableError("legacy route parent lacks durable closure evidence")
        if any(directory.glob(f"route-{route}-*.closed.json")):
            raise JournalUnavailableError("settled route owner remains without its route pointer")
        return None
    if not route_lock.exists():
        raise JournalUnavailableError("route lock is missing after initialization")

    with _owner_lock(route_lock):
        selected = _read_route(route_path, route)
        if selected["status"] == "provisioning":
            raise JournalUnavailableError("route parent provisioning is incomplete")
        owner_id = _uuid(selected["owner_id"], "owner_id")
        journal = directory / f"{owner_id}.jsonl"
        owner_lock = directory / f"{owner_id}.lock"
        parent_path = journal.with_suffix(".parent.json")
        if not owner_lock.exists():
            raise JournalUnavailableError("active route owner lock is missing")
        with _owner_lock(owner_lock):
            if not journal.exists():
                raise JournalUnavailableError("active route owner journal is missing")
            parent = _read_parent(parent_path, owner_id)
            records = _decode(journal, owner_id)
            if any(path.name.startswith(f".{journal.name}.") for path in directory.iterdir()):
                raise JournalUnavailableError("route owner has unresolved journal temp file")
            if selected["status"] == "closed":
                if parent["status"] != "resolved":
                    raise JournalCorruptError("closed route has an active parent")
                _verify_settled_route_parent(root, route, owner_id)
            if selected["status"] == "active" and parent["status"] == "resolved":
                raise JournalCorruptError("active route has a resolved parent")
            if not parent["parent_started"] and (
                parent.get("active_operation_id") is not None or records
            ):
                raise JournalCorruptError("unstarted route parent contains lifecycle evidence")
            latest = _latest_attempt_events(records)
            no_execution_rows = [row for row in records if row["event"] == "no_execution_consumed"]
            no_execution_receipt = None
            if len(no_execution_rows) == 1 and len(records) == 1:
                row = no_execution_rows[0]
                no_execution_receipt = (row["cycle_id"], row["outcome"])
            phases = tuple(
                (row["cycle_id"], row["phase"], row["checkpoint_head_sha"])
                for row in records if row["event"] == "phase_consumed"
            )
            return RouteParentSnapshot(
                route=route,
                route_status=selected["status"],
                owner_id=owner_id,
                parent_status=parent["status"],
                parent_started=parent["parent_started"],
                parent_consumed=parent["parent_consumed"],
                active_operation_id=parent["active_operation_id"],
                last_operation_id=parent["last_operation_id"],
                no_execution_receipt=no_execution_receipt,
                phase_receipts=phases,
                has_attempt_evidence=any(row["event"] == "intent" for row in records),
                has_unresolved_attempts=any(event != "consumed" for event in latest.values()),
            )


def provision_route_parent(state_root: Path, route: str) -> str:
    """Provision a new parent or reject an unconsumed persisted parent."""
    route_path, route_lock = _route_paths(state_root, route)
    directory = route_path.parent
    has_settled_markers = any(directory.glob(f"route-{route}-*.closed.json"))
    route_was_initialized = route_path.exists() or route_lock.exists() or has_settled_markers
    create_route_lock = not route_was_initialized
    legacy_id = _route_owner_id(route)
    legacy_journal, _legacy_lock = _owner_paths(state_root, legacy_id)
    with _owner_lock(route_lock, create=create_route_lock):
        current = _read_route(route_path, route) if route_path.exists() else None
        if current is None and route_was_initialized:
            raise JournalUnavailableError("planner route record is missing after initialization")
        if current is None and legacy_journal.with_suffix(".parent.json").exists():
            # Schema 1 had no durable parent-consumption transition. Do not
            # migrate or bypass that still-authoritative route owner.
            raise JournalUnavailableError("legacy planner parent lacks durable closure evidence")
        if current is not None and current["status"] == "active":
            journal, owner_lock = _owner_paths(state_root, current["owner_id"])
            with _owner_lock(owner_lock, create=False):
                parent_path = journal.with_suffix(".parent.json")
                parent = _read_parent(parent_path, current["owner_id"])
                if not journal.exists():
                    raise JournalUnavailableError("active planner parent journal is missing")
                records = _decode(journal, current["owner_id"])
                if parent["status"] == "resolved" and parent["parent_consumed"] and all(
                    event == "consumed" for event in _latest_attempt_events(records).values()
                ):
                    _record_settled_route_parent(state_root, route, current["owner_id"])
                    current["status"] = "closed"
                    _write_route(route_path, current)
                elif parent["status"] == "active":
                    # Return the same owner after restart. The caller's parent
                    # context performs the final unresolved-operation/attempt
                    # admission check before any effect.
                    return current["owner_id"]
                else:
                    raise JournalUnavailableError("planner parent evidence is inconsistent")
        if current is not None and current["status"] == "provisioning":
            owner_id = current["owner_id"]
            _provision_route_owner(state_root, owner_id, create_lock=False)
            current["status"] = "active"
            _write_route(route_path, current)
            return owner_id
        if current is not None and current["status"] not in {"closed"}:
            raise JournalUnavailableError("planner route state is not safely reusable")
        settled = _settled_route_owners(state_root, route)
        if current is not None and current["status"] == "closed":
            settled.add(current["owner_id"])
        if len(settled) >= MAX_ROUTE_PARENTS:
            raise JournalUnavailableError("planner route parent capacity exhausted")
        owner_id = str(uuid4())
        provisioning = {
            "version": 1, "route": route,
            "status": "provisioning", "owner_id": owner_id,
        }
        _owner_journal, owner_lock = _owner_paths(state_root, owner_id)
        with _owner_lock(owner_lock, create=True):
            pass
        _write_route(route_path, provisioning)
        _provision_route_owner(state_root, owner_id, create_lock=False)
        active = {**provisioning, "status": "active"}
        _write_route(route_path, active)
        return owner_id


def consume_bridge_cycle_phase(
    state_dir: Path, owner_id: str, *, cycle_id: str, phase: str,
    checkpoint_head_sha: str, checkpoint_status: Literal["complete"],
) -> None:
    """Record consumption only after the checkpoint reports complete/readback."""
    owner_id = _uuid(owner_id, "owner_id")
    state_root = Path(state_dir)
    if type(cycle_id) is not str or not _BRIDGE_CYCLE_ID_RX.fullmatch(cycle_id):
        raise JournalUnavailableError("bridge-cycle identifier is invalid")
    if checkpoint_status != "complete":
        raise JournalUnavailableError("bridge-cycle checkpoint phase is not complete")
    if phase not in _BRIDGE_CYCLE_PHASES or type(checkpoint_head_sha) is not str or not re.fullmatch(
        r"[a-fA-F0-9]{40,64}", checkpoint_head_sha,
    ):
        raise JournalUnavailableError("bridge-cycle phase receipt identity is invalid")
    row = {
        "version": SCHEMA_VERSION, "owner_id": owner_id, "cycle_id": cycle_id,
        "phase": phase, "checkpoint_head_sha": checkpoint_head_sha.lower(),
        "event": "phase_consumed", "ts": _now_iso(),
    }
    _validate_record(row, owner_id)
    route_path, route_lock = _route_paths(state_root, "bridge.cycle")
    with _owner_lock(route_lock):
        selected = _read_route(route_path, "bridge.cycle")
        if selected["status"] != "active" or selected["owner_id"] != owner_id:
            raise JournalUnavailableError("phase receipt owner is not the active bridge cycle")
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        if not journal.exists():
            raise JournalUnavailableError("bridge-cycle owner journal is missing")
        parent = _read_parent(journal.with_suffix(".parent.json"), owner_id)
        if parent["status"] != "active" or parent["parent_consumed"] or not parent["parent_started"]:
            raise JournalUnavailableError("bridge-cycle parent is not active")
        if parent.get("active_operation_id") is not None:
            raise JournalUnavailableError("bridge-cycle has an unresolved operation")
        records = _decode(journal, owner_id)
        if any(row["event"] == "no_execution_consumed" for row in records):
            raise JournalUnavailableError("no-execution terminal owner is close-only")
        if any(event != "consumed" for event in _latest_attempt_events(records).values()):
            raise JournalUnavailableError("bridge-cycle has unresolved attempt evidence")
        phase_rows = [r for r in records if r["event"] == "phase_consumed" and r["cycle_id"] == cycle_id]
        existing = next((r for r in phase_rows if r["phase"] == phase), None)
        if existing is not None:
            if existing["checkpoint_head_sha"] == row["checkpoint_head_sha"]:
                return
            raise JournalUnavailableError("bridge-cycle phase consumption conflicts with prior receipt")
        consumed = {r["phase"] for r in phase_rows}
        next_phase = next((name for name in _BRIDGE_CYCLE_PHASES if name not in consumed), None)
        if phase != next_phase:
            raise JournalUnavailableError("bridge-cycle phase receipt is out of order")
        _append(journal, row, records)
        observed = _decode(journal, owner_id)
        if not observed or observed[-1] != row:
            raise JournalUnavailableError("bridge-cycle phase receipt readback mismatch")


@contextmanager
def bridge_cycle_parent_context(
    state_dir: Path, *, cycle_id: str | None = None,
    completed_phase: str | None = None, checkpoint_head_sha: str | None = None,
    checkpoint_status: Literal["complete"] | None = None,
    no_execution_cycle_id: str | None = None,
    no_execution_outcome: str | None = None,
) -> Iterator[str]:
    """Start or resume one bridge cycle under its same durable route owner."""
    route = "bridge.cycle"
    state_root = Path(state_dir)
    owner_id = provision_route_parent(state_root, route)
    journal, owner_lock = _owner_paths(state_root, owner_id)
    with _owner_lock(owner_lock):
        parent = _read_parent(journal.with_suffix(".parent.json"), owner_id)
    resume_values = (
        cycle_id, completed_phase, checkpoint_head_sha, checkpoint_status,
        no_execution_cycle_id, no_execution_outcome,
    )
    if not parent["parent_started"]:
        if any(value is not None for value in resume_values):
            raise JournalUnavailableError("new bridge-cycle parent cannot accept resume evidence")
        with bridge_parent_context(state_root, owner_id, route=route):
            yield owner_id
        return
    if no_execution_outcome is not None or no_execution_cycle_id is not None:
        if cycle_id is not None or completed_phase is not None or checkpoint_head_sha is not None or checkpoint_status is not None:
            raise JournalUnavailableError("checkpoint and no-execution resume evidence cannot be mixed")
        _verify_bridge_cycle_no_execution(
            state_root, owner_id, cycle_id=no_execution_cycle_id, outcome=no_execution_outcome,
        )
        token = _ACTIVE_PARENT.set((state_root, owner_id))
        try:
            yield owner_id
        finally:
            _ACTIVE_PARENT.reset(token)
        return
    if any(value is None for value in resume_values[:4]):
        raise JournalUnavailableError("started bridge-cycle parent requires checkpoint or no-execution evidence")
    with resume_bridge_cycle_parent_context(
        state_root, owner_id, cycle_id=cycle_id,
        completed_phase=completed_phase, checkpoint_head_sha=checkpoint_head_sha,
        checkpoint_status=checkpoint_status,
    ):
        yield owner_id


@contextmanager
def resume_bridge_cycle_parent_context(
    state_dir: Path, owner_id: str, *, cycle_id: str,
    completed_phase: str, checkpoint_head_sha: str,
    checkpoint_status: Literal["complete"],
) -> Iterator[None]:
    """Reopen the same owner after caller verified the exact completed checkpoint."""
    owner_id = _uuid(owner_id, "owner_id")
    state_root = Path(state_dir)
    if type(cycle_id) is not str or not _BRIDGE_CYCLE_ID_RX.fullmatch(cycle_id):
        raise JournalUnavailableError("bridge-cycle identifier is invalid")
    if checkpoint_status != "complete":
        raise JournalUnavailableError("bridge-cycle checkpoint phase is not complete")
    if completed_phase not in _BRIDGE_CYCLE_PHASES:
        raise JournalUnavailableError("bridge-cycle checkpoint phase is not allowlisted")
    route_path, route_lock = _route_paths(state_root, "bridge.cycle")
    with _owner_lock(route_lock):
        selected = _read_route(route_path, "bridge.cycle")
        if selected["status"] != "active" or selected["owner_id"] != owner_id:
            raise JournalUnavailableError("bridge-cycle owner is no longer active")
    consume_bridge_cycle_phase(
        state_root, owner_id, cycle_id=cycle_id, phase=completed_phase,
        checkpoint_head_sha=checkpoint_head_sha, checkpoint_status=checkpoint_status,
    )
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        parent = _read_parent(journal.with_suffix(".parent.json"), owner_id)
        records = _decode(journal, owner_id)
        if (
            parent["status"] != "active" or parent["parent_consumed"]
            or not parent["parent_started"] or parent.get("active_operation_id") is not None
            or any(event != "consumed" for event in _latest_attempt_events(records).values())
        ):
            raise JournalUnavailableError("bridge-cycle parent has unresolved evidence")
    token = _ACTIVE_PARENT.set((Path(state_root), owner_id))
    try:
        yield
    finally:
        _ACTIVE_PARENT.reset(token)


def _verify_bridge_cycle_no_execution(
    state_root: Path, owner_id: str, *, cycle_id: str, outcome: str,
) -> None:
    if type(cycle_id) is not str or not _BRIDGE_CYCLE_ID_RX.fullmatch(cycle_id):
        raise JournalUnavailableError("bridge-cycle identifier is invalid")
    if type(outcome) is not str or outcome not in _BRIDGE_NO_EXECUTION_OUTCOMES:
        raise JournalUnavailableError("no-execution outcome is not allowlisted")
    route_path, route_lock = _route_paths(state_root, "bridge.cycle")
    with _owner_lock(route_lock):
        selected = _read_route(route_path, "bridge.cycle")
        if selected["status"] != "active" or selected["owner_id"] != owner_id:
            raise JournalUnavailableError("no-execution owner is not the active bridge cycle")
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        parent = _read_parent(journal.with_suffix(".parent.json"), owner_id)
        records = _decode(journal, owner_id)
        matching = [
            row for row in records
            if row["event"] == "no_execution_consumed" and row["cycle_id"] == cycle_id
        ]
        if len(matching) != 1 or matching[0]["outcome"] != outcome:
            raise JournalUnavailableError("no-execution terminal receipt is missing or mismatched")
        if (
            parent["status"] != "active" or parent["parent_consumed"]
            or parent.get("active_operation_id") is not None
            or any(row["event"] != "no_execution_consumed" for row in records)
            or any(row["event"] == "no_execution_consumed" and row["cycle_id"] != cycle_id for row in records)
        ):
            raise JournalUnavailableError("no-execution owner contains attempted or unresolved work")


def consume_bridge_cycle_no_execution(
    state_dir: Path, owner_id: str, *, cycle_id: str, outcome: str,
) -> None:
    """Persist an explicit normal planner terminal that selected no executor phase."""
    owner_id = _uuid(owner_id, "owner_id")
    state_root = Path(state_dir)
    if type(cycle_id) is not str or not _BRIDGE_CYCLE_ID_RX.fullmatch(cycle_id):
        raise JournalUnavailableError("bridge-cycle identifier is invalid")
    if type(outcome) is not str or outcome not in _BRIDGE_NO_EXECUTION_OUTCOMES:
        raise JournalUnavailableError("no-execution outcome is not allowlisted")
    row = {
        "version": SCHEMA_VERSION, "owner_id": owner_id, "cycle_id": cycle_id,
        "outcome": outcome, "event": "no_execution_consumed", "ts": _now_iso(),
    }
    _validate_record(row, owner_id)
    route_path, route_lock = _route_paths(state_root, "bridge.cycle")
    with _owner_lock(route_lock):
        selected = _read_route(route_path, "bridge.cycle")
        if selected["status"] != "active" or selected["owner_id"] != owner_id:
            raise JournalUnavailableError("no-execution owner is not the active bridge cycle")
    journal, lock = _owner_paths(state_root, owner_id)
    with _owner_lock(lock):
        if not journal.exists():
            raise JournalUnavailableError("bridge-cycle owner journal is missing")
        parent = _read_parent(journal.with_suffix(".parent.json"), owner_id)
        if parent["status"] != "active" or parent["parent_consumed"] or not parent["parent_started"]:
            raise JournalUnavailableError("bridge-cycle parent is not active")
        if parent.get("active_operation_id") is not None:
            raise JournalUnavailableError("bridge-cycle has an unresolved operation")
        records = _decode(journal, owner_id)
        if records:
            if len(records) == 1 and (
                records[0]["event"] == "no_execution_consumed"
                and records[0]["owner_id"] == owner_id
                and records[0]["cycle_id"] == cycle_id
                and records[0]["outcome"] == outcome
            ):
                return
            raise JournalUnavailableError("no-execution terminal cannot consume attempted work")
        _append(journal, row, records)
        observed = _decode(journal, owner_id)
        if not observed or observed[-1] != row:
            raise JournalUnavailableError("no-execution terminal readback mismatch")


def _settled_marker_path(state_root: Path, route: str, owner_id: str) -> Path:
    if route not in _ROUTES:
        raise ValueError("autonomous parent route is not approved")
    return Path(state_root) / "intent-journal" / f"route-{route}-{owner_id}.closed.json"


def _record_settled_route_parent(state_root: Path, route: str, owner_id: str) -> None:
    path = _settled_marker_path(state_root, route, owner_id)
    expected = {
        "version": 1, "route": route, "owner_id": owner_id,
        "closed_at": _now_iso(),
    }
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise JournalUnavailableError("settled parent marker is unreadable") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "route", "owner_id", "closed_at"}
            or value.get("version") != 1 or value.get("route") != route
            or value.get("owner_id") != owner_id or not isinstance(value.get("closed_at"), str)
        ):
            raise JournalCorruptError("settled parent marker is malformed")
        return
    _write_parent(path, expected)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise JournalUnavailableError("settled parent marker read-back failed") from exc
    if value != expected:
        raise JournalUnavailableError("settled parent marker read-back mismatch")


def _verify_settled_route_parent(state_root: Path, route: str, owner_id: str) -> None:
    """Require the exact read-only closure marker for a closed route owner."""
    path = _settled_marker_path(state_root, route, owner_id)
    if path.with_name(f".{path.name}.tmp").exists():
        raise JournalUnavailableError("settled parent marker has an unresolved temporary file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JournalUnavailableError("closed route settled marker is missing") from exc
    except Exception as exc:
        raise JournalUnavailableError("closed route settled marker is unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "route", "owner_id", "closed_at"}
        or type(value.get("version")) is not int or value["version"] != 1
        or value.get("route") != route or value.get("owner_id") != owner_id
        or not isinstance(value.get("closed_at"), str) or not value["closed_at"]
    ):
        raise JournalCorruptError("closed route settled marker is malformed")
    try:
        closed_at = datetime.fromisoformat(value["closed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise JournalCorruptError("closed route settled marker timestamp is malformed") from exc
    if closed_at.tzinfo is None:
        raise JournalCorruptError("closed route settled marker timestamp lacks timezone")


def _settled_route_owners(state_root: Path, route: str) -> set[str]:
    directory = Path(state_root) / "intent-journal"
    owners: set[str] = set()
    for path in directory.glob(f"route-{route}-*.closed.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(record, dict)
                or set(record) != {"version", "route", "owner_id", "closed_at"}
                or record.get("version") != 1 or record.get("route") != route
                or not isinstance(record.get("closed_at"), str)
            ):
                raise JournalCorruptError("settled parent marker is malformed")
            owner_id = _uuid(record["owner_id"], "owner_id")
            if path != _settled_marker_path(state_root, route, owner_id):
                raise JournalCorruptError("settled parent marker identity differs from path")
            owners.add(owner_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise JournalUnavailableError("settled parent index is corrupt") from exc
    return owners


def close_route_parent(
    state_root: Path, route: str, owner_id: str, *,
    cycle_id: str | None = None, required_phase_heads: dict[str, str] | None = None,
    no_execution_cycle_id: str | None = None, no_execution_outcome: str | None = None,
) -> bool:
    """Close after attempt settlement and, for bridge cycles, selected phase receipts."""
    owner_id = _uuid(owner_id, "owner_id")
    if route == "bridge.cycle":
        phase_close = cycle_id is not None and required_phase_heads is not None
        no_execution_close = no_execution_cycle_id is not None and no_execution_outcome is not None
        if phase_close == no_execution_close:
            return False
    elif any(value is not None for value in (
        cycle_id, required_phase_heads, no_execution_cycle_id, no_execution_outcome,
    )):
        raise JournalUnavailableError("bridge-cycle close arguments are only valid for bridge.cycle")
    route_path, route_lock = _route_paths(state_root, route)
    with _owner_lock(route_lock):
        selected = _read_route(route_path, route)
        if selected["status"] != "active" or selected["owner_id"] != owner_id:
            raise JournalUnavailableError("route does not select this active parent")
        journal, owner_lock = _owner_paths(state_root, owner_id)
        with _owner_lock(owner_lock):
            parent_path = journal.with_suffix(".parent.json")
            parent = _read_parent(parent_path, owner_id)
            if parent["status"] != "active" or parent["parent_consumed"]:
                raise JournalUnavailableError("parent is not active")
            if not parent["parent_started"]:
                raise JournalUnavailableError("planner parent was never started")
            if parent.get("active_operation_id") is not None:
                return False
            if not journal.exists():
                raise JournalUnavailableError("active planner parent journal is missing")
            records = _decode(journal, owner_id)
            if any(event != "consumed" for event in _latest_attempt_events(records).values()):
                return False
            if route == "bridge.cycle":
                if no_execution_close:
                    if cycle_id is not None or required_phase_heads is not None:
                        return False
                    if (
                        type(no_execution_cycle_id) is not str
                        or not _BRIDGE_CYCLE_ID_RX.fullmatch(no_execution_cycle_id)
                        or type(no_execution_outcome) is not str
                        or no_execution_outcome not in _BRIDGE_NO_EXECUTION_OUTCOMES
                    ):
                        return False
                    receipts = [
                        row for row in records
                        if row["event"] == "no_execution_consumed"
                        and row["cycle_id"] == no_execution_cycle_id
                    ]
                    if len(receipts) != 1 or receipts[0]["outcome"] != no_execution_outcome:
                        return False
                    if any(row["event"] != "no_execution_consumed" for row in records):
                        return False
                else:
                    if cycle_id is None or required_phase_heads is None:
                        return False
                    if type(cycle_id) is not str or not _BRIDGE_CYCLE_ID_RX.fullmatch(cycle_id):
                        return False
                    selected_phases = list(required_phase_heads)
                    if (
                        not selected_phases
                        or selected_phases != [phase for phase in _BRIDGE_CYCLE_PHASES if phase in selected_phases]
                        or any(phase not in _BRIDGE_CYCLE_PHASES for phase in selected_phases)
                    ):
                        return False
                    expected_heads = {}
                    for phase, head in required_phase_heads.items():
                        if type(head) is not str or not re.fullmatch(r"[a-fA-F0-9]{40,64}", head):
                            return False
                        expected_heads[phase] = head.lower()
                    phase_rows = [
                        row for row in records
                        if row["event"] == "phase_consumed" and row["cycle_id"] == cycle_id
                    ]
                    observed_heads = {row["phase"]: row["checkpoint_head_sha"] for row in phase_rows}
                    if observed_heads != expected_heads:
                        return False
            parent["parent_consumed"] = True
            parent["status"] = "resolved"
            parent["resolved_at"] = _now_iso()
            _write_parent(parent_path, parent)
            if _read_parent(parent_path, owner_id) != parent:
                raise JournalUnavailableError("parent consumption/closure read-back mismatch")
        _record_settled_route_parent(state_root, route, owner_id)
        selected["status"] = "closed"
        _write_route(route_path, selected)
        return True


@contextmanager
def route_parent_context(state_root: Path, route: str) -> Iterator[str]:
    """Provision and close one bridge-owned autonomous invocation parent."""
    owner_id = provision_route_parent(state_root, route)
    with bridge_parent_context(state_root, owner_id, route=route):
        yield owner_id
        if not close_route_parent(state_root, route, owner_id):
            raise JournalUnavailableError(f"{route} parent consumption was not verified")


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


class IntentHttpHooks:
    """Capture one SDK HTTP dispatch per request/response hook pair."""

    def __init__(self, operation: "IntentOperation") -> None:
        self._operation = operation
        self.failure: BaseException | None = None
        self._pending: ContextVar[tuple[IntentAttempt, bool] | None] = ContextVar(
            f"_intent_http_attempt_{id(self)}", default=None,
        )
        self._seen: ContextVar[bool] = ContextVar(
            f"_intent_http_seen_{id(self)}", default=False,
        )

    def on_request(self, request: Any) -> None:
        try:
            previous = self._pending.get()
            if previous is not None:
                attempt, returned = previous
                if not returned:
                    raise JournalUnavailableError("previous HTTP attempt has unknown settlement")
                self._operation.consume(attempt)
            attempt = self._operation.admit()
            self._pending.set((attempt, False))
            self._seen.set(True)
        except BaseException as exc:
            self.failure = exc
            raise

    def on_response(self, response: Any) -> None:
        pending = self._pending.get()
        if pending is None or pending[1]:
            raise JournalUnavailableError("HTTP response has no unique admitted intent")
        self._operation.returned(pending[0])
        self._pending.set((pending[0], True))

    @property
    def has_seen_request(self) -> bool:
        return self._seen.get()

    @property
    def has_unresolved_attempt(self) -> bool:
        pending = self._pending.get()
        return pending is not None and not pending[1]

    def consume_final(self) -> None:
        pending = self._pending.get()
        if pending is not None:
            if not pending[1]:
                raise JournalUnavailableError("final HTTP attempt has unknown settlement")
            self._operation.consume(pending[0])
            self._pending.set(None)


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
    create_lock = not journal.exists() and not path.exists()
    with _owner_lock(lock, create=create_lock):
        if path.exists():
            raise JournalUnavailableError("parent identity already exists")
        _ensure_journal(journal)
        value = _new_parent_record(owner_id)
        _write_parent(path, value)
        if _read_parent(path, owner_id) != value:
            raise JournalUnavailableError("parent record read-back mismatch")


def _read_parent(path: Path, owner_id: str) -> dict[str, Any]:
    if path.with_name(f".{path.name}.tmp").exists():
        raise JournalUnavailableError("parent record has an unresolved temporary file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise JournalUnavailableError("parent record missing or unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {
            "version", "owner_id", "status", "created_at", "resolved_at",
            "active_operation_id", "last_operation_id", "parent_started", "parent_consumed",
        }
        or value.get("version") != _PARENT_SCHEMA
        or value.get("owner_id") != owner_id
        or value.get("status") not in {"active", "resolved"}
        or not isinstance(value.get("parent_started"), bool)
        or not isinstance(value.get("parent_consumed"), bool)
        or not isinstance(value.get("created_at"), str)
    ):
        raise JournalCorruptError("parent record malformed")
    for field in ("active_operation_id", "last_operation_id"):
        if value[field] is not None:
            try:
                _uuid(value[field], field)
            except ValueError as exc:
                raise JournalCorruptError("parent operation identity malformed") from exc
    if (
        (value["status"] == "active" and (value["parent_consumed"] or value["resolved_at"] is not None))
        or (value["status"] == "resolved" and (
            not value["parent_consumed"] or value["active_operation_id"] is not None
            or not isinstance(value["resolved_at"], str)
        ))
    ):
        raise JournalCorruptError("parent lifecycle evidence is inconsistent")
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


@contextmanager
def bridge_parent_context(
    state_root: Path, owner_id: str, route: str = "planner",
) -> Iterator[None]:
    """Validate an initial bridge parent token; production cycle serialization stays in bridge.lock."""
    route_path, route_lock = _route_paths(state_root, route)
    with _owner_lock(route_lock):
        selected_route = _read_route(route_path, route)
        if selected_route["status"] != "active" or selected_route["owner_id"] != _uuid(owner_id, "owner_id"):
            raise JournalUnavailableError("bridge parent token is not the active route owner")
    journal, owner_lock = _owner_paths(state_root, owner_id)
    with _owner_lock(owner_lock):
        parent = _read_parent(journal.with_suffix(".parent.json"), owner_id)
        if parent["status"] != "active" or parent["parent_consumed"]:
            raise JournalUnavailableError("bridge parent is closed")
        if not journal.exists():
            raise JournalUnavailableError("active bridge parent journal is missing")
        if parent.get("active_operation_id") is not None:
            raise JournalUnavailableError("bridge parent has unresolved active operation")
        records = _decode(journal, owner_id)
        if parent["parent_started"]:
            if route != "bridge.cycle":
                raise JournalUnavailableError("planner parent was already started; continuation is held")
            if any(path.name.startswith(f".{journal.name}.") for path in journal.parent.iterdir()):
                raise JournalUnavailableError("bridge-cycle journal has unresolved temporary evidence")
            if any(event != "consumed" for event in _latest_attempt_events(records).values()):
                raise JournalUnavailableError("bridge-cycle attempts are unresolved")
        else:
            if records:
                raise JournalUnavailableError("bridge parent has prior attempts without parent consumption")
            parent["parent_started"] = True
            parent_path = journal.with_suffix(".parent.json")
            _write_parent(parent_path, parent)
            if _read_parent(parent_path, owner_id) != parent:
                raise JournalUnavailableError("parent-start transition read-back mismatch")
    token = _ACTIVE_PARENT.set((Path(state_root), _uuid(owner_id, "owner_id")))
    try:
        yield
    finally:
        _ACTIVE_PARENT.reset(token)


def set_parent_context(state_root: Path, owner_id: str) -> Token:
    """Set context for focused journal logic tests without bridge routing."""
    return _ACTIVE_PARENT.set((Path(state_root), _uuid(owner_id, "owner_id")))


def reset_parent_context(token: Token) -> None:
    _ACTIVE_PARENT.reset(token)


def _require_effect_admission_allowed(records: list[dict[str, Any]]) -> None:
    if any(row["event"] == "no_execution_consumed" for row in records):
        raise JournalUnavailableError("no-execution terminal owner is close-only")


def _latest_attempt_events(
    records: list[dict[str, Any]], operation_id: str | None = None,
) -> dict[tuple[str, str], str]:
    latest: dict[tuple[str, str], str] = {}
    for row in records:
        if row["event"] in {"phase_consumed", "no_execution_consumed"}:
            continue
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
        if not journal.exists():
            raise JournalUnavailableError("active parent journal is missing")
        records = _decode(journal, operation.owner_id)
        _require_effect_admission_allowed(records)
        if any(event != "consumed" for event in _latest_attempt_events(records).values()):
            raise JournalUnavailableError("parent has unresolved attempts")
        parent["active_operation_id"] = operation.operation_id
        _write_parent(parent_path, parent)
        if _read_parent(parent_path, operation.owner_id) != parent:
            raise JournalUnavailableError("active operation read-back mismatch")


def _close_operation(operation: IntentOperation) -> None:
    journal, lock = _owner_paths(operation.state_root, operation.owner_id)
    with _owner_lock(lock):
        if not journal.exists():
            raise JournalUnavailableError("active operation journal is missing")
        records = _decode(journal, operation.owner_id)
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
def _owner_lock(path: Path, *, create: bool = False) -> Iterator[None]:
    if fcntl is None:
        raise JournalUnavailableError("exclusive flock is unsupported")
    fd: int | None = None
    try:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if create:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(path, flags, 0o600)
        else:
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
    phases_by_cycle: dict[str, dict[str, str]] = {}
    bridge_cycle_id: str | None = None
    bridge_terminal_kind: str | None = None
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
        if row["event"] in {"no_execution_consumed", "phase_consumed"}:
            kind = row["event"]
            if bridge_cycle_id is None:
                bridge_cycle_id = row["cycle_id"]
                bridge_terminal_kind = kind
            elif bridge_cycle_id != row["cycle_id"] or bridge_terminal_kind != kind:
                raise JournalCorruptError("bridge-cycle owner has mixed cycle or terminal receipts")
            if kind == "no_execution_consumed":
                if records:
                    raise JournalCorruptError("no-execution terminal conflicts with prior owner evidence")
                records.append(row)
                continue
            phases = phases_by_cycle.setdefault(row["cycle_id"], {})
            if row["phase"] in phases:
                raise JournalCorruptError("duplicate bridge-cycle phase receipt")
            previous = [phase for phase in _BRIDGE_CYCLE_PHASES if phase in phases]
            if (
                (not previous and row["phase"] != _BRIDGE_CYCLE_PHASES[0])
                or (previous and _BRIDGE_CYCLE_PHASES.index(row["phase"]) <= _BRIDGE_CYCLE_PHASES.index(previous[-1]))
                or len(phases_by_cycle) > 1
            ):
                raise JournalCorruptError("bridge-cycle phase receipts are out of order")
            phases[row["phase"]] = row["checkpoint_head_sha"]
            records.append(row)
            continue
        if bridge_terminal_kind == "no_execution_consumed":
            raise JournalCorruptError("effect record follows no-execution terminal receipt")
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
    no_execution_fields = {"version", "owner_id", "cycle_id", "outcome", "event", "ts"}
    if isinstance(row, dict) and row.get("event") == "no_execution_consumed":
        if set(row) != no_execution_fields:
            raise ValueError("no-execution receipt schema mismatch")
        if type(row["version"]) is not int or row["version"] != SCHEMA_VERSION or row["owner_id"] != owner_id:
            raise ValueError("no-execution receipt version or owner mismatch")
        if type(row["cycle_id"]) is not str or not _BRIDGE_CYCLE_ID_RX.fullmatch(row["cycle_id"]):
            raise ValueError("no-execution receipt cycle identity is invalid")
        if type(row["outcome"]) is not str or row["outcome"] not in _BRIDGE_NO_EXECUTION_OUTCOMES:
            raise ValueError("no-execution receipt outcome is not allowlisted")
        if not isinstance(row["ts"], str) or len(row["ts"]) > 40:
            raise ValueError("no-execution receipt timestamp is invalid")
        from datetime import datetime
        try:
            parsed_ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("no-execution receipt timestamp is invalid") from exc
        if parsed_ts.tzinfo is None:
            raise ValueError("no-execution receipt timestamp needs timezone")
        return
    phase_fields = {"version", "owner_id", "cycle_id", "phase", "checkpoint_head_sha", "event", "ts"}
    if isinstance(row, dict) and row.get("event") == "phase_consumed":
        if set(row) != phase_fields:
            raise ValueError("phase receipt schema mismatch")
        if type(row["version"]) is not int or row["version"] != SCHEMA_VERSION or row["owner_id"] != owner_id:
            raise ValueError("phase receipt version or owner mismatch")
        if type(row["cycle_id"]) is not str or not _BRIDGE_CYCLE_ID_RX.fullmatch(row["cycle_id"]):
            raise ValueError("phase receipt cycle identity is invalid")
        if type(row["phase"]) is not str or row["phase"] not in _BRIDGE_CYCLE_PHASES:
            raise ValueError("phase receipt phase is not allowlisted")
        if type(row["checkpoint_head_sha"]) is not str or not re.fullmatch(
            r"[a-f0-9]{40,64}", row["checkpoint_head_sha"],
        ):
            raise ValueError("phase receipt checkpoint head is invalid")
        if not isinstance(row["ts"], str) or len(row["ts"]) > 40:
            raise ValueError("phase receipt timestamp is invalid")
        from datetime import datetime
        try:
            parsed_ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("phase receipt timestamp is invalid") from exc
        if parsed_ts.tzinfo is None:
            raise ValueError("phase receipt timestamp needs timezone")
        return
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
    with _owner_lock(lock, create=_test_only):
        _ensure_journal(journal)
        records = _decode(journal, _uuid(owner_id, "owner_id"))
        _require_effect_admission_allowed(records)
        parent = _ACTIVE_PARENT.get()
        operation = _ACTIVE_OPERATION.get()
        if not _test_only and (parent != (Path(state_root), row["owner_id"]) or operation is None or (
            operation.operation_id != row["operation_id"] or operation.component != row["component"]
            or operation.target != row["target"]
        )):
            raise JournalUnavailableError("attempt lacks matching active parent operation")
        if not _test_only:
            if not journal.exists():
                raise JournalUnavailableError("active parent journal is missing")
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
    if not journal.exists() and not lock.exists():
        raise JournalUnavailableError("owner journal is missing")
    with _owner_lock(lock):
        if not journal.exists():
            raise JournalUnavailableError("owner journal is missing")
        records = _decode(journal, _uuid(owner_id, "owner_id"))
        if any(path.name.startswith(f".{journal.name}.") for path in journal.parent.iterdir()):
            raise JournalUnavailableError("owner has an unresolved journal temp file")
        return records
