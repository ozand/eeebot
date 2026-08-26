"""Durable, deterministic per-cycle action index.

The prompt capture is large and short-lived.  This module reads the final
prompt record for each cycle, extracts tool calls, and writes a compact JSONL
summary before prompt rotation can remove the source.  It deliberately uses
only the standard library and is fail-open for host-timer use.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_RETENTION_DAYS = 90
_ARCHIVE_AFTER_DAYS = 7


def _day_from_name(name: str) -> str | None:
    stem = name[:-9] if name.endswith(".jsonl.gz") else name[:-6] if name.endswith(".jsonl") else ""
    try:
        datetime.strptime(stem, "%Y-%m-%d")
    except ValueError:
        return None
    return stem


def _iter_jsonl(path: Path, stats: dict[str, int] | None = None) -> Iterable[dict[str, Any]]:
    """Stream JSONL records without retaining large prompt files in memory."""
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[call-arg]
            for line in fh:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if stats is not None:
                        stats["skipped"] = stats.get("skipped", 0) + 1
                    continue
                if isinstance(value, dict):
                    yield value
                elif stats is not None:
                    stats["skipped"] = stats.get("skipped", 0) + 1
    except (OSError, EOFError, gzip.BadGzipFile):
        if stats is not None:
            stats["skipped"] = stats.get("skipped", 0) + 1


def _prompt_files(prompts_dir: Path) -> list[Path]:
    return sorted([*prompts_dir.glob("*.jsonl"), *prompts_dir.glob("*.jsonl.gz")])


def _ledger_rows(state_root: Path) -> list[dict[str, Any]]:
    ledger_dir = state_root / "ledger"
    paths = [ledger_dir / "cycles.jsonl", *sorted(ledger_dir.glob("cycles-*.jsonl.gz"))]
    rows: list[dict[str, Any]] = []
    for path in paths:
        if path.is_file():
            rows.extend(_iter_jsonl(path))
    return rows


def _ledger_by_cycle(state_root: Path) -> dict[str, dict[str, Any]]:
    titles: dict[str, str] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    for row in _ledger_rows(state_root):
        cycle_id = str(row.get("cycle_id") or "").strip()
        if not cycle_id:
            continue
        if row.get("phase") == "proposed" and row.get("task_title"):
            titles[cycle_id] = str(row["task_title"])
        if row.get("phase") == "outcome":
            outcomes[cycle_id] = row
    return {
        cycle_id: {
            "task_title": titles.get(cycle_id),
            "outcome": row.get("outcome"),
            "ts": row.get("ts"),
        }
        for cycle_id, row in outcomes.items()
    }


def _path_template(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.replace("\\", "/").strip()
    parts = [part for part in value.split("/") if part not in ("", ".", "..")]
    if not parts:
        return None
    name = parts[-1]
    suffix = Path(name).suffix
    if not suffix:
        return "/".join(parts[:-1] + ["*"]) if len(parts) > 1 else "*"
    return f"{parts[0]}/*{suffix}" if len(parts) > 1 else f"*{suffix}"


def _command_template(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    if not tokens:
        return None
    head = Path(tokens[0]).name
    # Keep the useful action for the common git command while still dropping
    # all arguments (the general contract is the executable head).
    if head == "git" and len(tokens) > 1 and not tokens[1].startswith("-"):
        head = f"git-{tokens[1]}"
    return head


def normalize_action(tool_name: Any, arguments: Any) -> str | None:
    """Return a compact ``tool:argument-shape`` template."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    name = tool_name.strip().lower()
    args = arguments if isinstance(arguments, dict) else {}
    if name in {"exec", "shell", "run_command", "execute"}:
        command = args.get("command", args.get("cmd"))
        template = _command_template(command)
        return f"exec:{template}" if template else "exec:*"
    prefix = {
        "read_file": "read", "read": "read", "write_file": "write", "write": "write",
        "edit_file": "edit", "edit": "edit", "list_dir": "list", "list": "list",
    }.get(name, name)
    value = next((args[key] for key in ("path", "file_path", "filename", "target_path") if key in args), None)
    template = _path_template(value)
    return f"{prefix}:{template}" if template else f"{prefix}:*"


def _tool_calls(record: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    for message in record.get("messages") or []:
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            name = function.get("name")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
            action = normalize_action(name, arguments)
            if action:
                actions.append(action)
    return actions


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("ACTION_INDEX_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return _DEFAULT_RETENTION_DAYS


def _rotate_and_prune(index_dir: Path, today: str) -> None:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=_retention_days())
    archive_cutoff = datetime.now(timezone.utc).date() - timedelta(days=_ARCHIVE_AFTER_DAYS)
    for path in index_dir.glob("*.jsonl"):
        day = _day_from_name(path.name)
        if not day:
            continue
        date = datetime.strptime(day, "%Y-%m-%d").date()
        if day != today and date <= archive_cutoff:
            try:
                with path.open("rb") as source, gzip.open(f"{path}.gz", "wb") as target:
                    target.write(source.read())
                path.unlink()
            except OSError:
                pass
    for path in index_dir.glob("*.jsonl.gz"):
        day = _day_from_name(path.name)
        if day:
            try:
                if datetime.strptime(day, "%Y-%m-%d").date() < cutoff:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass


def build_action_index(state_root: Path, prompts_dir: Path | None = None) -> dict[str, int]:
    """Extract present prompt files into the durable index; never raises."""
    summary = {"prompt_files": 0, "cycles": 0, "written": 0, "skipped": 0}
    try:
        prompts_dir = prompts_dir or state_root / "llm_calls" / "prompts"
        index_dir = state_root / "action_index"
        index_dir.mkdir(parents=True, exist_ok=True)
        existing: set[str] = set()
        for path in _prompt_files(index_dir):
            for row in _iter_jsonl(path):
                if row.get("cycle_id"):
                    existing.add(str(row["cycle_id"]))
        ledger = _ledger_by_cycle(state_root)
        grouped: dict[str, tuple[str, dict[str, Any]]] = {}
        for path in _prompt_files(prompts_dir):
            day = _day_from_name(path.name)
            if not day:
                continue
            summary["prompt_files"] += 1
            for record in _iter_jsonl(path, summary):
                cycle_id = str(record.get("cycle_id") or "").strip()
                if not cycle_id or not isinstance(record.get("messages"), list):
                    summary["skipped"] += 1
                    continue
                current = grouped.get(cycle_id)
                seq = record.get("seq") if isinstance(record.get("seq"), int) else -1
                old_seq = current[1].get("seq", -1) if current else -1
                if current is None or seq >= old_seq:
                    grouped[cycle_id] = (day, record)
        summary["cycles"] = len(grouped)
        for cycle_id, (day, record) in grouped.items():
            # A cycle is complete only once the ledger has a terminal row.
            # This prevents the prompt-record hook from indexing the first
            # call of a still-running cycle before later calls are captured.
            if cycle_id in existing or cycle_id not in ledger:
                continue
            row = ledger[cycle_id]
            output = {
                "cycle_id": cycle_id,
                "ts": record.get("ts") or row.get("ts") or "",
                "task_title": row.get("task_title"),
                "outcome": row.get("outcome"),
                "actions": _tool_calls(record),
            }
            output_path = index_dir / f"{day}.jsonl"
            with output_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
            existing.add(cycle_id)
            summary["written"] += 1
        _rotate_and_prune(index_dir, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    except Exception:
        pass
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the durable per-cycle action index")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--prompts-dir", type=Path, default=None)
    args = parser.parse_args()
    state_root = args.state_root or Path(os.environ.get("STATE_DIR", Path.home() / ".nanobot"))
    summary = build_action_index(state_root, args.prompts_dir)
    print("action-index: " + " ".join(f"{key}={value}" for key, value in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
