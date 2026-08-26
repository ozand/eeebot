"""Bounded archive review for the strategist role (#999)."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from nanobot.runtime.model_registry import resolve_model

LOG = logging.getLogger(__name__)
SCHEMA = "strategist-hadi-v1"
DEFAULT_H = 3
DEFAULT_F = 2
_MAX_SECTION = 8_000
_MAX_TEXT = 4_000

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

def _cap(value: Any, limit: int = _MAX_SECTION) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return value[:limit]
    if isinstance(value, dict):
        return {str(k): _cap(v, limit) for k, v in list(value.items())[:limit]}
    return value

def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default

def _load_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.is_file() or path.stat().st_size > 256_000:
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

def load_watermark(state_root: Path) -> dict[str, Any]:
    value = _load_json(Path(state_root) / "strategist" / "watermark.json", {})
    return value if isinstance(value, dict) else {}

def save_watermark(state_root: Path, value: dict[str, Any]) -> None:
    _atomic_json(Path(state_root) / "strategist" / "watermark.json", value)

def _read_lines(path: Path, limit: int) -> list[str]:
    try:
        if not path.is_file() or path.stat().st_size > 256_000:
            return []
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as fh:
            lines = []
            for line in fh:
                if line.strip():
                    lines.append(line.strip())
                    if len(lines) > limit:
                        lines.pop(0)
        return lines
    except Exception:
        return []

def _tree_digest(state_root: Path) -> dict[str, Any]:
    tree = _load_json(Path(state_root) / "evolution" / "tree.json", {})
    if not isinstance(tree, dict):
        return {"node_count": 0, "depth": 0, "outcome_mix": {}, "current_best_path": []}
    nodes = tree.get("nodes", [])
    if isinstance(nodes, dict):
        node_map = {str(key): value for key, value in nodes.items() if isinstance(value, dict)}
    elif isinstance(nodes, list):
        node_map = {
            str(node.get("sha") or node.get("id")): node
            for node in nodes
            if isinstance(node, dict) and (node.get("sha") or node.get("id"))
        }
    else:
        node_map = {}
    nodes = list(node_map.values())
    outcomes: dict[str, int] = {}
    best: list[str] = []
    for node in nodes[:500]:
        if not isinstance(node, dict):
            continue
        outcome = str(node.get("outcome") or node.get("status") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if node.get("is_best") or node.get("best"):
            best.append(str(node.get("id") or node.get("sha") or node.get("title") or ""))
    current = str(tree.get("current_sha") or "")
    while current and current in node_map and len(best) < 20:
        best.append(current)
        current = str(node_map[current].get("parent_sha") or "")
    chain_depth = len(best)
    fitness = [node.get("fitness") for node in nodes[:500] if isinstance(node.get("fitness"), dict)]
    rewards = [float(item["reward"]) for item in fitness if isinstance(item.get("reward"), (int, float))]
    return {
        "node_count": len(nodes),
        "current_best_path": best[:20],
        "fitness_summary": {
            "chain_depth": chain_depth,
            "reward_count": len(rewards),
            "reward_mean": sum(rewards) / len(rewards) if rewards else None,
            "reward_max": max(rewards) if rewards else None,
        },
    }

def _scorecard_input(state_root: Path) -> dict[str, Any]:
    latest = _load_json(Path(state_root) / "scorecard" / "latest.json", {})
    if not isinstance(latest, dict) or not latest:
        legacy = _load_json(Path(state_root) / "scorecard.json", {})
        latest = legacy if isinstance(legacy, dict) else {}
    history: list[Any] = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    for line in _read_lines(Path(state_root) / "scorecard" / "history.jsonl", 200):
        try:
            row = json.loads(line)
            stamp = row.get("computed_at_utc") or row.get("timestamp") or row.get("ts")
            parsed = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) if stamp else None
            if parsed is not None and parsed >= cutoff:
                history.append(row)
        except Exception:
            continue
    if isinstance(latest, dict):
        result = dict(latest)
    else:
        result = {}
    result["latest"] = _cap(latest, _MAX_SECTION)
    result["history_7d"] = _cap(history, 100)
    return result

def _funnel_input(state_root: Path) -> dict[str, Any]:
    records: dict[str, dict[str, int]] = {}
    ledger = Path(state_root) / "ledger" / "cycles.jsonl"
    ledger_paths = [ledger, *sorted(ledger.parent.glob("cycles-*.jsonl.gz"))]
    for ledger_path in ledger_paths:
        for line in _read_lines(ledger_path, 500):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            demand_id = str(row.get("demand_id") or "").strip()
            if not demand_id:
                continue
            counts = records.setdefault(demand_id, {"proposed": 0, "integrated": 0, "self_dedup": 0, "futile": 0})
            phase = str(row.get("phase") or "")
            if phase == "proposed":
                counts["proposed"] += 1
            if phase == "outcome" and str(row.get("outcome")) == "success":
                counts["integrated"] += 1
            if phase == "proposer_reject" and str(row.get("reason")) == "self_dedup":
                counts["self_dedup"] += 1
    futile = _load_json(Path(state_root) / "demand" / "futility.json", {})
    futile_ids = set(futile.get("futile_gap_ids", [])) if isinstance(futile, dict) else set()
    for demand_id in futile_ids:
        records.setdefault(str(demand_id), {"proposed": 0, "integrated": 0, "self_dedup": 0, "futile": 0})["futile"] = 1
    return {"by_demand_id": _cap(records, 200), "futility_sidecar": _cap(futile, 2_000)}

def _insight_input(repo_root: Path) -> dict[str, Any]:
    reusable: list[str] = []
    for path in sorted((Path(repo_root) / "lessons").glob("*.yaml")):
        reusable.extend(line[:500] for line in _read_lines(path, 200) if "reusable_insight" in line)
    indexes = {}
    for rel in ("memory/index.md", "docs/index.md"):
        path = Path(repo_root) / rel
        try:
            indexes[rel] = path.read_text(encoding="utf-8")[:_MAX_TEXT]
        except Exception:
            indexes[rel] = ""
    return {"reusable_insights": reusable[-30:], "indexes": indexes}

def collect_inputs(state_root: Path, repo_root: Path) -> dict[str, Any]:
    """Build capped archive inputs; never enumerate the repo."""
    goals = ""
    try:
        goals = (Path(repo_root) / "goals.md").read_text(encoding="utf-8")[:_MAX_TEXT]
    except Exception:
        pass
    scorecard = _scorecard_input(state_root)
    funnel = _funnel_input(state_root)
    insight_data = _insight_input(repo_root)
    return {
        "evolution_tree": _tree_digest(state_root),
        "scorecard": scorecard,
        "funnel": funnel,
        "futility": funnel.get("futility_sidecar", {}),
        "insights": insight_data,
        "goal_charter": goals,
        "goals": goals,
        "lessons": insight_data.get("reusable_insights", []) + _read_lines(Path(repo_root) / "lessons" / "lessons.yaml", 30),
        "recent_cycles": [json.loads(line) for line in _read_lines(
            Path(state_root) / "ledger" / "cycles.jsonl" if (Path(state_root) / "ledger" / "cycles.jsonl").is_file() else Path(state_root) / "cycle_ledger.jsonl", 50
        ) if _json_object(line)],
        "prior_decisions": [json.loads(line) for line in _read_lines(Path(state_root) / "strategist" / "decisions.jsonl", 10) if _json_object(line)],
    }

def _json_object(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except Exception:
        return False

def build_strategist_prompt(inputs: dict[str, Any], watermark: dict[str, Any]) -> tuple[str, str]:
    system = (
        "You are the eeebot strategist role. Review the bounded archive summary and return STRICT JSON only. "
        "Use HADI: each hypothesis must be falsifiable, have a bounded action, data_to_collect, and an "
        "insight_criterion stating what confirms or refutes it. Include no prose outside JSON. "
        "Use this TRIZ lens: identify where improving one tracked metric degrades another or is caused by "
        "the system's own activity, and prefer hypotheses that dissolve the contradiction rather than push the metric."
    )
    payload = {"schema": SCHEMA, "watermark": watermark, "archive": inputs, "output": {
        "schema": SCHEMA, "period_reviewed": "ISO period", "hypotheses": [{
            "title": "short title", "hypothesis": "falsifiable claim", "action": "bounded probe",
            "data_to_collect": "measurements", "insight_criterion": "confirm/refute condition", "priority": "medium"
        }], "futility_advisories": [{"topic_or_direction": "direction id", "reason": "one-line reason"}]
    }}
    return system, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

def _text_field(value: Any, limit: int = 1_000) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit

def validate_strategist_output(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"schema", "period_reviewed", "hypotheses", "futility_advisories"}:
        return False
    if value["schema"] != SCHEMA or not _text_field(value["period_reviewed"], 200):
        return False
    hypotheses, advisories = value["hypotheses"], value["futility_advisories"]
    if not isinstance(hypotheses, list) or not isinstance(advisories, list):
        return False
    for item in hypotheses:
        if not isinstance(item, dict) or not {"title", "hypothesis", "action", "data_to_collect", "insight_criterion"}.issubset(item):
            return False
        if set(item) - {"title", "hypothesis", "action", "data_to_collect", "insight_criterion", "priority"}:
            return False
        if not all(_text_field(item.get(key)) for key in ("title", "hypothesis", "action", "data_to_collect", "insight_criterion")):
            return False
        if "priority" in item and item["priority"] not in {"high", "medium", "low"}:
            return False
    return all(
        isinstance(item, dict)
        and {"topic_or_direction", "reason"}.issubset(item)
        and not (set(item) - {"topic_or_direction", "reason", "evidence", "confidence"})
        and _text_field(item.get("topic_or_direction"))
        and _text_field(item.get("reason"), 500)
        for item in advisories
    )

def _write_advisories(state_root: Path, advisories: list[dict[str, Any]]) -> None:
    _atomic_json(Path(state_root) / "strategist" / "advisories.json", {
        "schema": "strategist-advisories-v1", "updated_at": _now(), "advisories": advisories
    })

def _apply(value: dict[str, Any], state_root: Path, max_h: int, max_f: int) -> dict[str, int]:
    from nanobot.runtime.hypothesis_backlog import append_hypotheses
    hypotheses = value["hypotheses"][:max_h]
    advisories = value["futility_advisories"][:max_f]
    entries = [{**item, "source": "strategist", "created_at": _now()} for item in hypotheses]
    appended = append_hypotheses(state_root, entries)
    if advisories:
        _write_advisories(state_root, advisories)
    return {"hypotheses_appended": appended, "advisories_written": len(advisories), "advisories_recorded": len(advisories)}

def _default_llm(messages: list[dict[str, str]], model: str) -> str:
    from openai import OpenAI

    from nanobot.observability.llm_telemetry import call_context, record_llm_call, record_llm_prompt
    base_url, api_key = os.environ.get("LITELLM_BASE_URL", "").strip(), os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        raise RuntimeError("litellm credentials not configured; check the unit EnvironmentFile chain")
    started = time.monotonic()
    response = OpenAI(base_url=base_url, api_key=api_key, timeout=120).chat.completions.create(
        model=model, messages=messages, max_tokens=2_000, temperature=0.2
    )
    choice = response.choices[0]
    content = getattr(getattr(choice, "message", None), "content", "") or ""
    usage_obj = getattr(response, "usage", None)
    usage = {key: int(getattr(usage_obj, key, 0) or 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    with call_context(None, "strategist"):
        record_llm_call(model=model, duration_ms=(time.monotonic() - started) * 1000, usage=usage,
                        finish_reason=getattr(choice, "finish_reason", ""), retries=0)
        record_llm_prompt(messages=messages, content=content, reasoning_content=None,
                          finish_reason=getattr(choice, "finish_reason", ""), model=model,
                          prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"])
    return content

def run_strategist(state_root: Path, repo_root: Path, llm: Callable[[list[dict[str, str]], str], Any] | None = None) -> dict[str, Any]:
    state_root, repo_root = Path(state_root), Path(repo_root)
    old = load_watermark(state_root)
    model = resolve_model("strategist", strip_openai=True)
    decision: dict[str, Any] = {"timestamp": _now(), "model": model, "success": False}
    try:
        inputs = collect_inputs(state_root, repo_root)
        system, user = build_strategist_prompt(inputs, old)
        raw = (llm or _default_llm)([{"role": "system", "content": system}, {"role": "user", "content": user}], model)
        if hasattr(raw, "content"):
            raw = raw.content
        text = str(raw or "").strip()
        if text.startswith("```"):
            raise ValueError("strict JSON output must not be fenced")
        parsed = json.loads(text)
        if not validate_strategist_output(parsed):
            raise ValueError("invalid strategist-hadi-v1 output")
        counts = _apply(parsed, state_root, _env_int("SELFEVO_STRATEGIST_MAX_HYPOTHESES", DEFAULT_H), _env_int("SELFEVO_STRATEGIST_MAX_FUTILITY", DEFAULT_F))
        _atomic_json(Path(state_root) / "strategist" / "watermark.json", {"last_run": decision["timestamp"], "model": model, "total_runs": int(old.get("total_runs", 0)) + 1})
        decision.update({"success": True, "counts": counts, "hypotheses_count": counts["hypotheses_appended"], "advisories_count": counts["advisories_written"], "reason": "valid bounded advisory output applied"})
    except Exception as exc:
        decision.update({"error": str(exc)[:500], "reason": "no writes applied; watermark unchanged"})
        try:
            _append_jsonl(Path(state_root) / "strategist" / "errors.jsonl", {"timestamp": decision["timestamp"], "error": str(exc)[:500]})
        except Exception:
            LOG.exception("unable to record strategist error")
    try:
        _append_jsonl(Path(state_root) / "strategist" / "decisions.jsonl", decision)
    except Exception:
        LOG.exception("unable to record strategist decision")
    return decision

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the eeebot strategist (#999)")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    result = run_strategist(parser.parse_args().state_root, parser.parse_args().repo)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1

if __name__ == "__main__":
    raise SystemExit(main())
