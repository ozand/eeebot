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
import sys
from collections import defaultdict
from datetime import datetime, timezone
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


def load_base_shas(state_dir: Path) -> dict[str, str]:
    base_shas: dict[str, str] = {}
    pattern = str(state_dir / "subagents" / "archive" / "result-*.json")
    for p in glob.glob(pattern):
        try:
            with open(p, "rt", encoding="utf-8", errors="replace") as fh:
                d = json.load(fh)
                cid = d.get("cycle_id")
                rb = d.get("rollback") or {}
                b_sha = rb.get("main_sha_before")
                if cid and b_sha:
                    base_shas[str(cid)] = str(b_sha)
        except Exception:
            pass
    return base_shas


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


def load_executor_calls(state_dir: Path, days: int = 7) -> dict[str, int]:
    llm_dir = state_dir / "llm_calls"
    if not llm_dir.is_dir():
        return {}
    files = sorted(glob.glob(str(llm_dir / "*.jsonl")))
    selected_files = files[-days:] if days > 0 else files
    calls: dict[str, int] = defaultdict(int)
    for p in selected_files:
        try:
            with open(p, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    cid = str(row.get("cycle_id") or "")
                    if cid and str(row.get("component") or "").lower() == "executor":
                        calls[cid] += 1
        except Exception:
            pass
    return dict(calls)


def load_cycles(state_dir: Path, days: int = 7) -> dict[str, dict[str, Any]]:
    ledger_dir = state_dir / "ledger"
    if not ledger_dir.is_dir():
        return {}
    all_files = sorted(
        glob.glob(str(ledger_dir / "cycles-*.jsonl.gz")) + glob.glob(str(ledger_dir / "cycles.jsonl"))
    )
    selected_files = all_files[-days:] if days > 0 else all_files
    cycles: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"starts": [], "outcomes": [], "proposed": None}
    )
    for p in selected_files:
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
                    phase = row.get("phase")
                    if phase == "proposed" and not cycles[cid]["proposed"]:
                        cycles[cid]["proposed"] = row
                    elif phase == "started":
                        cycles[cid]["starts"].append(row)
                    elif phase == "outcome":
                        cycles[cid]["outcomes"].append(row)
        except Exception:
            pass
    return dict(cycles)


def analyze_shape_costs(
    state_dir: Path,
    repo_dir: Path | None = None,
    days: int = 7,
) -> dict[str, Any]:
    state_dir = Path(state_dir)
    repo_dir = Path(repo_dir) if repo_dir else None
    base_shas = load_base_shas(state_dir)
    subagent_iters = load_subagent_iterations(state_dir)
    executor_calls = load_executor_calls(state_dir, days=days)
    cycles = load_cycles(state_dir, days=days)

    records_by_shape: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records_by_assignment: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for cid, data in cycles.items():
        p_row = data.get("proposed")
        if not p_row:
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

        duration_s = 0.0
        for s_row, o_row in zip(data.get("starts", []), data.get("outcomes", [])):
            st = parse_ts(s_row.get("ts"))
            ot = parse_ts(o_row.get("ts"))
            if st and ot and ot >= st:
                duration_s += (ot - st).total_seconds()
        if duration_s == 0.0 and data.get("starts") and data.get("outcomes"):
            st = parse_ts(data["starts"][0].get("ts"))
            ot = parse_ts(data["outcomes"][-1].get("ts"))
            if st and ot and ot >= st:
                duration_s = (ot - st).total_seconds()

        outcomes = [str(o.get("outcome") or "") for o in data.get("outcomes", [])]
        final_outcome = outcomes[-1] if outcomes else "unknown"

        est = p_row.get("estimated_size") or p_row.get("estimated_iterations") or p_row.get("iterations_planned")

        demand_id = str(p_row.get("demand_id") or "")
        assignment = "assigned" if demand_id.startswith("priority-") else "self_proposed"

        rec = {
            "cycle_id": cid,
            "shape": shape,
            "assignment": assignment,
            "executor_calls": calls,
            "iterations_used": iters,
            "wall_clock_s": duration_s,
            "outcome": final_outcome,
            "estimate": est,
        }
        records_by_shape[shape].append(rec)
        records_by_assignment[assignment].append(rec)

    report: dict[str, Any] = {"shapes": {}, "provenance": "harness_measured_state"}

    for shape in TASK_SHAPES:
        recs = records_by_shape.get(shape, [])
        n = len(recs)
        if n < 5:
            report["shapes"][shape] = {
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

        integrated_count = sum(1 for r in recs if r["outcome"] == "success")
        rollback_count = sum(1 for r in recs if r["outcome"] in ("failed", "partial"))

        pairs = [(r["estimate"], r["iterations_used"]) for r in recs if r["estimate"] is not None]
        missing_count = sum(1 for r in recs if r["estimate"] is None)

        report["shapes"][shape] = {
            "n": n,
            "status": "достаточно истории",
            "distributions": {
                "executor_calls": compute_distribution(exec_calls),
                "iterations_used": compute_distribution(iterations),
                "wall_clock_s": compute_distribution(wall_clocks),
            },
            "rates": {
                "integrated_count": integrated_count,
                "integrated_share": round(integrated_count / n, 4),
                "rollback_count": rollback_count,
                "rollback_share": round(rollback_count / n, 4),
            },
            "estimates": {
                "pairs": pairs,
                "missing": missing_count,
            },
        }

    report["assignment_comparison"] = {}
    for assignment in ("assigned", "self_proposed"):
        recs = records_by_assignment.get(assignment, [])
        n = len(recs)
        if n == 0:
            continue
        exec_calls = [r["executor_calls"] for r in recs]
        iterations = [r["iterations_used"] for r in recs]
        wall_clocks = [r["wall_clock_s"] for r in recs]
        int_c = sum(1 for r in recs if r["outcome"] == "success")
        rb_c = sum(1 for r in recs if r["outcome"] in ("failed", "partial"))
        report["assignment_comparison"][assignment] = {
            "n": n,
            "distributions": {
                "executor_calls": compute_distribution(exec_calls),
                "iterations_used": compute_distribution(iterations),
                "wall_clock_s": compute_distribution(wall_clocks),
            },
            "rates": {
                "integrated_count": int_c,
                "integrated_share": round(int_c / n, 4),
                "rollback_count": rb_c,
                "rollback_share": round(rb_c / n, 4),
            },
            "estimates": {
                "pairs": [(r["estimate"], r["iterations_used"]) for r in recs if r["estimate"] is not None],
                "missing": sum(1 for r in recs if r["estimate"] is None),
            },
        }

    return report


def format_report(report: dict[str, Any], days: int = 7) -> str:
    lines = [
        f"=== Отчет о стоимости по формам задач (#1795) — окно {days} дней ===",
        f"Provenance: {report.get('provenance', 'unknown')} (ADR-023/ADR-027)",
        "",
    ]

    for shape in TASK_SHAPES:
        info = report["shapes"].get(shape, {})
        n = info.get("n", 0)
        lines.append(f"Форма: {shape} (n = {n})")
        if info.get("status") != "достаточно истории":
            lines.append("  Статус: мало истории (n < 5), статистический вывод опущен.")
            lines.append("")
            continue

        rates = info["rates"]
        dists = info["distributions"]
        ests = info["estimates"]

        lines.append(f"  Доля интеграций: {rates['integrated_share']*100:.1f}% ({rates['integrated_count']}/{n})")
        lines.append(f"  Доля откатов:     {rates['rollback_share']*100:.1f}% ({rates['rollback_count']}/{n})")

        ec = dists["executor_calls"]
        lines.append(
            f"  Executor-вызовы:  min={ec['min']:.0f}, p25={ec['p25']:.0f}, median={ec['median']:.0f}, "
            f"p75={ec['p75']:.0f}, p90={ec['p90']:.0f}, max={ec['max']:.0f}"
        )

        iu = dists["iterations_used"]
        lines.append(
            f"  Iterations used:  min={iu['min']:.0f}, p25={iu['p25']:.0f}, median={iu['median']:.0f}, "
            f"p75={iu['p75']:.0f}, p90={iu['p90']:.0f}, max={iu['max']:.0f}"
        )

        wc = dists["wall_clock_s"]
        lines.append(
            f"  Wall clock (сек): min={wc['min']:.1f}, p25={wc['p25']:.1f}, median={wc['median']:.1f}, "
            f"p75={wc['p75']:.1f}, p90={wc['p90']:.1f}, max={wc['max']:.1f}"
        )

        pairs = ests.get("pairs", [])
        missing = ests.get("missing", 0)
        if pairs:
            lines.append(f"  Пары оценка/факт: {len(pairs)} пар: {pairs}")
        else:
            lines.append(f"  Пары оценка/факт: 0 пар (оценка отсутствует: {missing}/{n})")
        lines.append("")

    ac = report.get("assignment_comparison", {})
    if ac:
        lines.append("=== Сравнение: Назначенные оператором (assigned) vs Инициатива цикла (self_proposed) ===")
        for kind, label in [("assigned", "Назначенные (assigned)"), ("self_proposed", "Инициатива цикла (self_proposed)")]:
            data = ac.get(kind)
            if not data:
                continue
            k_n = data["n"]
            k_rates = data["rates"]
            k_dists = data["distributions"]
            lines.append(f"Группа: {label} (n = {k_n})")
            lines.append(f"  Доля интеграций: {k_rates['integrated_share']*100:.1f}% ({k_rates['integrated_count']}/{k_n})")
            lines.append(f"  Доля откатов:     {k_rates['rollback_share']*100:.1f}% ({k_rates['rollback_count']}/{k_n})")
            ec = k_dists["executor_calls"]
            lines.append(f"  Executor-вызовы:  min={ec['min']:.0f}, p25={ec['p25']:.0f}, median={ec['median']:.0f}, p75={ec['p75']:.0f}, max={ec['max']:.0f}")
            iu = k_dists["iterations_used"]
            lines.append(f"  Iterations used:  min={iu['min']:.0f}, p25={iu['p25']:.0f}, median={iu['median']:.0f}, p75={iu['p75']:.0f}, max={iu['max']:.0f}")
            wc = k_dists["wall_clock_s"]
            lines.append(f"  Wall clock (сек): min={wc['min']:.1f}, p25={wc['p25']:.1f}, median={wc['median']:.1f}, p75={wc['p75']:.1f}, max={wc['max']:.1f}")
            lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report task shape cost distributions (#1795).")
    parser.add_argument("--state-dir", default="/var/lib/eeepc-agent/self-evolving-agent/state", help="Path to state dir")
    parser.add_argument("--repo", default="/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving", help="Path to instance git repo")
    parser.add_argument("--days", type=int, default=7, help="Days window")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    report = analyze_shape_costs(Path(args.state_dir), repo_dir=Path(args.repo) if args.repo else None, days=args.days)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report, days=args.days))


if __name__ == "__main__":
    main()
