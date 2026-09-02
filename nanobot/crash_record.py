"""Durable record of every bridge invocation exit (#1197).

On 2026-09-01 the bridge crash-looped for 9 h 20 min (140 consecutive failed
invocations, ``NameError`` at import, #1000/#1142) and nothing durable recorded
it: systemd's ``Result`` describes only the latest run, a process that dies at
import writes no ledger row, and the deploy gate waits on ledger activity.

This module is the one writer for that record and is deliberately **stdlib
only, with no package-internal imports**: it is armed from ``nanobot/__init__``
before ``nanobot.runtime.bridge`` is imported, so an import-time crash in the
bridge is still caught, and a recorder that could itself fail while recording
a crash would leave exactly the silence this exists to remove.

Records, under ``<state_dir>/bridge/``:

- ``exits.jsonl`` — one row per recorded exit (append-only).
- ``exit_streak.json`` — the countable state: ``consecutive_failures``, the
  first/last failure timestamps, the last error and exit status, the last
  success timestamp. A success resets the streak.

Two sources write the same record: ``source="process"`` from inside the
interpreter (``sys.excepthook`` for uncaught exceptions, the bridge's
``__main__`` guard for ordinary exit codes) and ``source="systemd"`` from the
unit's ``ExecStopPost=`` (this module's CLI), which also sees signals, OOM
kills and timeouts. A systemd record that lands within
:data:`MERGE_WINDOW_S` of a process record for the same outcome is merged
into it instead of counting a second failure.

Nothing here is a size cap and nothing falls back silently: a write failure
is printed to stderr (the journal under systemd) and raised to the caller.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# Mirrors the bridge's own default (nanobot/runtime/bridge.py: STATE_DIR) — kept
# as a literal here because this module must not import the bridge.
DEFAULT_STATE_DIR = "/var/lib/eeepc-agent/self-evolving-agent/state"
STATE_ENV_VARS = ("STATE_DIR", "NANOBOT_RUNTIME_STATE_ROOT")
# "1" arms the recorder regardless of argv (tests, ad-hoc runs); "0" disables it.
ENV_MARKER = "NANOBOT_BRIDGE_EXIT_RECORD"
BRIDGE_MODULE = "nanobot.runtime.bridge"
RECORDS_REL = Path("bridge") / "exits.jsonl"
STREAK_REL = Path("bridge") / "exit_streak.json"
STREAK_SCHEMA = "bridge-exit-streak-v1"
MERGE_WINDOW_S = 300.0
_armed = False


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """The bridge's state directory: ``STATE_DIR`` (the unit's env file), else
    ``NANOBOT_RUNTIME_STATE_ROOT`` (the unit's drop-in), else the host default."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    for name in STATE_ENV_VARS:
        value = str(env_map.get(name) or "").strip()
        if value:
            return Path(value)
    return Path(DEFAULT_STATE_DIR)


def is_bridge_invocation(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> bool:
    """True only for ``python -m nanobot.runtime.bridge`` (read from
    ``sys.orig_argv``, which is set before any import runs — ``sys.argv[0]`` is
    still ``-m`` at that point) or when :data:`ENV_MARKER` is ``1``. Every
    other entry point (CLI, tests, other ``-m`` modules) leaves the recorder
    inert."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    marker = str(env_map.get(ENV_MARKER) or "").strip().lower()
    if marker in {"0", "false", "no", "off"}:
        return False
    if marker in {"1", "true", "yes", "on"}:
        return True
    argv = list(getattr(sys, "orig_argv", []) if argv is None else argv)
    for index, token in enumerate(argv[:-1]):
        if token == "-m" and argv[index + 1] == BRIDGE_MODULE:
            return True
    return False


def first_traceback_line(exc_type: type[BaseException], exc: BaseException, tb: Any) -> tuple[str, str]:
    """``("NameError: name '_x' is not defined", "nanobot/runtime/bridge.py:4987")``
    — the error line and the innermost frame, the two things a person greps
    the journal for."""
    error = "".join(traceback.format_exception_only(exc_type, exc)).strip().splitlines()
    where = ""
    frames = traceback.extract_tb(tb) if tb is not None else []
    if frames:
        last = frames[-1]
        where = f"{last.filename}:{last.lineno}"
    return (error[0] if error else exc_type.__name__), where


def _load_streak(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # Corrupt streak state is reported, then rebuilt from this record — the
        # append-only exits.jsonl keeps the history.
        print(f"bridge-exit-record: streak file unreadable ({exc!r}); rebuilding", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def record_exit(
    root: str | Path,
    *,
    outcome: str,
    exit_status: Any,
    source: str = "process",
    error: str = "",
    where: str = "",
    service_result: str = "",
    exit_code: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one exit row and update the streak; return the new streak state.

    ``outcome`` is ``"success"`` or ``"failure"``. Raises on a write failure
    after printing what could not be written — never a silent fallback.
    """
    if outcome not in ("success", "failure"):
        raise ValueError(f"outcome must be success|failure, got {outcome!r}")
    root = Path(root)
    records_path, streak_path = root / RECORDS_REL, root / STREAK_REL
    stamp = _now_iso(now)
    row = {
        "ts": stamp, "source": source, "outcome": outcome, "exit_status": exit_status,
        "service_result": service_result, "exit_code": exit_code, "error": error, "where": where,
        "pid": os.getpid(), "argv": list(getattr(sys, "orig_argv", sys.argv))[:6],
    }
    try:
        records_path.parent.mkdir(parents=True, exist_ok=True)
        streak = _load_streak(streak_path)
        last_ts = _parse_iso(streak.get("updated_at"))
        merged = (
            source == "systemd"
            and streak.get("last_source") == "process"
            and streak.get("last_outcome") == outcome
            and last_ts is not None
            and 0 <= ((_parse_iso(stamp) or datetime.now(timezone.utc)) - last_ts).total_seconds() <= MERGE_WINDOW_S
        )
        row["merged_with_previous"] = merged
        with records_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        streak.setdefault("schema_version", STREAK_SCHEMA)
        streak["total_records"] = int(streak.get("total_records") or 0) + 1
        if not merged:
            if outcome == "failure":
                streak["consecutive_failures"] = int(streak.get("consecutive_failures") or 0) + 1
                streak["total_failures"] = int(streak.get("total_failures") or 0) + 1
                if streak["consecutive_failures"] == 1:
                    streak["first_failure_ts"] = stamp
                streak["last_failure_ts"] = stamp
                streak["last_exit_status"] = exit_status
                streak["last_error"] = error
                streak["last_where"] = where
            else:
                streak["consecutive_failures"] = 0
                streak["last_success_ts"] = stamp
                streak.pop("first_failure_ts", None)
        if service_result:
            streak["last_service_result"] = service_result
        if exit_code:
            streak["last_exit_code"] = exit_code
        streak["last_source"] = source
        streak["last_outcome"] = outcome
        streak["updated_at"] = stamp
        _write_atomic(streak_path, streak)
        return streak
    except OSError as exc:
        print(f"bridge-exit-record: FAILED to write {records_path} / {streak_path}: {exc!r}; row={json.dumps(row)}", file=sys.stderr)
        raise


def _excepthook_recording(previous: Any, root: Path) -> Any:
    def hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        error, where = first_traceback_line(exc_type, exc, tb)
        status = 130 if issubclass(exc_type, KeyboardInterrupt) else 1
        try:
            record_exit(root, outcome="failure", exit_status=status, error=error, where=where)
        except Exception as record_exc:  # the original crash must still be shown
            print(f"bridge-exit-record: recorder failed: {record_exc!r}", file=sys.stderr)
        previous(exc_type, exc, tb)
    return hook


def arm(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> bool:
    """Install the recording ``sys.excepthook`` if this process is the bridge.
    Returns whether it armed. Idempotent."""
    global _armed
    if _armed or not is_bridge_invocation(argv, env):
        return _armed
    sys.excepthook = _excepthook_recording(sys.excepthook, state_dir(env))
    _armed = True
    return True


def main(argv: list[str] | None = None) -> int:
    """``ExecStopPost=`` entry point: ``python -m nanobot.crash_record --source
    systemd --exit-code ${EXIT_CODE} --exit-status ${EXIT_STATUS}
    --service-result ${SERVICE_RESULT}``. Outcome is ``success`` iff
    ``SERVICE_RESULT`` is ``success``; ``exit-status`` may be a number or a
    signal name. Exit 0 on a written record, 2 when it could not be written."""
    parser = argparse.ArgumentParser(description="Record one bridge invocation exit (#1197)")
    parser.add_argument("--source", default="systemd")
    parser.add_argument("--exit-status", default="")
    parser.add_argument("--exit-code", default="")
    parser.add_argument("--service-result", default="")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--error", default="")
    args = parser.parse_args(argv)
    root = Path(args.state_dir) if args.state_dir else state_dir()
    outcome = "success" if args.service_result == "success" else "failure"
    status: Any = args.exit_status
    if str(status).isdigit():
        status = int(status)
    try:
        streak = record_exit(root, outcome=outcome, exit_status=status, source=args.source,
                             service_result=args.service_result, exit_code=args.exit_code, error=args.error)
    except OSError:
        return 2
    print(json.dumps({"outcome": outcome, "consecutive_failures": streak.get("consecutive_failures", 0)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
