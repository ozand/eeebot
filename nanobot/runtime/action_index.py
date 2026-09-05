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
import posixpath
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_RETENTION_DAYS = 90
_ARCHIVE_AFTER_DAYS = 7

# ── #1348: action detail (resolution the miner can name a procedure from) ────
# ``actions`` keeps today's coarse templates (``exec:python3``,
# ``edit:scripts/*.py``) for every existing reader. ``actions_detail`` is a
# parallel list, same length and order, that additionally names the action:
# the argv head beyond an interpreter (script path or ``-m module``) and ONE
# concrete target path, or the concrete workspace-relative path of a
# read/edit/write. Everything else is dropped by construction — the scan stops
# at the first flag, so flag values are never seen; tokens must look like a
# workspace-relative path or a dotted module; env assignments, redirections,
# heredoc bodies, URLs and anything with ``= : @`` never qualify.
_DETAIL_TOKEN_CAP = 120          # chars per recorded token
_DETAIL_SCAN_TOKENS = 8          # argv positions inspected after the head
_DETAIL_MAX_TARGETS = 1          # concrete targets recorded per command
_INTERPRETER_RE = re.compile(r"^(python(?:[23](?:\.\d+)?)?|node|sh|bash|dash|zsh|perl|ruby)$")
_WRAPPERS = frozenset({"time", "nice", "sudo", "env"})
_WRAPPERS_WITH_ARG = frozenset({"timeout"})
_TARGET_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_TARGET_MODULE_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
_TARGET_SUFFIXES = frozenset({
    ".py", ".md", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".toml", ".sh",
    ".cfg", ".ini", ".js", ".ts", ".html", ".css", ".log", ".csv",
})
_SHELL_OPERATOR_CHARS = frozenset("|&;<>()`$")


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


def _known_workspace_roots(state_root: Path | None = None) -> tuple[str, ...]:
    roots: list[str] = []
    for value in (
        os.environ.get("TARGET_WORKSPACE", ""),
        os.environ.get("RELEASE_ROOT", ""),
        str(state_root.parent / "eeebot-self-evolving") if state_root else "",
        "/opt/eeepc-agent/runtimes/self-evolving-agent/current",
        # F4: also strip state_root so reads of state/*.json etc. become
        # "state/*.ext" templates rather than the legacy "var/*.ext" pattern
        # that resulted when the full /var/lib/... path was not recognized.
        str(state_root.parent) if state_root else "",
        str(state_root) if state_root else "",
    ):
        if value:
            root = posixpath.normpath(value.replace("\\", "/")).rstrip("/")
            if root and root not in roots:
                roots.append(root)
    return tuple(roots)


def _path_template(value: Any, workspace_roots: tuple[str, ...] = ()) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = posixpath.normpath(value.replace("\\", "/").strip())
    for root in workspace_roots:
        if value == root or value.startswith(root + "/"):
            value = value[len(root):].lstrip("/")
            break
    parts = [part for part in value.split("/") if part not in ("", ".", "..")]
    if not parts:
        return None
    name = parts[-1]
    suffix = Path(name).suffix
    if not suffix:
        return "/".join(parts[:-1] + ["*"]) if len(parts) > 1 else "*"
    return f"{parts[0]}/*{suffix}" if len(parts) > 1 else f"*{suffix}"


def _strip_shell_prefixes(command: str) -> str:
    """Drop leading env assignments and executor ``cd`` prefixes.

    For a compound command, the first meaningful command after those prefixes
    is indexed; shell syntax after it is intentionally out of scope.
    """
    command = command.strip()
    while command:
        command = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&]+\s+)+", "", command)
        match = re.match(r'^cd\s+(?:"[^"]*"|\'[^\']*\'|[^;&]+?)\s*(?:&&|;|$)', command)
        if not match:
            break
        command = command[match.end():].lstrip()
    return command


def _command_template(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = _strip_shell_prefixes(value)
    if not value:
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


def _split_command(value: Any) -> list[str]:
    """Tokens of the first command after env/cd prefixes and wrappers (``time``, ``timeout N``)."""
    if not isinstance(value, str) or not value.strip():
        return []
    value = _strip_shell_prefixes(value)
    if not value:
        return []
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    while tokens:
        head = Path(tokens[0]).name
        if head in _WRAPPERS:
            tokens = tokens[1:]
        elif head in _WRAPPERS_WITH_ARG and len(tokens) > 2:
            tokens = tokens[2:]
        else:
            break
    return tokens


def _target_like(token: str, workspace_roots: tuple[str, ...] = ()) -> str | None:
    """Return the recordable form of *token* — a workspace-relative path or a
    dotted module name — or None when it must not be recorded.

    Never returns a token containing ``= : @ + \\`` or whitespace (env values,
    URLs, credentials, base64), an absolute path outside the workspace roots,
    or anything longer than ``_DETAIL_TOKEN_CAP``.
    """
    if not isinstance(token, str) or not token or len(token) > _DETAIL_TOKEN_CAP:
        return None
    candidate = token
    path_like = "/" in candidate or "\\" in candidate
    if path_like:
        # Strip a workspace root BEFORE the character rule: on a Windows
        # checkout the root itself carries a drive colon.
        candidate = posixpath.normpath(candidate.replace("\\", "/"))
        for root in workspace_roots:
            if candidate == root or candidate.startswith(root + "/"):
                candidate = candidate[len(root):].lstrip("/")
                break
        if candidate.startswith("/") or candidate.startswith("..") or not candidate:
            return None
    if any(ch in candidate for ch in " \t=:@+\\") or any(ch in candidate for ch in _SHELL_OPERATOR_CHARS):
        return None
    if path_like:
        if not _TARGET_PATH_RE.match(candidate):
            return None
        return candidate
    if _TARGET_MODULE_RE.match(candidate) and Path(candidate).suffix not in _TARGET_SUFFIXES:
        return candidate  # dotted module: tests.test_agents_structure
    if _TARGET_PATH_RE.match(candidate) and Path(candidate).suffix in _TARGET_SUFFIXES:
        return candidate  # bare file name: setup.py, README.md
    return None


def _command_detail(value: Any, workspace_roots: tuple[str, ...] = ()) -> str | None:
    """``<head> [script|-m module] [target]`` — bounded, values-free (#1348).

    Head is the executable basename (``git-<sub>`` for git). For an
    interpreter the next token is kept only when it is a script path or
    ``-m <module>``; anything else (``-c``, ``-``, heredoc) ends the detail at
    the head. Then at most ``_DETAIL_MAX_TARGETS`` positional path-like tokens
    are recorded, scanning at most ``_DETAIL_SCAN_TOKENS`` positions and
    stopping at the first flag or shell operator — so a flag's value is
    never inspected, let alone recorded.
    """
    tokens = _split_command(value)
    if not tokens:
        return None
    head = Path(tokens[0]).name
    i = 1
    if head == "git" and len(tokens) > 1 and not tokens[1].startswith("-"):
        head = f"git-{tokens[1]}"
        i = 2
    parts = [head]
    if _INTERPRETER_RE.match(head):
        if i < len(tokens) and tokens[i] == "-m" and i + 1 < len(tokens):
            module = tokens[i + 1]
            if _TARGET_MODULE_RE.match(module) or re.match(r"^[A-Za-z_]\w*$", module):
                parts += ["-m", module[:_DETAIL_TOKEN_CAP]]
                i += 2
            else:
                return " ".join(parts)
        elif i < len(tokens) and not tokens[i].startswith("-"):
            script = _target_like(tokens[i], workspace_roots)
            if script is None:
                name = Path(tokens[i].replace("\\", "/")).name
                script = name if _TARGET_PATH_RE.match(name) and Path(name).suffix in _TARGET_SUFFIXES else None
            if script is None:
                return " ".join(parts)
            parts.append(script)
            i += 1
        else:
            return " ".join(parts)  # python3 -c ..., python3 - <<EOF, bare python3
    targets = 0
    for token in tokens[i:i + _DETAIL_SCAN_TOKENS]:
        if token.startswith("-") or any(ch in token for ch in _SHELL_OPERATOR_CHARS):
            break
        target = _target_like(token, workspace_roots)
        if target is not None:
            parts.append(target)
            targets += 1
            if targets >= _DETAIL_MAX_TARGETS:
                break
    return " ".join(parts)


def _path_detail(value: Any, workspace_roots: tuple[str, ...] = ()) -> str | None:
    """Concrete workspace-relative path for a read/edit/write, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _target_like(value.strip(), workspace_roots)


def normalize_action_detail(tool_name: Any, arguments: Any, workspace_roots: tuple[str, ...] = ()) -> str | None:
    """``tool:<detail>`` — same prefix as :func:`normalize_action`, higher resolution.

    Falls back to the coarse template when nothing recordable remains, so the
    detail list is always as long as the template list.
    """
    template = normalize_action(tool_name, arguments, workspace_roots)
    if template is None:
        return None
    name = str(tool_name).strip().lower()
    args = arguments if isinstance(arguments, dict) else {}
    prefix = template.split(":", 1)[0]
    if name in {"exec", "shell", "run_command", "execute"}:
        detail = _command_detail(args.get("command", args.get("cmd")), workspace_roots)
    else:
        value = next((args[key] for key in ("path", "file_path", "filename", "target_path") if key in args), None)
        detail = _path_detail(value, workspace_roots)
    return f"{prefix}:{detail}" if detail else template


def normalize_action(tool_name: Any, arguments: Any, workspace_roots: tuple[str, ...] = ()) -> str | None:
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
    template = _path_template(value, workspace_roots)
    return f"{prefix}:{template}" if template else f"{prefix}:*"


def _tool_calls(record: dict[str, Any], workspace_roots: tuple[str, ...] = ()) -> list[str]:
    return [template for template, _detail in _tool_call_pairs(record, workspace_roots)]


def _tool_call_pairs(record: dict[str, Any], workspace_roots: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """``(template, detail)`` per tool call, in order (#1348)."""
    actions: list[tuple[str, str]] = []
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
            action = normalize_action(name, arguments, workspace_roots)
            if action:
                detail = normalize_action_detail(name, arguments, workspace_roots) or action
                actions.append((action, detail))
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


def build_action_index(
    state_root: Path,
    prompts_dir: Path | None = None,
    *,
    max_days: int | None = None,
    force_regenerate: bool = False,
) -> dict[str, int]:
    """Extract present prompt files into the durable index; never raises.

    F4: when ``force_regenerate=True`` (or env ``ACTION_INDEX_FORCE_REGEN=1``)
    existing index day-files are deleted and rebuilt from source prompts that
    are still present, so stale ``var/*`` legacy templates are rewritten with
    the corrected path stripping (including state_root).  Only days whose
    source prompt file still exists are regenerated; day-files with no source
    are left intact to preserve history.

    #1059: skips historical prompt day files whose durable index file already
    exists (unless force_regenerate is True).  Only the newest/today file
    (and unindexed days) are parsed.  Accepts optional ``max_days`` to bound
    inspection window when called from synchronous hooks.
    """
    force_regenerate = force_regenerate or os.environ.get("ACTION_INDEX_FORCE_REGEN", "") == "1"
    summary = {
        "prompt_files": 0,
        "cycles": 0,
        "written": 0,
        "skipped_existing": 0,
        "skipped_incomplete": 0,
        "skipped_write_error": 0,
        "malformed_records": 0,
        "force_regenerated": 0,
    }
    try:
        prompts_dir = prompts_dir or state_root / "llm_calls" / "prompts"
        index_dir = state_root / "action_index"
        index_dir.mkdir(parents=True, exist_ok=True)

        prompt_files = _prompt_files(prompts_dir)
        if not prompt_files:
            return summary

        # F4: forced regeneration — delete existing day-files whose source
        # prompt day-file still exists so they are rebuilt with corrected
        # workspace_roots (state_root stripped → "state/*.ext" templates).
        if force_regenerate:
            prompt_days: set[str] = set()
            for path in prompt_files:
                day = _day_from_name(path.name)
                if day:
                    prompt_days.add(day)
            for path in list(_prompt_files(index_dir)):
                day = _day_from_name(path.name)
                if day and day in prompt_days:
                    try:
                        path.unlink(missing_ok=True)
                        summary["force_regenerated"] += 1
                    except OSError:
                        pass

        # Collect existing indexed cycles and indexed days
        existing: set[str] = set()
        indexed_days: set[str] = set()
        for path in _prompt_files(index_dir):
            day = _day_from_name(path.name)
            if day:
                indexed_days.add(day)
            for row in _iter_jsonl(path):
                if row.get("cycle_id"):
                    existing.add(str(row["cycle_id"]))

        # Filter prompt files by max_days if specified (from newest distinct calendar days)
        # Multiple files can exist for the same day (e.g. .jsonl and .jsonl.gz); group by day.
        valid_prompt_files = [p for p in prompt_files if _day_from_name(p.name)]
        if max_days is not None and max_days > 0:
            distinct_days = sorted({_day_from_name(p.name) for p in valid_prompt_files if _day_from_name(p.name)})
            selected_days = set(distinct_days[-max_days:])
            valid_prompt_files = [p for p in valid_prompt_files if _day_from_name(p.name) in selected_days]

        # Newest day in prompts can still gain cycles (e.g. today or latest active)
        newest_day = _day_from_name(valid_prompt_files[-1].name) if valid_prompt_files else None

        ledger = _ledger_by_cycle(state_root)
        workspace_roots = _known_workspace_roots(state_root)
        grouped: dict[str, tuple[str, int, list[tuple[str, str]]]] = {}
        for path in valid_prompt_files:
            day = _day_from_name(path.name)
            if not day:
                continue

            # #1059: Day-level skip before opening large/gzipped prompt files!
            # If the index for this day already exists and it's not the newest day
            # that could still receive new cycles, skip opening it completely.
            if not force_regenerate and day in indexed_days and day != newest_day:
                continue

            summary["prompt_files"] += 1
            file_stats = {"skipped": 0}
            for record in _iter_jsonl(path, file_stats):
                cycle_id = str(record.get("cycle_id") or "").strip()
                if not cycle_id or not isinstance(record.get("messages"), list):
                    file_stats["skipped"] += 1
                    continue
                current = grouped.get(cycle_id)
                seq = record.get("seq") if isinstance(record.get("seq"), int) else -1
                old_seq = current[1] if current else -1
                if current is None or seq >= old_seq:
                    grouped[cycle_id] = (day, seq, _tool_call_pairs(record, workspace_roots))
            summary["malformed_records"] += file_stats["skipped"]
        summary["cycles"] = len(grouped)
        for cycle_id, (day, _seq, pairs) in grouped.items():
            # A cycle is complete only once the ledger has a terminal row.
            # This prevents the prompt-record hook from indexing the first
            # call of a still-running cycle before later calls are captured.
            if cycle_id in existing:
                summary["skipped_existing"] += 1
                continue
            if cycle_id not in ledger:
                summary["skipped_incomplete"] += 1
                continue
            row = ledger[cycle_id]
            output = {
                "cycle_id": cycle_id,
                "ts": row.get("ts") or "",
                "task_title": row.get("task_title"),
                "outcome": row.get("outcome"),
                "actions": [template for template, _detail in pairs],
                # #1348: same length/order as ``actions``; readers without it
                # (rows written before this field) fall back to ``actions``.
                "actions_detail": [detail for _template, detail in pairs],
            }
            output_path = index_dir / f"{day}.jsonl"
            try:
                with output_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
            except OSError:
                summary["skipped_write_error"] += 1
                continue
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
    parser.add_argument(
        "--force-regen",
        action="store_true",
        default=False,
        help="F4: delete and rebuild existing index day-files whose source prompt file still exists",
    )
    args = parser.parse_args()
    state_root = args.state_root or Path(os.environ.get("STATE_DIR", Path.home() / ".nanobot"))
    summary = build_action_index(state_root, args.prompts_dir, force_regenerate=args.force_regen)
    print("action-index: " + " ".join(f"{key}={value}" for key, value in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
