"""Bounded, provenance-carrying archive inputs for the strategist role (#1182).

Every reader returns a value AND a status ("complete" | "partial" | "empty") so
:func:`nanobot.runtime.strategist.run_strategist` can refuse the LLM call when
the archive view is mostly empty instead of advising from nothing. Readers never
raise; failures degrade to "empty" with a note. Stdlib only.

:func:`ledger_rows` is a narrow, horizon-bounded read of the live
``cycles.jsonl`` plus the rotated ``cycles-YYYY-MM-DD.jsonl.gz`` archives
(``cycle_ledger`` writes one per UTC day). It should collapse into the shared
``state_access.ledger_window`` reader from #1174 when that lands; it mirrors
that contract's meta fields (``covered_from``, ``files_read``, ``files_skipped``,
``bytes_read``, ``status``).
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import statistics
from collections import deque
from pathlib import Path
from typing import Any

# Point-in-time JSON sidecars (tree, scorecard latest, goal_text).
_MAX_JSON_BYTES = 256_000
# Ledger window for the funnel and the tree outcome mix.
_FUNNEL_HORIZON_DAYS = 30
_FUNNEL_MAX_IDS = 200
FUNNEL_COLUMNS = ("proposed", "integrated", "self_dedup", "futile", "attempt_count")
_LEDGER_MAX_BYTES = 8 * 1024 * 1024
_LEDGER_PHASES = ("proposed", "proposer_reject", "outcome", "evolution_tree")
# scorecard/history.jsonl: one ~3 KB row per recompute (~48/day live), so the
# prompt gets a sampled trend of numeric leaves, not rows.
_HISTORY_TAIL_ROWS = 400
_HISTORY_MAX_FILE_BYTES = 8 * 1024 * 1024
_HISTORY_DAYS = 7
_HISTORY_SAMPLES = 8
_HISTORY_MAX_SERIES = 60
_HISTORY_MAX_DEPTH = 3
# Insight sources: v2 lesson cards, legacy ``generalized_insight`` rows, errors.
_INSIGHTS_V2 = 10
_INSIGHTS_LEGACY = 10
_INSIGHTS_ERRORS = 5
_INSIGHT_TEXT_CHARS = 200
_INDEX_CHARS = 2_000
_TREE_BEST_PATH_HOPS = 20
_MAX_TEXT = 4_000
# Inputs the strategist conditions on; more than _MAX_EMPTY_INPUTS empty => refuse.
INPUT_NAMES = ("goals", "scorecard", "funnel", "insights", "evolution_tree")
_MAX_EMPTY_INPUTS = 1


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _parse_ts(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def load_json(path: Path, default: Any = None, max_bytes: int = _MAX_JSON_BYTES) -> Any:
    """Bounded JSON read; ``default`` on absent/oversize/corrupt."""
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _read_lines(path: Path) -> list[str] | None:
    """All lines of a text or ``.gz`` file; ``None`` when unreadable/corrupt."""
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            return handle.readlines()
    except Exception:
        return None

def ledger_rows(state_root: Path, horizon_days: int = _FUNNEL_HORIZON_DAYS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rows with a phase in :data:`_LEDGER_PHASES` from archives inside the
    horizon (oldest first, by the date in the file name) then the live file.
    Substring-prefilters before ``json.loads``; skips corrupt files per file."""
    ledger_dir = Path(state_root) / "ledger"
    cutoff = (_utcnow() - dt.timedelta(days=horizon_days)).date()
    meta: dict[str, Any] = {"files_read": 0, "files_skipped": 0, "bytes_read": 0, "covered_from": None, "status": "empty", "notes": []}
    paths: list[Path] = []
    for gz_path in sorted(ledger_dir.glob("cycles-*.jsonl.gz")):
        try:
            day = dt.date.fromisoformat(gz_path.name[len("cycles-"):-len(".jsonl.gz")])
        except ValueError:
            meta["files_skipped"] += 1
            meta["notes"].append(f"bad_name:{gz_path.name}")
            continue
        if day >= cutoff:
            paths.append(gz_path)
    live = ledger_dir / "cycles.jsonl"
    if live.is_file():
        paths.append(live)
    # cycle_ledger writes json.dumps default separators; accept the compact form too.
    needles = tuple(f'"phase":{sep}"{phase}"' for phase in _LEDGER_PHASES for sep in (" ", ""))
    rows: list[dict[str, Any]] = []
    for path in paths:
        lines = _read_lines(path)
        if lines is None:
            meta["files_skipped"] += 1
            meta["notes"].append(f"unreadable:{path.name}")
            meta["status"] = "partial"
            continue
        meta["files_read"] += 1
        for line in lines:
            meta["bytes_read"] += len(line)
            if meta["bytes_read"] > _LEDGER_MAX_BYTES:
                meta["status"] = "partial"
                meta["notes"].append("cap_bytes")
                break
            if not any(needle in line for needle in needles):
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
        if "cap_bytes" in meta["notes"]:
            break
    stamps = [str(r.get("ts")) for r in rows if r.get("ts")]
    meta["covered_from"] = min(stamps) if stamps else None
    if rows and meta["status"] != "partial":
        meta["status"] = "complete"
    return rows, meta

def charter_input(state_root: Path) -> tuple[str, dict[str, Any]]:
    """The operator charter: ``<RELEASE_ROOT>/goals.md`` (the path the proposer
    reads, #944), falling back to ``state/goals/goal_text.json`` ``text``."""
    from nanobot.runtime.goal_review import read_charter_text
    from nanobot.runtime.llm_proposer import _release_root_from_env

    text = read_charter_text(_release_root_from_env())
    source = "release_root"
    if not text:
        legacy = load_json(Path(state_root) / "goals" / "goal_text.json", {})
        text = str(legacy.get("text") or "") if isinstance(legacy, dict) else ""
        source = "goal_text.json" if text else "none"
    text = text[:_MAX_TEXT]
    return text, {"chars": len(text), "source": source, "status": "complete" if text else "empty"}

def _history_rows(path: Path) -> list[dict[str, Any]]:
    """Newest :data:`_HISTORY_TAIL_ROWS` rows of ``path`` inside the 7-day window."""
    try:
        if not path.is_file() or path.stat().st_size > _HISTORY_MAX_FILE_BYTES:
            return []
    except OSError:
        return []
    tail: deque[str] = deque((line for line in _read_lines(path) or () if line.strip()), maxlen=_HISTORY_TAIL_ROWS)
    cutoff = _utcnow() - dt.timedelta(days=_HISTORY_DAYS)
    rows: list[dict[str, Any]] = []
    for line in tail:
        try:
            row = json.loads(line)
        except Exception:
            continue
        stamp = _parse_ts(row.get("computed_at_utc") or row.get("timestamp") or row.get("ts")) if isinstance(row, dict) else None
        if stamp is not None and stamp >= cutoff:
            rows.append(row)
    return rows

def _sample(rows: list[Any], limit: int) -> list[Any]:
    """Evenly spaced subsequence of ``rows`` from the oldest to the newest row."""
    if len(rows) <= limit:
        return rows
    step = (len(rows) - 1) / (limit - 1)
    return [rows[round(i * step)] for i in range(limit)]

def _numeric_leaves(value: Any, prefix: str = "", depth: int = 0, out: dict[str, float] | None = None) -> dict[str, float]:
    out = {} if out is None else out
    if isinstance(value, dict) and depth < _HISTORY_MAX_DEPTH:
        for key, member in value.items():
            _numeric_leaves(member, f"{prefix}{key}.", depth + 1, out)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out[prefix[:-1]] = round(float(value), 4)
    return out

def history_series(rows: list[dict[str, Any]], samples: int = _HISTORY_SAMPLES) -> dict[str, Any]:
    """Trend view: ``samples`` rows evenly spaced oldest→newest, one series per
    dotted numeric path (depth ≤ 3); paths whose value moved come first."""
    picked = _sample(rows, samples)
    flat = [_numeric_leaves(row) for row in picked]
    series = {path: [row.get(path) for row in flat] for path in sorted({p for row in flat for p in row})}
    moved = sorted(series, key=lambda p: (len({v for v in series[p] if v is not None}) <= 1, p))
    return {"samples": [str(r.get("computed_at_utc") or r.get("timestamp") or r.get("ts")) for r in picked],
            "series": {path: series[path] for path in moved[:_HISTORY_MAX_SERIES]}}

def scorecard_input(state_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(state_root)
    latest = load_json(root / "scorecard" / "latest.json", {})
    if not isinstance(latest, dict) or not latest:
        legacy = load_json(root / "scorecard.json", {})
        latest = legacy if isinstance(legacy, dict) else {}
    history = _history_rows(root / "scorecard" / "history.jsonl")
    if not history:
        archives = sorted((root / "scorecard" / "archive").glob("*.jsonl.gz"))
        if archives:
            history = _history_rows(archives[-1])
    trend = history_series(history)
    status = "empty" if not latest else ("complete" if history else "partial")
    meta = {"latest_keys": len(latest), "history_rows": len(history), "history_samples": len(trend["samples"]), "status": status}
    return {"latest": latest, "history_7d": trend}, meta

def funnel_input(rows: list[dict[str, Any]], futility: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Per-demand-id :data:`FUNNEL_COLUMNS` over ``rows`` (as compact lists,
    newest-proposed id first); keeps the :data:`_FUNNEL_MAX_IDS` most recently
    proposed ids. ``futile``/``attempt_count`` come from the per-gap records of
    ``demand/futility.json`` (keyed by gap id, #996)."""
    counts: dict[str, dict[str, Any]] = {}
    cycle_demands: dict[str, str] = {}
    last_ts: dict[str, str] = {}
    def rec(demand_id: str) -> dict[str, Any]:
        return counts.setdefault(demand_id, {"proposed": 0, "integrated": 0, "self_dedup": 0, "futile": 0, "attempt_count": None})
    for row in rows:
        demand_id = str(row.get("demand_id") or "").strip()
        phase = str(row.get("phase") or "")
        cycle_id = str(row.get("cycle_id") or "").strip()
        if phase == "proposed" and demand_id:
            cycle_demands[cycle_id] = demand_id
            rec(demand_id)["proposed"] += 1
            last_ts[demand_id] = max(last_ts.get(demand_id, ""), str(row.get("ts") or ""))
        elif phase == "proposer_reject" and demand_id and str(row.get("reason")) == "self_dedup":
            rec(demand_id)["self_dedup"] += 1
        elif phase == "outcome" and str(row.get("outcome")) == "success" and cycle_demands.get(cycle_id):
            rec(cycle_demands[cycle_id])["integrated"] += 1
    for gap_id, record in (futility.items() if isinstance(futility, dict) else ()):
        if isinstance(record, dict) and (record.get("futile") is True or str(gap_id) in counts):
            rec(str(gap_id)).update(futile=int(record.get("futile") is True), attempt_count=record.get("attempt_count"))
    ordered = sorted(counts, key=lambda key: last_ts.get(key, ""), reverse=True)[:_FUNNEL_MAX_IDS]
    kept = {key: [counts[key][column] for column in FUNNEL_COLUMNS] for key in ordered}
    meta = {"ids": len(kept), "ids_dropped": len(counts) - len(kept), "status": "complete" if kept else "empty"}
    return {"columns": list(FUNNEL_COLUMNS), "by_demand_id": kept}, meta

def tree_digest(state_root: Path, rows: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Node count, best-path depth, fitness summary over the fields
    ``evolution_tree.record_node`` actually populates, and the outcome mix of
    the nodes' cycles joined from ledger ``outcome`` rows."""
    tree = load_json(Path(state_root) / "evolution" / "tree.json", {})
    tree = tree if isinstance(tree, dict) else {}
    nodes_raw = tree.get("nodes")
    node_map: dict[str, dict[str, Any]] = {}
    if isinstance(nodes_raw, dict):
        node_map = {str(k): v for k, v in nodes_raw.items() if isinstance(v, dict)}
    elif isinstance(nodes_raw, list):
        node_map = {str(n.get("sha") or n.get("id")): n for n in nodes_raw if isinstance(n, dict) and (n.get("sha") or n.get("id"))}
    best_path: list[str] = []
    curr = str(tree.get("current_sha") or "")
    while curr and curr in node_map and len(best_path) < _TREE_BEST_PATH_HOPS:
        best_path.append(curr)
        curr = str(node_map[curr].get("parent_sha") or "")
    fitness: dict[str, list[float]] = {}
    for node in node_map.values():
        for key, value in (node.get("fitness") or {}).items() if isinstance(node.get("fitness"), dict) else ():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                fitness.setdefault(str(key), []).append(float(value))
    rewards = fitness.get("reward", [])
    summary: dict[str, Any] = {"chain_depth": len(best_path), "reward_count": len(rewards),
                               "reward_mean": statistics.fmean(rewards) if rewards else None, "reward_max": max(rewards) if rewards else None}
    for key, values in sorted(fitness.items()):
        if key != "reward":
            summary[key] = {"count": len(values), "mean": round(statistics.fmean(values), 4), "max": max(values)}
    cycle_ids = {str(n.get("cycle_id")) for n in node_map.values() if n.get("cycle_id")}
    outcome_mix: dict[str, int] = {}
    for row in rows or ():
        if row.get("phase") == "outcome" and str(row.get("cycle_id")) in cycle_ids:
            outcome_mix[str(row.get("outcome") or "unknown")] = outcome_mix.get(str(row.get("outcome") or "unknown"), 0) + 1
    stamps = sorted(str(n.get("ts")) for n in node_map.values() if n.get("ts"))
    digest = {"node_count": len(node_map), "current_best_path": best_path, "fitness_summary": summary, "outcome_mix": outcome_mix,
              "switches": len(tree.get("switches") or []), "ts_span": [stamps[0], stamps[-1]] if stamps else None}
    fitness_values = sum(len(v) for v in fitness.values())
    status = "empty" if not node_map else ("complete" if fitness_values else "partial")
    return digest, {"nodes": len(node_map), "fitness_values": fitness_values, "status": status}

def _clip(value: Any) -> str:
    return " ".join(str(value or "").split())[:_INSIGHT_TEXT_CHARS]

def insights_input(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """v2 lesson cards (``problem`` -> ``solution``, filler skipped via
    ``lesson_v2.solution_is_meaningful``), distinct legacy
    ``generalized_insight`` lines, ``errors.yaml`` summaries, plus the two
    index files. The historical ``reusable_insight`` key never existed."""
    from nanobot.runtime.lesson_v2 import bounded_load_yaml, solution_is_meaningful

    root = Path(repo_root)
    cards: list[dict[str, str]] = []
    legacy: list[str] = []
    filler_skipped = 0
    for entry in sorted(bounded_load_yaml(root / "lessons" / "lessons.yaml"), key=lambda e: str(e.get("first_seen") or e.get("date") or ""), reverse=True):
        if "problem" in entry or "solution" in entry:
            if not solution_is_meaningful(entry.get("problem"), entry.get("solution")):
                filler_skipped += 1
            elif len(cards) < _INSIGHTS_V2:
                cards.append({"id": str(entry.get("id") or ""), "problem": _clip(entry.get("problem")),
                              "solution": _clip(entry.get("solution")), "first_seen": str(entry.get("first_seen") or "")})
        elif entry.get("generalized_insight"):
            text = _clip(entry.get("generalized_insight"))
            if text not in legacy and len(legacy) < _INSIGHTS_LEGACY:
                legacy.append(text)
    errors = [{"title": _clip(e.get("title")), "root_cause": _clip(e.get("root_cause")), "prevention": _clip(e.get("prevention"))}
              for e in bounded_load_yaml(root / "lessons" / "errors.yaml")[-_INSIGHTS_ERRORS:]]
    indexes: dict[str, str] = {}
    for rel in ("memory/index.md", "docs/index.md"):
        try:
            indexes[rel] = (root / rel).read_text(encoding="utf-8")[:_INDEX_CHARS]
        except Exception:
            indexes[rel] = ""
    data = {"cards": cards, "legacy_insights": legacy, "errors": errors, "indexes": indexes}
    meta = {"cards": len(cards), "filler_skipped": filler_skipped, "legacy": len(legacy), "errors": len(errors),
            "status": "complete" if cards or legacy or errors else "empty"}
    return data, meta

def empty_inputs(inputs_status: dict[str, Any]) -> list[str]:
    """Names in :data:`INPUT_NAMES` whose status is ``empty``."""
    return [name for name in INPUT_NAMES if (inputs_status.get(name) or {}).get("status") == "empty"]

def should_refuse(inputs_status: dict[str, Any]) -> bool:
    return len(empty_inputs(inputs_status)) > _MAX_EMPTY_INPUTS
