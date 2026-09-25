"""Session timing, progress watchdog, and wall-clock safety margins (#1899).

Addresses unit timeout kills (TimeoutStartSec=3300s):
1. Wall Clock Safety Margin: stops the subagent before starting a new model call
   if the remaining wall-clock budget is less than p99 call duration + final/gate budget.
   Configured via env:
   - NANOBOT_WALL_CALL_P99_SECS: default 243.0s (from #1899 empirical measurement on un/qwen3.8-27b-gguf).
   - NANOBOT_WALL_FINAL_BUDGET_SECS: default 300.0s (budget for finalization, smoke, repair, gate).
2. Progress Watchdog: stops the subagent if N minutes pass without any completed
   model call or tool step.
   Configured via env:
   - NANOBOT_PROGRESS_TIMEOUT_SECS (or NANOBOT_PROGRESS_TIMEOUT_MINUTES): default 600.0s (10 min).
3. Max Call Gap Tracker: computes the maximum gap between completed LLM calls in a cycle,
   recorded in cycle_ledger so close misses to the progress watchdog are visible.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

# Defaults derived from empirical measurements in #1899
DEFAULT_CALL_P99_SECS: float = 243.0
DEFAULT_FINAL_BUDGET_SECS: float = 300.0
DEFAULT_PROGRESS_TIMEOUT_SECS: float = 600.0  # 10 minutes
DEFAULT_WALL_SECS: float = 3000.0  # 50 minutes


def get_call_p99_secs() -> float:
    """Return p99 single-call duration budget in seconds (#1899)."""
    raw = os.environ.get("NANOBOT_WALL_CALL_P99_SECS", "").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return DEFAULT_CALL_P99_SECS


def get_final_budget_secs() -> float:
    """Return budget reserved for finalization, smoke tests, and gate (#1899)."""
    raw = os.environ.get("NANOBOT_WALL_FINAL_BUDGET_SECS", "").strip()
    if raw:
        try:
            val = float(raw)
            if val >= 0:
                return val
        except (ValueError, TypeError):
            pass
    return DEFAULT_FINAL_BUDGET_SECS


def get_wall_safety_margin_secs() -> float:
    """Total margin required before starting a new model call (#1899)."""
    return get_call_p99_secs() + get_final_budget_secs()


def get_progress_timeout_secs() -> float:
    """Return timeout for absence of progress in seconds (#1899)."""
    raw_s = os.environ.get("NANOBOT_PROGRESS_TIMEOUT_SECS", "").strip()
    if raw_s:
        try:
            val = float(raw_s)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    raw_m = os.environ.get("NANOBOT_PROGRESS_TIMEOUT_MINUTES", "").strip()
    if raw_m:
        try:
            val = float(raw_m)
            if val > 0:
                return val * 60.0
        except (ValueError, TypeError):
            pass
    return DEFAULT_PROGRESS_TIMEOUT_SECS


def get_bridge_wall_secs() -> float:
    """Return total bridge wall clock allocation in seconds (#1899)."""
    raw = os.environ.get("NANOBOT_SUBAGENT_WALL_SECS", "").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return DEFAULT_WALL_SECS


def should_stop_for_wall_clock(
    wall_deadline: float | None,
    *,
    clock: Callable[[], float] | None = None,
) -> bool:
    """Return True iff remaining wall clock is less than the safety margin (#1899)."""
    if wall_deadline is None:
        return False
    c = clock or time.monotonic
    remaining = wall_deadline - c()
    return remaining < get_wall_safety_margin_secs()


class ProgressWatchdog:
    """Watchdog tracking progress during subagent execution (#1899).

    Triggers when no model call and no tool step has completed for N minutes
    (default 10 minutes = 600s, configured via NANOBOT_PROGRESS_TIMEOUT_SECS).
    """

    def __init__(
        self,
        timeout_secs: float | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ):
        self._clock = clock or time.monotonic
        self.timeout_secs = (
            timeout_secs if timeout_secs is not None else get_progress_timeout_secs()
        )
        self._last_progress = self._clock()
        self._last_call_completed: float | None = None
        self.max_call_gap_s: float | None = None

    def record_step_completed(self) -> None:
        """Record completion of a tool step."""
        self._last_progress = self._clock()

    def record_call_completed(self) -> None:
        """Record completion of an LLM model call and update max_call_gap_s."""
        now = self._clock()
        self._last_progress = now
        if self._last_call_completed is not None:
            gap = now - self._last_call_completed
            if self.max_call_gap_s is None or gap > self.max_call_gap_s:
                self.max_call_gap_s = gap
        self._last_call_completed = now

    def is_stalled(self) -> bool:
        """Return True iff no progress has completed for timeout_secs."""
        return (self._clock() - self._last_progress) >= self.timeout_secs

    def remaining_time(self) -> float:
        """Remaining seconds before the progress timeout expires."""
        elapsed = self._clock() - self._last_progress
        return max(0.0, self.timeout_secs - elapsed)


def compute_cycle_max_call_gap(
    state_dir: Path | str | None,
    cycle_id: str | None,
    *,
    since_ts: str | None = None,
    fallback_gap: float | None = None,
) -> float | None:
    """Compute the maximum gap (in seconds) between consecutive completed LLM calls (#1899).

    Scans the newest files in state_dir/llm_calls for rows matching cycle_id.
    If fewer than 2 matching calls are found, returns fallback_gap.
    """
    if not state_dir or not cycle_id:
        return round(fallback_gap, 1) if fallback_gap is not None else None

    try:
        llm_dir = Path(state_dir) / "llm_calls"
        if not llm_dir.is_dir():
            return round(fallback_gap, 1) if fallback_gap is not None else None

        candidates = sorted(llm_dir.glob("*.jsonl"))
        if not candidates:
            return round(fallback_gap, 1) if fallback_gap is not None else None

        calls_ts: list[datetime] = []
        for path in candidates[-2:]:
            try:
                with path.open("rt", encoding="utf-8") as f:
                    for line in f:
                        if cycle_id not in line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        if record.get("cycle_id") != cycle_id:
                            continue
                        ts_str = record.get("ts")
                        if not ts_str:
                            continue
                        if since_ts and ts_str < since_ts:
                            continue
                        try:
                            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            calls_ts.append(dt)
                        except Exception:
                            continue
            except Exception:
                continue

        calls_ts.sort()
        if len(calls_ts) >= 2:
            gaps = [
                (calls_ts[i] - calls_ts[i - 1]).total_seconds()
                for i in range(1, len(calls_ts))
            ]
            computed = max(gaps)
            if fallback_gap is not None:
                computed = max(computed, fallback_gap)
            return round(computed, 1)

        return round(fallback_gap, 1) if fallback_gap is not None else None
    except Exception:
        return round(fallback_gap, 1) if fallback_gap is not None else None
