#!/usr/bin/env python3
"""
llm_prompt_inspect.py — inspect the prompts/*.jsonl(.gz) captures (issue #693).

Reads the daily-rotated (and gzip-archived) JSONL files written by
``nanobot.observability.llm_telemetry.record_llm_prompt`` (one line per LLM
call through ``chat_with_retry``, full assembled ``messages`` + response) and
prints a per-message breakdown — role, byte size, an approximate token count
(``len(text) // 4``), and a truncated preview — so it's possible to see what
actually dominates a call's context (e.g. the ~14k unaccounted prompt tokens
noted in issue #675/#693).

Usage:
    python3 scripts/llm_prompt_inspect.py [--dir PATH] [--cycle CYCLE_ID]
                                           [--date YYYY-MM-DD] [--call SEQ]
                                           [--json]

Defaults: --dir is $LLM_CALLS_DIR/prompts, else $STATE_DIR/llm_calls/prompts,
else ~/.nanobot/llm_calls/prompts (same resolution order as the writer, plus
the "prompts" subdirectory).

Selecting calls:
- ``--date`` restricts to a single day's file (plain or ``.gz``); omit to
  scan all available files.
- ``--cycle`` restricts to a single cycle_id.
- ``--call`` restricts to a single sequence number (requires --cycle or a
  result set that already narrows to one cycle, otherwise the first match
  per cycle is used).
Without ``--call``, all matching calls are listed with a one-line summary;
with ``--call``, the full per-message breakdown for that call is printed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any


def _default_dir() -> Path:
    env_dir = os.environ.get("LLM_CALLS_DIR", "").strip()
    if env_dir:
        return Path(env_dir) / "prompts"
    state_dir = os.environ.get("STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir) / "llm_calls" / "prompts"
    return Path.home() / ".nanobot" / "llm_calls" / "prompts"


def _iter_files(directory: Path, date: str | None) -> list[Path]:
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.jsonl")) + sorted(directory.glob("*.jsonl.gz"))
    if date:
        files = [f for f in files if f.name.startswith(date)]
    return files


def load_records(directory: Path, date: str | None = None) -> list[dict[str, Any]]:
    """Load and parse all prompt-capture records under *directory*."""
    records: list[dict[str, Any]] = []
    for path in _iter_files(directory, date):
        try:
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[call-arg]
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return records


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if content is None:
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            return json.dumps(tool_calls, ensure_ascii=False)
        return ""
    return json.dumps(content, ensure_ascii=False)


def _est_tokens(text: str) -> int:
    return len(text) // 4


def summarize_call(record: dict[str, Any], preview_chars: int = 80) -> dict[str, Any]:
    """Return a per-message breakdown (role, bytes, est. tokens, preview) + totals."""
    messages = record.get("messages") or []
    breakdown = []
    total_bytes = 0
    total_tokens = 0
    for idx, msg in enumerate(messages):
        text = _message_text(msg)
        size = len(text.encode("utf-8", errors="replace"))
        tokens = _est_tokens(text)
        total_bytes += size
        total_tokens += tokens
        preview = text[:preview_chars].replace("\n", " ")
        if len(text) > preview_chars:
            preview += "..."
        breakdown.append(
            {
                "index": idx,
                "role": msg.get("role", ""),
                "bytes": size,
                "est_tokens": tokens,
                "preview": preview,
            }
        )
    response_text = (record.get("content") or "") + (record.get("reasoning_content") or "")
    return {
        "cycle_id": record.get("cycle_id", ""),
        "component": record.get("component", ""),
        "seq": record.get("seq"),
        "model": record.get("model", ""),
        "ts": record.get("ts", ""),
        "finish_reason": record.get("finish_reason", ""),
        "prompt_tokens": record.get("prompt_tokens", 0),
        "completion_tokens": record.get("completion_tokens", 0),
        "messages": breakdown,
        "message_count": len(messages),
        "total_message_bytes": total_bytes,
        "total_message_est_tokens": total_tokens,
        "response_bytes": len(response_text.encode("utf-8", errors="replace")),
        "response_est_tokens": _est_tokens(response_text),
    }


def select_calls(
    records: list[dict[str, Any]],
    cycle: str | None,
    call: int | None,
) -> list[dict[str, Any]]:
    selected = records
    if cycle:
        selected = [r for r in selected if r.get("cycle_id") == cycle]
    if call is not None:
        selected = [r for r in selected if r.get("seq") == call]
    return selected


def _print_summary_line(record: dict[str, Any]) -> None:
    print(
        f"{record.get('ts', '')}  cycle={record.get('cycle_id') or '-':<20} "
        f"component={record.get('component') or '-':<12} seq={record.get('seq')!s:<4} "
        f"model={record.get('model', ''):<24} "
        f"prompt_tokens={record.get('prompt_tokens', 0):<7} "
        f"completion_tokens={record.get('completion_tokens', 0)}"
    )


def _print_call_detail(summary: dict[str, Any]) -> None:
    print(f"cycle_id={summary['cycle_id']} component={summary['component']} seq={summary['seq']}")
    print(f"model={summary['model']} ts={summary['ts']} finish_reason={summary['finish_reason']}")
    print(
        f"reported prompt_tokens={summary['prompt_tokens']} "
        f"completion_tokens={summary['completion_tokens']}"
    )
    print()
    print(f"{'idx':>4}  {'role':<10} {'bytes':>8} {'~tokens':>8}  preview")
    for msg in summary["messages"]:
        print(f"{msg['index']:>4}  {msg['role']:<10} {msg['bytes']:>8} {msg['est_tokens']:>8}  {msg['preview']}")
    print()
    print(
        f"TOTAL messages={summary['message_count']} bytes={summary['total_message_bytes']} "
        f"~tokens={summary['total_message_est_tokens']}"
    )
    print(f"response bytes={summary['response_bytes']} ~tokens={summary['response_est_tokens']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=None, help="prompts directory (default: resolved like the writer)")
    parser.add_argument("--cycle", default=None, help="filter to a single cycle_id")
    parser.add_argument("--date", default=None, help="filter to a single day (YYYY-MM-DD)")
    parser.add_argument("--call", type=int, default=None, help="show full per-message breakdown for this seq")
    parser.add_argument("--json", action="store_true", help="emit raw JSON instead of a human table")
    args = parser.parse_args(argv)

    directory = args.dir or _default_dir()
    records = load_records(directory, args.date)
    selected = select_calls(records, args.cycle, args.call)

    if not selected:
        if args.json:
            print(json.dumps([]))
        else:
            print(f"No matching prompt captures found under {directory}", file=sys.stderr)
        return 0

    if args.call is not None:
        summaries = [summarize_call(r) for r in selected]
        if args.json:
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
        else:
            for summary in summaries:
                _print_call_detail(summary)
                print()
        return 0

    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        for record in selected:
            _print_summary_line(record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
