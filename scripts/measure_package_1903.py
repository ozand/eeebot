#!/usr/bin/env python3
"""Offline measurement instrument for ADR-031 rule 7 / issue #1903.

Runs OUTSIDE the bridge cycle path -- read-only against a runtime state
directory. Computes, per ``cycle_id`` (the unit of account, keyed on each
cycle's EARLIEST recorded ``phase: started`` row -- retries of the same
``cycle_id`` never re-admit it if that earliest start falls outside the
requested window):

  (a) executor calls  -- count of ``component: executor`` rows in
      ``state/llm_calls/*.jsonl``, summed across every run (retry) of the
      cycle_id.
  (b) confirmed result -- the cycle's LAST ``phase: outcome`` ledger row
      has ``outcome: success`` AND ``verdict: accept``. Per
      ``cycle_ledger.record_cycle_outcome``'s own docstring ("must be
      called in the SAME step that ... performs the merge"), ``success``
      already means the commit reached ``origin/main`` -- no separate
      ``completed.json`` cross-check is ANDed in. That was the
      pre-registration's literal wording, but ANDing a completed.json
      cross-check (files_changed non-empty) does NOT reproduce the
      recorded baselines: measured on the host it gives 16/42 and 11/24,
      not the recorded 20/42 and 14/24. ``completed.json`` records
      DEMAND-backlog fulfillment, a strict subset of successful
      integrations -- self-directed or non-demand-linked successes never
      get an entry there at all, so ANDing it under-counts. ``demand_linked``
      below reports the completed.json cross-reference anyway, as a
      separate informational column, never folded into (b).
  (b, rule C) -- amendment recorded in #1903 on 2026-09-25, before the
      window closed and before (b) was computed: a (b) cycle is NOT a
      confirmed result when every file its branch changed
      (``git diff --name-only <merge>^1 <merge>^2`` of its
      ``merge: integrate selfevo/cycle-<id>`` commit on the instance
      repo's first-parent history) is a service path -- see
      :data:`SERVICE_PATH_PREFIXES` / :data:`SERVICE_PATH_EXACT`. Commit
      subjects, trailers and bodies are not used. Excluded cycles stay in
      the denominator. Needs ``--repo``; without it rule C is reported as
      not applied rather than silently equal to (b).
  usage-confirmed -- a ``completed.json`` entry for the same cycle_id has
      ``confirmed: true`` -- a separate, stricter, mix-sensitive signal;
      never substituted for (b).
  planning_session_outcome -- the cycle's LAST ``phase: planning_session``
      ledger row's ``outcome`` (``integrated``/``no_plan``/``malformed``/
      ``timed_out``/``refused``/...), or ``None`` if the cycle predates the
      planning-session instrument (e.g. every cycle on 2026-09-21).
  cause -- the cycle's LAST ``phase: outcome`` row's ``outcome`` value (the
      named reason a cycle did or did not complete), or
      ``no_terminal_row`` if the cycle_id never got one.

Runs per cycle_id and total wall time of the cycle's own model calls (any
component, every run) are also reported -- the "queue + generation, not
separable" covariate from the pre-registration, not a cost.

Selection: exactly one of
  --start/--end (UTC, ISO-8601, --end optional -> open-ended)
  --after T --first-n N  (first N cycle_ids strictly after T, ordered by
      --order-by)

--order-by {first_start, first_executor_call} (default first_start):
  first_start is the pre-registration's own definition ("started" =
  the cycle's first `phase: started` row) and is what any FORWARD
  measurement should use. first_executor_call exists only to reproduce
  ADR-031's historical "first 24 cycles of 2026-09-21" exactly -- the
  write-ahead `started` row and the cycle's actual first executor model
  call are minutes apart (dedup + gate + system-prompt overhead), which
  swaps which cycle lands at position 24 versus 25 in this dataset and
  changes the reproduced mean (13.42 vs the recorded 13.79) without
  changing the median or max. Cycles with zero executor calls have no
  first_executor_call and sort last under that mode.

Median/mean/max in the summary are the ordinary statistics.median (the
average of the two middle values on an even count) -- ADR-031's original
"9" for the same 24 cycles was the lower-middle value, not this; #1903
records that discrepancy and corrects ADR-031 separately.
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from nanobot.runtime.service_paths import is_service_only

_MERGE_SUBJECT_PREFIX = "merge: integrate selfevo/cycle-"

def load_branch_files(repo: Path, cycle_ids, ref: str = "origin/main") -> dict[str, list[str]]:
    """cycle_id -> files its branch changed, from the newest
    ``merge: integrate selfevo/cycle-<cycle_id>`` commit on *ref*'s
    first-parent history. Cycle ids with no such merge are absent."""
    wanted = set(cycle_ids)
    if not wanted:
        return {}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        ).stdout

    merges: dict[str, str] = {}
    for line in git("log", ref, "--first-parent", "--format=%H%x09%s").splitlines():
        sha, _, subject = line.partition("\t")
        if not subject.startswith(_MERGE_SUBJECT_PREFIX):
            continue
        cid = subject[len(_MERGE_SUBJECT_PREFIX):].strip()
        if cid in wanted and cid not in merges:
            merges[cid] = sha
    return {
        cid: [f for f in git("diff", "--name-only", f"{sha}^1", f"{sha}^2").splitlines() if f.strip()]
        for cid, sha in merges.items()
    }


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iter_json_lines(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


def load_ledger_rows(state_dir: Path) -> list[dict]:
    ledger_dir = state_dir / "ledger"
    rows: list[dict] = []
    for path in sorted(ledger_dir.glob("cycles-*.jsonl.gz")):
        rows.extend(_iter_json_lines(path))
    active = ledger_dir / "cycles.jsonl"
    if active.is_file():
        rows.extend(_iter_json_lines(active))
    return rows


def load_llm_calls_rows(state_dir: Path) -> list[dict]:
    llm_dir = state_dir / "llm_calls"
    rows: list[dict] = []
    if not llm_dir.is_dir():
        return rows
    for path in sorted(llm_dir.glob("*.jsonl")):
        rows.extend(_iter_json_lines(path))
    return rows


def load_completed(state_dir: Path) -> dict[str, dict]:
    """cycle_id -> {"has_integration": bool, "usage_confirmed": bool}.

    A cycle_id can appear on more than one completed.json entry; either
    flag is True if ANY of its entries satisfies it.
    """
    path = state_dir / "demand" / "completed.json"
    result: dict[str, dict] = {}
    if not path.is_file():
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return result
    for entry in (data.get("entries") or {}).values():
        cid = entry.get("cycle_id")
        if not cid:
            continue
        slot = result.setdefault(cid, {"has_integration": False, "usage_confirmed": False})
        if entry.get("files_changed"):
            slot["has_integration"] = True
        if entry.get("confirmed") is True:
            slot["usage_confirmed"] = True
    return result


class CycleRecord:
    __slots__ = (
        "cycle_id", "first_start", "first_executor_call", "n_runs",
        "last_outcome", "last_verdict", "planning_session_outcome",
        "executor_calls", "planner_calls", "total_wall_ms",
        "demand_linked", "usage_confirmed", "branch_files",
    )

    def __init__(self, cycle_id: str) -> None:
        self.cycle_id = cycle_id
        self.first_start: "datetime | None" = None
        self.first_executor_call: "datetime | None" = None
        self.n_runs = 0
        self.last_outcome: "str | None" = None
        self.last_verdict: "str | None" = None
        self.planning_session_outcome: "str | None" = None
        self.executor_calls = 0
        self.planner_calls = 0
        self.total_wall_ms = 0.0
        self.demand_linked = False
        self.usage_confirmed = False
        #: None = not looked up, or no merge commit found for this cycle.
        self.branch_files: "list[str] | None" = None

    @property
    def condition_b(self) -> bool:
        return self.last_outcome == "success" and self.last_verdict == "accept"

    @property
    def excluded_by_rule_c(self) -> bool:
        return self.condition_b and is_service_only(self.branch_files)

    @property
    def condition_b_rule_c(self) -> bool:
        return self.condition_b and not self.excluded_by_rule_c

    @property
    def cause(self) -> str:
        return self.last_outcome if self.last_outcome is not None else "no_terminal_row"


def build_cycle_records(state_dir: Path) -> dict[str, CycleRecord]:
    ledger_rows = load_ledger_rows(state_dir)
    records: dict[str, CycleRecord] = {}

    def rec(cid: str) -> CycleRecord:
        return records.setdefault(cid, CycleRecord(cid))

    started_ts: dict[str, list[datetime]] = defaultdict(list)
    outcome_rows: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    planning_rows: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)

    for row in ledger_rows:
        cid = row.get("cycle_id")
        ts_raw = row.get("ts")
        if not cid or not ts_raw:
            continue
        try:
            ts = _parse_ts(ts_raw)
        except Exception:
            continue
        phase = row.get("phase")
        # "Started" is the sample's own unit of account (pre-registration):
        # a cycle_id counts only if it has a genuine write-ahead `started`
        # row. The SAME ledger file also carries pre-selection events
        # (guard_key_match, proposer_reject/skip, proposed,
        # demand_vector_split, ...) from OTHER writers that reuse the
        # `cycle_id` field for candidates rejected before a cycle ever
        # started -- those must never enter the denominator.
        if phase == "started":
            started_ts[cid].append(ts)
        elif phase == "outcome":
            outcome_rows[cid].append((ts, row))
        elif phase == "planning_session":
            planning_rows[cid].append((ts, row))

    for cid, starts in started_ts.items():
        r = rec(cid)
        starts = sorted(starts)
        r.first_start = starts[0]
        r.n_runs = len(starts)
        if outcome_rows.get(cid):
            _, last_row = max(outcome_rows[cid], key=lambda pair: pair[0])
            r.last_outcome = last_row.get("outcome")
            r.last_verdict = last_row.get("verdict")
        if planning_rows.get(cid):
            _, last_row = max(planning_rows[cid], key=lambda pair: pair[0])
            r.planning_session_outcome = last_row.get("outcome")

    for row in load_llm_calls_rows(state_dir):
        cid = row.get("cycle_id")
        if not cid or cid not in records:
            continue
        r = records[cid]
        ts_raw = row.get("ts")
        component = row.get("component")
        if component == "executor":
            r.executor_calls += 1
            if ts_raw:
                try:
                    ts = _parse_ts(ts_raw)
                except Exception:
                    ts = None
                if ts is not None and (r.first_executor_call is None or ts < r.first_executor_call):
                    r.first_executor_call = ts
        elif component == "planner":
            r.planner_calls += 1
        duration = row.get("duration_ms")
        if isinstance(duration, (int, float)):
            r.total_wall_ms += duration

    for cid, flags in load_completed(state_dir).items():
        if cid in records:
            records[cid].demand_linked = flags["has_integration"]
            records[cid].usage_confirmed = flags["usage_confirmed"]

    return records


def _start_key(r: CycleRecord) -> datetime:
    assert r.first_start is not None  # every built record has a real `started` row
    return r.first_start


def select_window(
    records: dict[str, CycleRecord],
    *,
    start: "datetime | None" = None,
    end: "datetime | None" = None,
    after: "datetime | None" = None,
    first_n: "int | None" = None,
    order_by: str = "first_start",
) -> list[CycleRecord]:
    with_start = [r for r in records.values() if r.first_start is not None]
    if after is not None:
        candidates = [r for r in with_start if _start_key(r) > after]
        if order_by == "first_executor_call":
            candidates = [r for r in candidates if r.first_executor_call is not None]

            def _call_key(r: CycleRecord) -> datetime:
                assert r.first_executor_call is not None
                return r.first_executor_call

            candidates.sort(key=_call_key)
        else:
            candidates.sort(key=_start_key)
        return candidates[:first_n] if first_n is not None else candidates
    assert start is not None
    candidates = [r for r in with_start if _start_key(r) >= start]
    if end is not None:
        candidates = [r for r in candidates if _start_key(r) < end]
    candidates.sort(key=_start_key)
    return candidates


def apply_rule_c(selected: list[CycleRecord], repo: Path, ref: str = "origin/main") -> None:
    """Fill ``branch_files`` for the (b) cycles in *selected* (rule C only
    ever removes (b) cycles, so the others need no lookup)."""
    files = load_branch_files(repo, [r.cycle_id for r in selected if r.condition_b], ref=ref)
    for r in selected:
        r.branch_files = files.get(r.cycle_id)


def summarize(selected: list[CycleRecord], rule_c_applied: bool = False) -> dict:
    n = len(selected)
    exec_counts = [r.executor_calls for r in selected]
    n_b = sum(1 for r in selected if r.condition_b)
    n_usage = sum(1 for r in selected if r.usage_confirmed)
    n_excluded = sum(1 for r in selected if r.excluded_by_rule_c)
    return {
        "rule_c_applied": rule_c_applied,
        "rule_c_excluded_count": n_excluded if rule_c_applied else None,
        "condition_b_rule_c_count": (n_b - n_excluded) if rule_c_applied else None,
        "condition_b_rule_c_share": ((n_b - n_excluded) / n) if (rule_c_applied and n) else None,
        "b_without_merge_count": (
            sum(1 for r in selected if r.condition_b and r.branch_files is None) if rule_c_applied else None
        ),
        "n": n,
        "executor_calls_median": statistics.median(exec_counts) if exec_counts else None,
        "executor_calls_mean": statistics.fmean(exec_counts) if exec_counts else None,
        "executor_calls_max": max(exec_counts) if exec_counts else None,
        "condition_b_count": n_b,
        "condition_b_share": (n_b / n) if n else None,
        "usage_confirmed_count": n_usage,
        "usage_confirmed_share": (n_usage / n) if n else None,
        "cause_breakdown": Counter(r.cause for r in selected),
    }


def print_report(selected: list[CycleRecord], summary: dict) -> None:
    header = (
        "cycle_id", "first_start", "n_runs", "executor_calls", "planner_calls",
        "total_wall_ms", "last_outcome", "last_verdict", "condition_b",
        "demand_linked", "usage_confirmed", "planning_session_outcome",
        "excluded_by_rule_c",
    )
    print("\t".join(header))
    for r in selected:
        print("\t".join(str(v) for v in (
            r.cycle_id,
            r.first_start.isoformat().replace("+00:00", "Z") if r.first_start else "",
            r.n_runs, r.executor_calls, r.planner_calls, round(r.total_wall_ms, 1),
            r.last_outcome, r.last_verdict, r.condition_b, r.demand_linked,
            r.usage_confirmed, r.planning_session_outcome,
            r.excluded_by_rule_c if summary["rule_c_applied"] else "n/a",
        )))
    print()
    print(f"n = {summary['n']}")
    print(
        f"(a) executor_calls: median={summary['executor_calls_median']} "
        f"mean={summary['executor_calls_mean']:.2f} max={summary['executor_calls_max']}"
        if summary["n"] else "(a) executor_calls: n=0"
    )
    print(
        f"(b) outcome=success + verdict=accept: {summary['condition_b_count']}/{summary['n']} "
        f"({summary['condition_b_share'] * 100:.1f}%)" if summary["n"] else "(b): n=0"
    )
    if not summary["rule_c_applied"]:
        print("(b, rule C): not applied -- pass --repo <instance repo>")
    elif summary["n"]:
        print(
            f"(b, rule C) excluding service-path-only branches: "
            f"{summary['condition_b_rule_c_count']}/{summary['n']} "
            f"({summary['condition_b_rule_c_share'] * 100:.1f}%); "
            f"excluded by rule C: {summary['rule_c_excluded_count']}; "
            f"(b) cycles with no merge commit found: {summary['b_without_merge_count']}"
        )
    print(
        f"usage-confirmed (completed.json confirmed=true): "
        f"{summary['usage_confirmed_count']}/{summary['n']} "
        f"({summary['usage_confirmed_share'] * 100:.1f}%)" if summary["n"] else "usage-confirmed: n=0"
    )
    print("cause breakdown:", dict(summary["cause_breakdown"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", required=True, type=Path)
    ap.add_argument("--start", help="UTC ISO-8601, e.g. 2026-09-21T00:00:00Z")
    ap.add_argument("--end", help="UTC ISO-8601, exclusive; omit for open-ended")
    ap.add_argument("--after", help="UTC ISO-8601; strictly-after cutoff for --first-n mode")
    ap.add_argument("--first-n", type=int, help="take the first N cycle_ids after --after, ordered by --order-by")
    ap.add_argument(
        "--order-by", choices=("first_start", "first_executor_call"), default="first_start",
        help="ordering for --first-n; first_executor_call only to reproduce ADR-031's historical cut",
    )
    ap.add_argument("--repo", type=Path, help="instance repo checkout; enables (b, rule C)")
    ap.add_argument("--ref", default="origin/main", help="ref whose first-parent history holds the cycle merges")
    args = ap.parse_args(argv)

    if bool(args.start) == bool(args.after):
        ap.error("give exactly one of --start/--end or --after/--first-n")
    if args.after and not args.first_n:
        ap.error("--after requires --first-n")

    records = build_cycle_records(args.state_dir)

    if args.start:
        selected = select_window(records, start=_parse_ts(args.start), end=_parse_ts(args.end) if args.end else None)
    else:
        selected = select_window(
            records, after=_parse_ts(args.after), first_n=args.first_n, order_by=args.order_by,
        )

    if args.repo:
        apply_rule_c(selected, args.repo, ref=args.ref)
    summary = summarize(selected, rule_c_applied=bool(args.repo))
    print_report(selected, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
