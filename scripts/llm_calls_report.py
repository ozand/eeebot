#!/usr/bin/env python3
"""
llm_calls_report.py — summarize the llm_calls/*.jsonl telemetry (issue #675).

Reads the daily-rotated JSONL files written by
``nanobot.observability.llm_telemetry.record_llm_call`` (one line per LLM
call through ``chat_with_retry``) and aggregates:

- per-model: count, avg/p50/p95 duration_ms, total tokens
- per-cycle: total LLM wall-time (sum of duration_ms) — a utilization proxy
  for how much of a self-evolving cycle is spent waiting on the LLM
- overall totals

Usage:
    python3 scripts/llm_calls_report.py [--dir PATH] [--since YYYY-MM-DD] [--json]

Defaults: --dir is $LLM_CALLS_DIR, else $STATE_DIR/llm_calls, else
~/.nanobot/llm_calls (same resolution order as the writer).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _default_dir() -> Path:
    env_dir = os.environ.get("LLM_CALLS_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    state_dir = os.environ.get("STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir) / "llm_calls"
    return Path.home() / ".nanobot" / "llm_calls"


def load_records(directory: Path, since: str | None = None) -> list[dict[str, Any]]:
    """Load and parse all JSONL records under *directory*, optionally filtered by day."""
    records: list[dict[str, Any]] = []
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*.jsonl")):
        if since and path.stem < since:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
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


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate records into per-model, per-cycle, and overall summaries."""
    per_model: dict[str, dict[str, Any]] = {}
    per_cycle: dict[str, float] = {}
    total_calls = 0
    total_duration_ms = 0.0
    total_tokens = 0

    for rec in records:
        model = rec.get("model") or "(unknown)"
        duration_ms = float(rec.get("duration_ms") or 0.0)
        tokens = int(rec.get("total_tokens") or 0)
        cycle_id = rec.get("cycle_id") or ""

        bucket = per_model.setdefault(model, {"count": 0, "durations": [], "total_tokens": 0})
        bucket["count"] += 1
        bucket["durations"].append(duration_ms)
        bucket["total_tokens"] += tokens

        if cycle_id:
            per_cycle[cycle_id] = per_cycle.get(cycle_id, 0.0) + duration_ms

        total_calls += 1
        total_duration_ms += duration_ms
        total_tokens += tokens

    per_model_summary = {}
    for model, bucket in per_model.items():
        durations = sorted(bucket["durations"])
        count = bucket["count"]
        per_model_summary[model] = {
            "count": count,
            "avg_duration_ms": round(sum(durations) / count, 2) if count else 0.0,
            "p50_duration_ms": round(_percentile(durations, 0.50), 2),
            "p95_duration_ms": round(_percentile(durations, 0.95), 2),
            "total_tokens": bucket["total_tokens"],
        }

    per_cycle_summary = {
        cycle_id: round(total, 2)
        for cycle_id, total in sorted(per_cycle.items(), key=lambda kv: kv[1], reverse=True)
    }

    return {
        "totals": {
            "calls": total_calls,
            "duration_ms": round(total_duration_ms, 2),
            "tokens": total_tokens,
        },
        "per_model": per_model_summary,
        "per_cycle_duration_ms": per_cycle_summary,
    }


def _print_table(summary: dict[str, Any]) -> None:
    totals = summary["totals"]
    print(f"Total calls: {totals['calls']}  "
          f"Total LLM wall-time: {totals['duration_ms']:.0f}ms  "
          f"Total tokens: {totals['tokens']}")
    print()

    print("Per model:")
    print(f"  {'model':<30} {'count':>6} {'avg_ms':>10} {'p50_ms':>10} {'p95_ms':>10} {'tokens':>10}")
    for model, stats in sorted(summary["per_model"].items(), key=lambda kv: kv[1]["count"], reverse=True):
        print(f"  {model:<30} {stats['count']:>6} {stats['avg_duration_ms']:>10.1f} "
              f"{stats['p50_duration_ms']:>10.1f} {stats['p95_duration_ms']:>10.1f} {stats['total_tokens']:>10}")
    print()

    print("Per cycle (LLM wall-time, utilization proxy):")
    if not summary["per_cycle_duration_ms"]:
        print("  (no cycle_id attribution in this data)")
    else:
        for cycle_id, duration_ms in summary["per_cycle_duration_ms"].items():
            print(f"  {cycle_id:<30} {duration_ms:>10.0f}ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=None, help="llm_calls directory (default: env-resolved)")
    parser.add_argument("--since", type=str, default=None, help="only include files from YYYY-MM-DD onward")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
    args = parser.parse_args(argv)

    directory = args.dir or _default_dir()
    records = load_records(directory, since=args.since)
    summary = aggregate(records)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_table(summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
