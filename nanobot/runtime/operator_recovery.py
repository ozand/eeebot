"""Fail-closed operator resume for stopped no-plan recovery state."""
from __future__ import annotations

import json
import os
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


def _append_transition(state_root: Path, count: int) -> bytes:
    from datetime import datetime, timezone

    from nanobot.runtime.cycle_ledger import _LEDGER_FILENAME, _ledger_dir

    directory = _ledger_dir(state_root)
    created = not directory.exists()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if created:
            _fsync_directory(directory.parent)
    except Exception as exc:
        raise AuditWriteError(attempted=False, durable=False) from exc
    ledger = directory / _LEDGER_FILENAME
    row = {
        "phase": "no_plan_recovery",
        "cycle_id": "operator-resume",
        "family": "planner",
        "consecutive_count": count,
        "state": "normal",
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    durable = False
    attempted = False
    try:
        attempted = True
        with open(ledger, "ab") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_directory(directory)
        durable = True
        if payload.rstrip(b"\n") not in ledger.read_bytes().splitlines():
            raise OSError("ledger verification failed")
        return payload
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
    if lock_path.is_symlink() or state_dir.is_symlink() or state_path.is_symlink():
        raise ResumeRefusedError("unsafe_state_path")
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
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ResumeRefusedError("state_unavailable_or_invalid") from exc
        updated = _validate(raw)
        count = updated["consecutive_planner"]
        updated["stopped"] = False
        _write_state(state_path, updated)
        from nanobot.runtime.cycle_ledger import _LEDGER_FILENAME, _ledger_dir

        ledger_path = _ledger_dir(root) / _LEDGER_FILENAME
        try:
            ledger_before = ledger_path.read_bytes() if ledger_path.exists() else b""
        except OSError:
            ledger_before = None
        try:
            _append_transition(root, count)
        except Exception as exc:
            audit = "unverified" if getattr(exc, "attempted", True) else "not_attempted"
            if getattr(exc, "durable", False) and ledger_before is not None:
                try:
                    appended = ledger_path.read_bytes()[len(ledger_before):].splitlines()
                    audit = "verified" if any(
                        (row := json.loads(line)).get("phase") == "no_plan_recovery"
                        and row.get("cycle_id") == "operator-resume"
                        and row.get("family") == "planner"
                        and row.get("consecutive_count") == count
                        and row.get("state") == "normal" for line in appended
                    ) else "unverified"
                except Exception:
                    pass
            raise ResumeRefusedError("audit_unverified", result="partial", state="verified", audit=audit, durability="verified") from exc
        return {"result": "resumed", "state_status": "verified", "state_durability": "verified", "audit_status": "verified", "category": "ok"}
    finally:
        lock_handle.close()
