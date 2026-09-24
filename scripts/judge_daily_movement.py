#!/usr/bin/env python3
"""Judge daily loop movement vs appearance of work vs stalled (#1855).

Evaluates the preceding 24-hour window from objective harness sidecars
via state_access and writes state/day_verdict/latest.json
and state/day_verdict/history.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from nanobot.runtime import state_access

PRODUCER = "scripts.judge_daily_movement"
SCHEMA_VERSION = "day-verdict-v1"

# Trivial / self-referential paths whose modification alone does not count
# as real movement:
TRIVIAL_PATHS = frozenset({
    "AGENTS.md",
    "memory/MEMORY.md",
    "memory/HISTORY.md",
})

TRIVIAL_DIR_PREFIXES = (
    "diary/",
    "memory/",
)


@dataclass(frozen=True)
class DailyVerdict:
    schema_version: str
    evaluated_at_utc: str
    window_start_utc: str
    window_end_utc: str
    verdict: str  # "movement" | "appearance" | "stalled" | "insufficient_data"
    total_attempts: int
    successful_cycles: int
    progressive_cycles: int
    appearance_cycles: int
    failed_cycles: int
    model_call_incomplete_cycles: int
    wasted_box_cycles: int
    productive_ratio: float
    reason: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_progressive_file(path: str) -> bool:
    """Return True if path represents code or functional surface."""
    p = str(path).strip()
    if not p:
        return False
    if p in TRIVIAL_PATHS:
        return False
    if any(p.startswith(prefix) for prefix in TRIVIAL_DIR_PREFIXES):
        return False
    return True


def is_progressive_cycle(files_changed: Sequence[str]) -> bool:
    """A cycle is progressive if at least one changed file is not trivial/metadata."""
    if not files_changed:
        return False
    return any(is_progressive_file(f) for f in files_changed)


def evaluate_daily_movement(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    evaluated_at: datetime | None = None,
) -> DailyVerdict:
    eval_now = evaluated_at or datetime.now(timezone.utc)
    outcomes = [
        r for r in rows
        if r.get("phase") == "outcome"
    ]

    run_ends = [r for r in rows if r.get("phase") == "run_end"]
    killed = [
        r for r in run_ends
        if str(r.get("classification") or "") == "unit_timeout"
        or (r.get("source") == "systemd" and str(r.get("outcome") or "") == "interrupted")
    ]
    unattributed_kills = [r for r in killed if not str(r.get("cycle_id") or "").strip()]
    total_attempts = len(outcomes) + len(killed)
    successes = [r for r in outcomes if str(r.get("outcome")).strip().lower() == "success"]
    successful_cycles = len(successes)

    progressive = [
        r for r in successes
        if is_progressive_cycle(r.get("files_changed") or ())
    ]
    progressive_cycles = len(progressive)
    appearance_cycles = successful_cycles - progressive_cycles

    failures = [
        r for r in outcomes
        if str(r.get("outcome")).strip().lower() == "failed"
    ]
    failed_cycles = len(failures)
    model_call_incomplete = [
        r for r in outcomes
        if str(r.get("outcome") or "").strip().lower() == "model_call_incomplete"
    ]
    model_call_incomplete_cycles = len(model_call_incomplete)

    # #1850: wasted box cycles (consumed >= 80% iterations without progressive delivery)
    wasted_box = 0
    for r in outcomes:
        frac = r.get("iteration_fraction")
        if isinstance(frac, (int, float)) and frac >= 0.8:
            if r not in progressive:
                wasted_box += 1
    # A systemd-killed invocation consumed the box but never reached a cycle
    # outcome; count it as wasted rather than letting the missing row look free.
    wasted_box += len(killed)
    wasted_box_cycles = wasted_box

    productive_ratio = (
        round(progressive_cycles / total_attempts, 4) if total_attempts > 0 else 0.0
    )

    if total_attempts == 0:
        verdict = "insufficient_data"
        reason = "no cycle attempts recorded in the 24-hour evaluation window"
    elif progressive_cycles == 0 and total_attempts >= 5:
        verdict = "stalled"
        reason = f"{total_attempts} attempts with 0 progressive code integrations"
    elif productive_ratio < 0.15 or (appearance_cycles > progressive_cycles and appearance_cycles >= 5):
        verdict = "appearance"
        reason = (
            f"predominantly metadata/appearance changes ({appearance_cycles} appearance vs "
            f"{progressive_cycles} progressive, productive_ratio={productive_ratio})"
        )
    else:
        verdict = "movement"
        reason = (
            f"demonstrated code/functional delivery ({progressive_cycles} progressive cycles, "
            f"productive_ratio={productive_ratio})"
        )
    if unattributed_kills:
        verdict = "incomplete"
        reason += f"; incomplete evidence: {len(unattributed_kills)} killed run(s) unattributed"

    return DailyVerdict(
        schema_version=SCHEMA_VERSION,
        evaluated_at_utc=eval_now.isoformat().replace("+00:00", "Z"),
        window_start_utc=window_start.isoformat().replace("+00:00", "Z"),
        window_end_utc=window_end.isoformat().replace("+00:00", "Z"),
        verdict=verdict,
        total_attempts=total_attempts,
        successful_cycles=successful_cycles,
        progressive_cycles=progressive_cycles,
        appearance_cycles=appearance_cycles,
        failed_cycles=failed_cycles,
        model_call_incomplete_cycles=model_call_incomplete_cycles,
        wasted_box_cycles=wasted_box_cycles,
        productive_ratio=productive_ratio,
        reason=reason,
        details={
            "trivial_paths": sorted(TRIVIAL_PATHS),
            "trivial_dir_prefixes": list(TRIVIAL_DIR_PREFIXES),
            "killed_by_timeout": len(killed),
            "unattributed_killed_by_timeout": len(unattributed_kills),
            "model_call_incomplete_cycles": model_call_incomplete_cycles,
            "killed_runs": [
                {"run_id": r.get("run_id"), "cycle_id": r.get("cycle_id") or None}
                for r in killed
            ],
        },
    )


def _write_verdict(state_dir: Path, verdict: DailyVerdict) -> None:
    """Atomic write to state/day_verdict/latest.json and append to history.jsonl."""
    out_dir = state_dir / "day_verdict"
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = verdict.to_dict()
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    latest_path = out_dir / "latest.json"
    tmp_path = out_dir / "latest.json.tmp"
    tmp_path.write_text(data, encoding="utf-8")
    os.replace(tmp_path, latest_path)

    history_path = out_dir / "history.jsonl"
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def read_daily_verdict(state_dir: Path) -> dict[str, Any] | None:
    """Read the latest daily verdict, distinguishing absent/corrupt from zero movement."""
    latest_path = state_dir / "day_verdict" / "latest.json"
    if not latest_path.exists():
        return None
    try:
        data = json.loads(latest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION:
            return data
        return None
    except Exception:
        return None


def run_judge(
    state_dir: Path,
    *,
    now: datetime | None = None,
    window_hours: int = 24,
) -> DailyVerdict:
    ref_now = now or datetime.now(timezone.utc)
    window_start = ref_now - timedelta(hours=window_hours)

    window = state_access.ledger_window(
        state_dir,
        since_ts=window_start.isoformat().replace("+00:00", "Z"),
        phases=frozenset({"outcome"}),
    )

    run_rows = state_access.run_window(
        state_dir,
        since_ts=window_start.isoformat().replace("+00:00", "Z"),
    )
    verdict = evaluate_daily_movement(
        (*window.rows, *run_rows.rows),
        window_start=window_start,
        window_end=ref_now,
        evaluated_at=ref_now,
    )

    _write_verdict(state_dir, verdict)
    return verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("NANOBOT_STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state")),
        help="Path to the runtime state directory",
    )
    parser.add_argument("--json", action="store_true", help="Print verdict JSON to stdout")
    args = parser.parse_args()

    verdict = run_judge(args.state_dir)
    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))
    else:
        print(f"Daily Verdict: {verdict.verdict.upper()} (progressive: {verdict.progressive_cycles}/{verdict.total_attempts}, reason: {verdict.reason})")


if __name__ == "__main__":
    main()
