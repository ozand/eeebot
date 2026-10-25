"""Fail-closed operator resume for stopped no-plan recovery state."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from nanobot.runtime import no_plan_recovery

_SCHEMA = "no-plan-recovery-v1"


class AuditWriteError(Exception):
    def __init__(self, *, attempted: bool, durable: bool):
        self.attempted = attempted
        self.durable = durable
        super().__init__("audit write failed")


class ResumeRefusedError(Exception):
    def __init__(self, category: str, *, result: str = "refused", state: str = "unchanged", audit: str = "not_attempted", durability: str = "not_attempted"):
        self.category = category
        self.result = result
        self.state = state
        self.audit = audit
        self.durability = durability
        super().__init__(category)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate(raw: Any) -> dict[str, Any]:
    fields = no_plan_recovery.RecoveryState.__dataclass_fields__
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        raise ResumeRefusedError("invalid_state")
    for name, field in fields.items():
        if name not in raw:
            raise ResumeRefusedError("invalid_state")
        value = raw[name]
        if name in ("consecutive_supply", "consecutive_planner"):
            if type(value) is not int or value < 0:
                raise ResumeRefusedError("invalid_state")
        elif name == "deferred_verdict_ids":
            if not isinstance(value, list) or any(type(item) is not str for item in value):
                raise ResumeRefusedError("invalid_state")
        elif type(value) is not bool:
            raise ResumeRefusedError("invalid_state")
    if raw["stopped"] is not True:
        raise ResumeRefusedError("not_stopped")
    return dict(raw)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_state(path: Path, raw: dict[str, Any]) -> None:
    temp_path = None
    replaced = False
    durable = False
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as fh:
            temp_path = Path(fh.name)
            json.dump(raw, fh, ensure_ascii=False, indent=2, allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp_path, path.stat().st_mode & 0o777)
        os.replace(temp_path, path)
        temp_path = None
        replaced = True
        _fsync_directory(path.parent)
        durable = True
        if json.loads(path.read_text(encoding="utf-8")) != raw:
            raise OSError("state verification failed")
        return
    except Exception as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if replaced:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                status = "verified" if current.get("stopped") is False else "unverified"
            except Exception:
                status = "unverified"
            durability = "verified" if durable else "unverified"
            raise ResumeRefusedError("state_durability_unverified", result="partial", state=status, durability=durability) from exc
        raise ResumeRefusedError("state_write_failed") from exc


def _reject_symlink_components(path: Path) -> None:
    """Reject static symlink components; bridge.lock coordinates conforming writers."""
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ResumeRefusedError("unsafe_state_path")


def _preflight_ledger(state_root: Path) -> tuple[Path, bytes]:
    from nanobot.runtime.cycle_ledger import _LEDGER_FILENAME, _ledger_dir

    directory = _ledger_dir(state_root)
    ledger = directory / _LEDGER_FILENAME
    _reject_symlink_components(directory)
    _reject_symlink_components(ledger)
    from nanobot.runtime.day_key import cutover_marker_path

    _reject_symlink_components(cutover_marker_path(directory))
    for archive in directory.glob("cycles-*.jsonl.gz"):
        _reject_symlink_components(archive)
    if not directory.exists():
        return ledger, b""
    if not directory.is_dir():
        raise ResumeRefusedError("unsafe_ledger_path")
    if not ledger.exists():
        return ledger, b""
    if not stat.S_ISREG(ledger.stat().st_mode):
        raise ResumeRefusedError("unsafe_ledger_path")
    try:
        previous = ledger.read_bytes()
    except OSError as exc:
        raise ResumeRefusedError("ledger_unavailable") from exc
    if previous and not previous.endswith(b"\n"):
        raise ResumeRefusedError("ledger_unterminated_line")
    return ledger, previous


def _append_transition(state_root: Path, count: int, *, now=None) -> dict[str, Any]:
    from datetime import datetime, timezone

    from nanobot.runtime import day_key
    from nanobot.runtime.cycle_ledger import (
        _LEDGER_FILENAME,
        _append_transition_marker,
        _ledger_dir,
        _retention_days,
        _rotate_and_prune,
    )

    directory = _ledger_dir(state_root)
    created = not directory.exists()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if created:
            _fsync_directory(directory.parent)
    except Exception as exc:
        raise AuditWriteError(attempted=False, durable=False) from exc
    ledger = directory / _LEDGER_FILENAME
    moment = now or datetime.now(timezone.utc)
    had_prior_history = any(directory.glob("cycles-*.jsonl.gz"))
    current_day = day_key.day_key(now)
    _rotate_and_prune(directory, ledger, current_day, _retention_days())
    cutover, just_recorded = day_key.record_cutover_if_absent(day_key.cutover_marker_path(directory), moment)
    local_tz = now.tzinfo if now is not None else None
    if just_recorded and had_prior_history and day_key.is_transition_day(
        current_day, local_tz=local_tz, cutover_utc=cutover
    ):
        _append_transition_marker(ledger, current_day, cutover, moment)
        _fsync_directory(directory)
    row = {
        "phase": "no_plan_recovery",
        "cycle_id": "operator-resume",
        "family": "planner",
        "consecutive_count": count,
        "state": "normal",
        "ts": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    durable = False
    attempted = False
    try:
        if ledger.exists():
            existing = ledger.read_bytes()
            if existing and not existing.endswith(bytes([10])):
                raise OSError("ledger ends with an incomplete line")
        attempted = True
        with open(ledger, "ab") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_directory(directory)
        durable = True
        complete_lines = ledger.read_bytes().splitlines(keepends=True)
        if not complete_lines or not complete_lines[-1].endswith(bytes([10])):
            raise OSError("ledger verification failed")
        if json.loads(complete_lines[-1]) != row:
            raise OSError("ledger verification failed")
        return row
    except Exception as exc:
        raise AuditWriteError(attempted=attempted, durable=durable) from exc


def resume_operator(state_root: Path) -> dict[str, str]:
    """Resume validated stopped state while holding the bridge flock."""
    try:
        import fcntl
    except ImportError as exc:
        raise ResumeRefusedError("locking_unsupported") from exc

    root = Path(state_root).expanduser()
    if root.is_symlink():
        raise ResumeRefusedError("unsafe_state_root")
    lock_path = root / "bridge.lock"
    state_dir = root / "planner"
    state_path = state_dir / "no_plan_recovery.json"
    _reject_symlink_components(lock_path)
    _reject_symlink_components(state_path)
    try:
        lock_handle = open(lock_path, "a+")
    except OSError as exc:
        raise ResumeRefusedError("lock_unavailable") from exc
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ResumeRefusedError("lock_busy_or_unavailable") from exc

        try:
            raw = json.loads(
                state_path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON constant")),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise ResumeRefusedError("state_unavailable_or_invalid") from exc
        updated = _validate(raw)
        count = updated["consecutive_planner"]
        ledger_path, ledger_before = _preflight_ledger(root)
        updated["stopped"] = False
        _write_state(state_path, updated)
        try:
            _append_transition(root, count)
        except Exception as exc:
            audit = "unverified" if getattr(exc, "attempted", True) else "not_attempted"
            if getattr(exc, "durable", False):
                try:
                    appended = ledger_path.read_bytes()[len(ledger_before):].splitlines(keepends=True)
                    if appended and appended[-1].endswith(bytes([10])):
                        row = json.loads(appended[-1])
                        audit = "verified" if (
                            row.get("phase") == "no_plan_recovery"
                            and row.get("cycle_id") == "operator-resume"
                            and row.get("family") == "planner"
                            and row.get("consecutive_count") == count
                            and row.get("state") == "normal"
                        ) else "unverified"
                except Exception:
                    pass
            raise ResumeRefusedError("audit_unverified", result="partial", state="verified", audit=audit, durability="verified") from exc
        return {"result": "resumed", "state_status": "verified", "state_durability": "verified", "audit_status": "verified", "category": "ok"}
    finally:
        lock_handle.close()
