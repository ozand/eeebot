"""#1795: Offline cost report by task shape.

Measures executor calls (per cycle_id, aggregated across runs), iterations_used,
wall-clock duration, integration share, rollback share, and estimate-vs-actual pairs
for each task shape defined in nanobot.runtime.task_shape.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nanobot.runtime.task_shape import TASK_SHAPES, classify_task_shape  # noqa: E402


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion."""
    if total <= 0:
        return 0.0, 0.0
    p = successes / total
    denom = 1 + (z**2) / total
    centre = (p + (z**2) / (2 * total)) / denom
    spread = (z * math.sqrt((p * (1 - p) + (z**2) / (4 * total)) / total)) / denom
    return round(max(0.0, centre - spread), 4), round(min(1.0, centre + spread), 4)


def classify_resolution(
    final_outcome: str,
    outcome_row: dict[str, Any] | None,
    rollback: dict[str, Any] | None,
    gates: list[dict[str, Any]],
) -> tuple[str, str]:
    """Classify resolution into three non-integration types plus integration:
    - ('integrated', 'success')
    - ('gate_rejected', reason): real work attempted and rejected by gate (verdict_reason)
    - ('duplicate_cut', reason): duplicate cut pre-spawn (existence_index_duplicate, recent_duplicate_failure)
    - ('empty_noop', reason): executor ran but produced 0 file changes (files_changed empty / no-op)
    """
    if final_outcome == "success":
        return "integrated", "success"

    outcome_row = outcome_row or {}
    rollback = rollback or {}
    files_changed = outcome_row.get("files_changed") or []
    outcome_reason = outcome_row.get("reason") or outcome_row.get("verdict_reason")
    rb_reason = rollback.get("reason")

    # (2) Дубль отсечен до исполнения
    if (
        final_outcome == "skipped-duplicate"
        or outcome_reason in ("existence_index_duplicate", "recent_duplicate_failure", "demand_cooling")
        or rb_reason in ("existence_index_duplicate", "recent_duplicate_failure", "demand_cooling")
    ):
        return "duplicate_cut", str(rb_reason or outcome_reason or "duplicate_cut")

    # (1) Работа была и отвергнута гейтом (настоящий откат)
    gate_reason = None
    for g in gates:
        if g.get("allowed") is False:
            gate_reason = g.get("reason")

    if (
        len(files_changed) > 0
        or gate_reason is not None
        or rb_reason in (
            "mutation_surface_violation", "gate_failed", "test_weakening",
            "fitness_sidecar_tamper", "switch_base_gate_error", "blocked_file_present"
        )
    ):
        return "gate_rejected", str(gate_reason or rb_reason or outcome_reason or "gate_rejected")

    # (3) Исполнитель ничего не изменил (files_changed пуст)
    return "empty_noop", str(outcome_reason or rb_reason or "empty_noop")


def compute_distribution(values: list[float | int]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "p25": 0.0, "median": 0.0, "p75": 0.0, "p90": 0.0, "max": 0.0}
    s = sorted(values)
    n = len(s)

    def q(pct: float) -> float:
        idx = int(pct * (n - 1))
        return float(s[idx])

    return {
        "min": float(s[0]),
        "p25": q(0.25),
        "median": q(0.50),
        "p75": q(0.75),
        "p90": q(0.90),
        "max": float(s[-1]),
    }


def summarize_resolutions(recs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(recs)
    if n == 0:
        return {}
    int_recs = [r for r in recs if r.get("resolution_kind") == "integrated"]
    gate_recs = [r for r in recs if r.get("resolution_kind") == "gate_rejected"]
    dup_recs = [r for r in recs if r.get("resolution_kind") == "duplicate_cut"]
    noop_recs = [r for r in recs if r.get("resolution_kind") == "empty_noop"]

    def count_reasons(sub_recs: list[dict[str, Any]]) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for r in sub_recs:
            reason = str(r.get("resolution_reason") or "unknown")
            c[reason] += 1
        return dict(c)

    ci_low, ci_high = wilson_interval(len(int_recs), n)

    return {
        "integrated_count": len(int_recs),
        "integrated_share": round(len(int_recs) / n, 4),
        "integrated_ci95": [ci_low, ci_high],
        "gate_rejected_count": len(gate_recs),
        "gate_rejected_share": round(len(gate_recs) / n, 4),
        "gate_rejected_reasons": count_reasons(gate_recs),
        "duplicate_cut_count": len(dup_recs),
        "duplicate_cut_share": round(len(dup_recs) / n, 4),
        "duplicate_cut_reasons": count_reasons(dup_recs),
        "empty_noop_count": len(noop_recs),
        "empty_noop_share": round(len(noop_recs) / n, 4),
        "empty_noop_reasons": count_reasons(noop_recs),
    }


def load_result_artifacts(state_dir: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    base_shas: dict[str, str] = {}
    rollbacks: dict[str, dict[str, Any]] = {}
    pattern = str(state_dir / "subagents" / "archive" / "result-*.json")
    for p in glob.glob(pattern):
        try:
            with open(p, "rt", encoding="utf-8", errors="replace") as fh:
                d = json.load(fh)
                cid = d.get("cycle_id")
                rb = d.get("rollback")
                if cid and isinstance(rb, dict):
                    rollbacks[str(cid)] = rb
                    b_sha = rb.get("main_sha_before")
                    if b_sha:
                        base_shas[str(cid)] = str(b_sha)
        except Exception:
            pass
    return base_shas, rollbacks


def load_subagent_iterations(state_dir: Path) -> dict[str, int]:
    archive_dir = state_dir / "subagents" / "archive"
    if not archive_dir.is_dir():
        return {}
    subagent_iters: dict[str, int] = defaultdict(int)
    for p in glob.glob(str(archive_dir / "*.json")):
        try:
            with open(p, "rt", encoding="utf-8", errors="replace") as fh:
                d = json.load(fh)
                cid = str(d.get("cycle_id") or "")
                label = str(d.get("label") or "")
                iters = d.get("context_usage", {}).get("iterations")
                if cid and iters is not None and label != "selfevo-planner":
                    subagent_iters[cid] += len(iters)
        except Exception:
            pass
    return dict(subagent_iters)


def load_executor_calls(
    state_dir: Path, window_start: datetime, window_end: datetime
) -> dict[str, int]:
    llm_dir = state_dir / "llm_calls"
    if not llm_dir.is_dir():
        return {}
    calls: dict[str, int] = defaultdict(int)
    for p in glob.glob(str(llm_dir / "*.jsonl")):
        try:
            with open(p, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    ts = parse_ts(row.get("ts"))
                    if not ts or ts < window_start or ts > window_end:
                        continue
                    cid = str(row.get("cycle_id") or "")
                    if cid and str(row.get("component") or "").lower() == "executor":
                        calls[cid] += 1
        except Exception:
            pass
    return dict(calls)


def load_cycles(
    state_dir: Path, window_start: datetime, window_end: datetime
) -> dict[str, dict[str, Any]]:
    ledger_dir = state_dir / "ledger"
    if not ledger_dir.is_dir():
        return {}
    all_files = sorted(
        glob.glob(str(ledger_dir / "cycles-*.jsonl.gz")) + glob.glob(str(ledger_dir / "cycles.jsonl"))
    )
    cycles: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"starts": [], "outcomes": [], "gates": [], "proposed": None, "phases": set()}
    )
    for p in all_files:
        open_fn = gzip.open if p.endswith(".gz") else open
        try:
            with open_fn(p, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    cid = str(row.get("cycle_id") or "")
                    if not cid or not cid.startswith("cycle-"):
                        continue
                    ts = parse_ts(row.get("ts"))
                    if not ts or ts < window_start or ts > window_end:
                        continue
                    phase = row.get("phase")
                    cycles[cid]["phases"].add(phase)
                    if phase == "proposed" and not cycles[cid]["proposed"]:
                        cycles[cid]["proposed"] = row
                    elif phase == "started":
                        cycles[cid]["starts"].append(row)
                    elif phase == "outcome":
                        cycles[cid]["outcomes"].append(row)
                    elif phase == "gate":
                        cycles[cid]["gates"].append(row)
        except Exception:
            pass
    return dict(cycles)


def build_shape_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    records_by_shape: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records_by_assignment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        records_by_shape[r["shape"]].append(r)
        records_by_assignment[r["assignment"]].append(r)

    shapes_out: dict[str, Any] = {}
    for shape in TASK_SHAPES:
        recs = records_by_shape.get(shape, [])
        n = len(recs)
        if n < 5:
            shapes_out[shape] = {
                "n": n,
                "status": "мало истории (n < 5)",
                "distributions": None,
                "rates": None,
                "estimates": {"pairs": [], "missing": n},
            }
            continue

        exec_calls = [r["executor_calls"] for r in recs]
        iterations = [r["iterations_used"] for r in recs]
        wall_clocks = [r["wall_clock_s"] for r in recs]

        pairs = [(r["estimate"], r["iterations_used"]) for r in recs if r["estimate"] is not None]
        missing_count = sum(1 for r in recs if r["estimate"] is None)

        shapes_out[shape] = {
            "n": n,
            "status": "достаточно истории",
            "distributions": {
                "executor_calls": compute_distribution(exec_calls),
                "iterations_used": compute_distribution(iterations),
                "wall_clock_s": compute_distribution(wall_clocks),
            },
            "rates": summarize_resolutions(recs),
            "estimates": {
                "pairs": pairs,
                "missing": missing_count,
            },
        }

    assignment_out: dict[str, Any] = {}
    for assignment in ("assigned", "self_proposed"):
        recs = records_by_assignment.get(assignment, [])
        n = len(recs)
        if n == 0:
            continue
        exec_calls = [r["executor_calls"] for r in recs]
        iterations = [r["iterations_used"] for r in recs]
        wall_clocks = [r["wall_clock_s"] for r in recs]

        assignment_out[assignment] = {
            "n": n,
            "distributions": {
                "executor_calls": compute_distribution(exec_calls),
                "iterations_used": compute_distribution(iterations),
                "wall_clock_s": compute_distribution(wall_clocks),
            },
            "rates": summarize_resolutions(recs),
            "estimates": {
                "pairs": [(r["estimate"], r["iterations_used"]) for r in recs if r["estimate"] is not None],
                "missing": sum(1 for r in recs if r["estimate"] is None),
            },
        }

    return {"shapes": shapes_out, "assignment_comparison": assignment_out}


def analyze_shape_costs(
    state_dir: Path,
    repo_dir: Path | None = None,
    days: int = 7,
    now: datetime | None = None,
    split_at: datetime | str | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir)
    repo_dir = Path(repo_dir) if repo_dir else None
    now_utc = now or datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(days=days)

    base_shas, result_rollbacks = load_result_artifacts(state_dir)
    subagent_iters = load_subagent_iterations(state_dir)
    executor_calls = load_executor_calls(state_dir, cutoff_utc, now_utc)
    cycles = load_cycles(state_dir, cutoff_utc, now_utc)

    all_records: list[dict[str, Any]] = []
    total_unpaired_starts = 0
    total_multi_run_cycles = 0
    dropped_no_proposed = 0

    for cid, data in cycles.items():
        p_row = data.get("proposed")
        if not p_row:
            dropped_no_proposed += 1
            continue
        base_sha = base_shas.get(cid)
        shape = classify_task_shape(
            task_title=str(p_row.get("task_title") or ""),
            target_path=str(p_row.get("target_path") or ""),
            repo=repo_dir,
            base_sha=base_sha,
        )

        calls = executor_calls.get(cid, 0)
        iters = sum(int(o.get("iterations_used") or 0) for o in data.get("outcomes", []))
        if iters == 0:
            iters = subagent_iters.get(cid, 0)

        # Pair each outcome with latest start before it (#1926 review)
        starts = sorted([parse_ts(s["ts"]) for s in data.get("starts", []) if parse_ts(s.get("ts"))])
        outcomes = sorted([parse_ts(o["ts"]) for o in data.get("outcomes", []) if parse_ts(o.get("ts"))])

        if len(starts) > 1:
            total_multi_run_cycles += 1

        used_starts = set()
        run_durations = []
        for ot in outcomes:
            candidates = [st for st in starts if st <= ot and st not in used_starts]
            if candidates:
                best_start = candidates[-1]
                used_starts.add(best_start)
                run_durations.append((ot - best_start).total_seconds())

        unpaired = len(starts) - len(used_starts)
        total_unpaired_starts += unpaired
        duration_s = sum(run_durations)

        outcomes_list = [str(o.get("outcome") or "") for o in data.get("outcomes", [])]
        final_outcome = outcomes_list[-1] if outcomes_list else "unknown"
        final_outcome_row = data.get("outcomes", [])[-1] if data.get("outcomes") else None

        rb = result_rollbacks.get(cid) or {}
        res_kind, res_reason = classify_resolution(
            final_outcome=final_outcome,
            outcome_row=final_outcome_row,
            rollback=rb,
            gates=data.get("gates", []),
        )

        est = p_row.get("estimated_size") or p_row.get("estimated_iterations") or p_row.get("iterations_planned")

        demand_id = str(p_row.get("demand_id") or "")
        assignment = "assigned" if demand_id.startswith("priority-") else "self_proposed"

        rec = {
            "cycle_id": cid,
            "ts": p_row.get("ts"),
            "shape": shape,
            "assignment": assignment,
            "executor_calls": calls,
            "iterations_used": iters,
            "wall_clock_s": duration_s,
            "outcome": final_outcome,
            "resolution_kind": res_kind,
            "resolution_reason": res_reason,
            "estimate": est,
        }
        all_records.append(rec)

    full_report = build_shape_report(all_records)
    report: dict[str, Any] = {
        "shapes": full_report["shapes"],
        "assignment_comparison": full_report["assignment_comparison"],
        "provenance": "harness_measured_state",
        "window": {"start": cutoff_utc.isoformat(), "end": now_utc.isoformat(), "days": days},
        "totals": {
            "total_cycles_in_window": len(cycles),
            "proposed_cycles_analyzed": len(all_records),
            "dropped_no_proposed": dropped_no_proposed,
            "multi_run_cycles": total_multi_run_cycles,
            "unpaired_starts": total_unpaired_starts,
        },
    }

    if split_at is not None:
        split_utc = parse_ts(split_at) if isinstance(split_at, str) else split_at
        if split_utc:
            before_recs = [r for r in all_records if parse_ts(r.get("ts")) and parse_ts(r.get("ts")) < split_utc]
            after_recs = [r for r in all_records if parse_ts(r.get("ts")) and parse_ts(r.get("ts")) >= split_utc]
            report["split"] = {
                "split_at": split_utc.isoformat(),
                "before": {
                    "totals": {"proposed_cycles_analyzed": len(before_recs)},
                    **build_shape_report(before_recs),
                },
                "after": {
                    "totals": {"proposed_cycles_analyzed": len(after_recs)},
                    **build_shape_report(after_recs),
                },
            }

    return report




def format_sub_report(data: dict[str, Any], title_prefix: str = "") -> list[str]:
    lines: list[str] = []
    shapes = data.get("shapes", {})
    for shape in TASK_SHAPES:
        info = shapes.get(shape, {})
        n = info.get("n", 0)
        lines.append(f"{title_prefix}Форма: {shape} (n = {n})")
        if info.get("status") != "достаточно истории":
            lines.append("  Статус: мало истории (n < 5), статистический вывод опущен.")
            lines.append("")
            continue

        rates = info["rates"]
        dists = info["distributions"]
        ests = info["estimates"]

        lines.append(f"  Доля интеграций (outcome success, bridge.py:5286): {rates['integrated_share']*100:.1f}% ({rates['integrated_count']}/{n})")
        lines.append(f"  (1) Отвергнуто гейтом (настоящий откат):            {rates['gate_rejected_share']*100:.1f}% ({rates['gate_rejected_count']}/{n}) | причины: {rates.get('gate_rejected_reasons', {})}")
        lines.append(f"  (2) Дубль отсечен до исполнения (pre-spawn):        {rates['duplicate_cut_share']*100:.1f}% ({rates['duplicate_cut_count']}/{n}) | причины: {rates.get('duplicate_cut_reasons', {})}")
        lines.append(f"  (3) Исполнитель ничего не изменил (files []):       {rates['empty_noop_share']*100:.1f}% ({rates['empty_noop_count']}/{n}) | причины: {rates.get('empty_noop_reasons', {})}")

        ec = dists["executor_calls"]
        lines.append(f"  Executor-вызовы:  min={ec['min']:.0f}, p25={ec['p25']:.0f}, median={ec['median']:.0f}, p75={ec['p75']:.0f}, p90={ec['p90']:.0f}, max={ec['max']:.0f}")

        iu = dists["iterations_used"]
        lines.append(f"  Iterations used:  min={iu['min']:.0f}, p25={iu['p25']:.0f}, median={iu['median']:.0f}, p75={iu['p75']:.0f}, p90={iu['p90']:.0f}, max={iu['max']:.0f}")

        wc = dists["wall_clock_s"]
        lines.append(f"  Wall clock (сек): min={wc['min']:.1f}, p25={wc['p25']:.1f}, median={wc['median']:.1f}, p75={wc['p75']:.1f}, max={wc['max']:.1f}")

        pairs = ests.get("pairs", [])
        missing = ests.get("missing", 0)
        if pairs:
            lines.append(f"  Пары оценка/факт: {len(pairs)} пар: {pairs}")
        else:
            lines.append(f"  Пары оценка/факт: 0 пар (оценка отсутствует: {missing}/{n})")
        lines.append("")

    ac = data.get("assignment_comparison", {})
    if ac:
        lines.append(f"{title_prefix}=== Сравнение: Назначенные оператором (assigned) vs Инициатива цикла (self_proposed) ===")
        for kind, label in [("assigned", "Назначенные (assigned)"), ("self_proposed", "Инициатива цикла (self_proposed)")]:
            k_data = ac.get(kind)
            if not k_data:
                continue
            k_n = k_data["n"]
            k_rates = k_data["rates"]
            k_dists = k_data["distributions"]
            ci = k_rates.get("integrated_ci95", [0.0, 0.0])
            lines.append(f"Группа: {label} (n = {k_n})")
            lines.append(f"  Доля интеграций: {k_rates['integrated_share']*100:.1f}% ({k_rates['integrated_count']}/{k_n}, 95% CI: [{ci[0]*100:.1f}%, {ci[1]*100:.1f}%])")
            lines.append(f"  (1) Отвергнуто гейтом:      {k_rates['gate_rejected_share']*100:.1f}% ({k_rates['gate_rejected_count']}/{k_n})")
            lines.append(f"  (2) Дубль отсечен:          {k_rates['duplicate_cut_share']*100:.1f}% ({k_rates['duplicate_cut_count']}/{k_n})")
            lines.append(f"  (3) Ничего не изменил:      {k_rates['empty_noop_share']*100:.1f}% ({k_rates['empty_noop_count']}/{k_n})")
            ec = k_dists["executor_calls"]
            lines.append(f"  Executor-вызовы:  min={ec['min']:.0f}, p25={ec['p25']:.0f}, median={ec['median']:.0f}, p75={ec['p75']:.0f}, max={ec['max']:.0f}")
            iu = k_dists["iterations_used"]
            lines.append(f"  Iterations used:  min={iu['min']:.0f}, p25={iu['p25']:.0f}, median={iu['median']:.0f}, p75={iu['p75']:.0f}, max={iu['max']:.0f}")
            wc = k_dists["wall_clock_s"]
            lines.append(f"  Wall clock (сек): min={wc['min']:.1f}, p25={wc['p25']:.1f}, median={wc['median']:.1f}, p75={wc['p75']:.1f}, max={wc['max']:.1f}")
            lines.append("")
        assigned_n = ac.get("assigned", {}).get("n", 0)
        lines.append(
            f"  Примечание по неопределенности: доверительные интервалы интеграций (assigned vs self_proposed) "
            f"перекрываются из-за малого размера выборки оператора (n={assigned_n}); различие является "
            f"описательным наблюдением выборки, а не статистически значимым выводом."
        )
        lines.append("")
    return lines


def format_report(report: dict[str, Any], days: int = 7) -> str:
    win = report.get("window", {})
    tot = report.get("totals", {})
    lines = [
        f"=== Отчет о стоимости по формам задач (#1795) — окно {days} дней ===",
        f"Окно UTC: {win.get('start')} — {win.get('end')}",
        f"Всего циклов в окне: {tot.get('total_cycles_in_window', 0)} (пропущено без proposed: {tot.get('dropped_no_proposed', 0)})",
        f"Циклов с задачей (proposed) проанализировано: {tot.get('proposed_cycles_analyzed', 0)}",
        f"Циклов с >1 прогоном: {tot.get('multi_run_cycles', 0)}, непарных стартов: {tot.get('unpaired_starts', 0)}",
        f"Provenance: {report.get('provenance', 'unknown')} (ADR-023/ADR-027)",
        "",
    ]

    lines.extend(format_sub_report(report))

    split = report.get("split")
    if split:
        split_at = split.get("split_at")
        lines.append("================================================================================")
        lines.append(f"=== СРЕЗ ДО СТАВКИ: ДО {split_at} (n = {split.get('before', {}).get('totals', {}).get('proposed_cycles_analyzed', 0)}) ===")
        lines.append("================================================================================")
        lines.extend(format_sub_report(split.get("before", {}), title_prefix="[ДО] "))

        lines.append("================================================================================")
        lines.append(f"=== СРЕЗ ПОСЛЕ СТАВКИ: ПОСЛЕ {split_at} (n = {split.get('after', {}).get('totals', {}).get('proposed_cycles_analyzed', 0)}) ===")
        lines.append("================================================================================")
        lines.extend(format_sub_report(split.get("after", {}), title_prefix="[ПОСЛЕ] "))

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report task shape cost distributions (#1795).")
    parser.add_argument("--state-dir", default="/var/lib/eeepc-agent/self-evolving-agent/state", help="Path to state dir")
    parser.add_argument("--repo", default="/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving", help="Path to instance git repo")
    parser.add_argument("--days", type=int, default=7, help="Days window")
    parser.add_argument("--split-at", default=None, help="Split report at UTC timestamp (e.g. 2026-09-18T18:01:00Z)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    report = analyze_shape_costs(
        Path(args.state_dir),
        repo_dir=Path(args.repo) if args.repo else None,
        days=args.days,
        split_at=args.split_at,
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report, days=args.days))


if __name__ == "__main__":
    main()
