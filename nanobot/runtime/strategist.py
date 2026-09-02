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

from nanobot.runtime import strategist_inputs
from nanobot.runtime.model_registry import resolve_model

LOG = logging.getLogger(__name__)
SCHEMA = "strategist-hadi-v1"
DEFAULT_H = 3
DEFAULT_F = 2
_MAX_SECTION = 8_000
_MAX_PROMPT_CHARS = 48_000
# Size guards for the small point-in-time files this module reads itself
# (watermark, decisions tail, futility sidecar). Archive inputs live in
# strategist_inputs with their own bounds.
_MAX_JSON_BYTES = 256_000
_MAX_LINES_FILE_BYTES = 256_000
# Decision reason when the archive view is too empty to advise on (#1182).
REASON_INPUTS_UNAVAILABLE = "inputs_unavailable"
# Prompt budget: while the payload is over the cap, the largest of these
# sections is halved (goals, the tree digest, the futility sidecar and
# inputs_status are never shrunk); every halving is recorded in inputs_status.
_HALVING_SECTIONS = ("lessons", "prior_decisions", "recent_cycles", "insights", "funnel", "scorecard")
_HALVING_STEPS = 48  # 6 sections x 8 halvings (1/256 each) before the truncated fallback
_HALVING_KEEP = {"columns"}  # table headers inside a section, not data

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
        if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
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
        if os.name != "nt":
            os.chmod(temporary, 0o644)  # mkstemp creates 0600; normalize for non-agent readers (#1096)
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
        if not path.is_file() or path.stat().st_size > _MAX_LINES_FILE_BYTES:
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
    """Tree digest without the ledger outcome join; :func:`collect_inputs`
    uses the joined form from ``strategist_inputs.tree_digest``."""
    return strategist_inputs.tree_digest(Path(state_root))[0]

def collect_inputs(state_root: Path, repo_root: Path) -> dict[str, Any]:
    """Build capped archive inputs with per-input provenance; never enumerate the repo.

    Each archive section comes from ``strategist_inputs`` and reports its
    status under ``inputs_status`` so :func:`run_strategist` can refuse to
    advise on a mostly empty view (#1182). ``recent_cycles`` and
    ``prior_decisions`` are context, not inputs, and carry no status.
    """
    state_root, repo_root = Path(state_root), Path(repo_root)
    goals, goals_meta = strategist_inputs.charter_input(state_root)
    scorecard, scorecard_meta = strategist_inputs.scorecard_input(state_root)
    rows, ledger_meta = strategist_inputs.ledger_rows(state_root)
    futility = _load_json(state_root / "demand" / "futility.json", {})
    funnel, funnel_meta = strategist_inputs.funnel_input(rows, futility)
    tree, tree_meta = strategist_inputs.tree_digest(state_root, rows)
    insights, insights_meta = strategist_inputs.insights_input(repo_root)
    live_ledger = state_root / "ledger" / "cycles.jsonl"
    recent_path = live_ledger if live_ledger.is_file() else state_root / "cycle_ledger.jsonl"
    recent = [json.loads(line) for line in _read_lines(recent_path, 50) if _json_object(line)]
    prior = [json.loads(line) for line in _read_lines(state_root / "strategist" / "decisions.jsonl", 10) if _json_object(line)]
    return {
        "evolution_tree": tree,
        "scorecard": {"latest": _cap(scorecard["latest"], _MAX_SECTION), "history_7d": scorecard["history_7d"]},
        "funnel": funnel,
        "futility": _cap(futility, 2_000),
        "insights": {key: value for key, value in insights.items() if key != "legacy_insights"},
        "goals": goals,
        "lessons": insights["legacy_insights"],  # legacy lessons.yaml rows; v2 cards are insights.cards
        "recent_cycles": recent,
        # a prior row's own inputs_status is provenance, not advice context
        "prior_decisions": [{k: v for k, v in row.items() if k != "inputs_status"} for row in prior],
        "inputs_status": {
            "goals": goals_meta,
            "scorecard": scorecard_meta,
            "funnel": {**funnel_meta, "ledger": ledger_meta},
            "insights": insights_meta,
            "evolution_tree": tree_meta,
            "recent_cycles": len(recent),
            "halved": [],  # filled by build_strategist_prompt when it shrinks sections
        },
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
    watermark = _cap(watermark, 1_000) if isinstance(watermark, dict) else {}
    payload = {"schema": SCHEMA, "watermark": watermark, "archive": inputs, "output": {
        "schema": SCHEMA, "period_reviewed": "ISO period", "hypotheses": [{
            "title": "short title", "hypothesis": "falsifiable claim", "action": "bounded probe",
            "data_to_collect": "measurements", "insight_criterion": "confirm/refute condition", "priority": "medium"
        }], "futility_advisories": [{"topic_or_direction": "direction id", "reason": "one-line reason"}]
    }}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > _MAX_PROMPT_CHARS:
        archive = payload["archive"]
        status = archive.get("inputs_status")
        halved = status.setdefault("halved", []) if isinstance(status, dict) else []
        for _ in range(_HALVING_STEPS):
            if len(encoded) <= _MAX_PROMPT_CHARS:
                break
            sizes = {key: len(json.dumps(archive.get(key), ensure_ascii=False)) for key in _HALVING_SECTIONS}
            label = _halve_section(archive, max(sizes, key=sizes.get))
            if label is None:
                break
            halved.append(label)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > _MAX_PROMPT_CHARS:
            encoded = json.dumps({"schema": SCHEMA, "watermark": _cap(watermark, 200), "archive": {"truncated": True}}, ensure_ascii=False)
        if len(encoded) > _MAX_PROMPT_CHARS:
            encoded = encoded[:_MAX_PROMPT_CHARS]
    return system, encoded

def _halve_section(archive: dict[str, Any], key: str) -> str | None:
    """Halve one prompt section in place. Lists and strings keep their first
    half; a dict halves its largest list/dict/str member (so a one-key section
    such as ``funnel.by_demand_id`` still shrinks, newest ids first). Returns
    the label recorded under ``inputs_status.halved``, ``None`` if nothing
    shrinkable is left."""
    value = archive.get(key)
    if isinstance(value, (list, str)):
        if len(value) <= 1:
            return None
        archive[key] = value[: len(value) // 2]
        return key
    if not isinstance(value, dict):
        return None
    members = [(k, v) for k, v in value.items() if k not in _HALVING_KEEP and isinstance(v, (list, dict, str)) and len(v) > 1]
    if not members:
        return None
    name, member = max(members, key=lambda item: len(json.dumps(item[1], ensure_ascii=False)))
    value[name] = dict(list(member.items())[: len(member) // 2]) if isinstance(member, dict) else member[: len(member) // 2]
    return f"{key}.{name}"

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

def _write_advisories(state_root: Path, advisories: list[dict[str, Any]]) -> bool:
    try:
        _atomic_json(Path(state_root) / "strategist" / "advisories.json", {
            "schema": "strategist-advisories-v1", "updated_at": _now(), "advisories": advisories
        })
        return True
    except Exception:
        return False

def _apply(value: dict[str, Any], state_root: Path, max_h: int, max_f: int) -> dict[str, int]:
    from nanobot.runtime.hypothesis_backlog import append_hypotheses
    hypotheses = value["hypotheses"][:max_h]
    advisories = value["futility_advisories"][:max_f]
    entries = [{**item, "source": "strategist", "created_at": _now()} for item in hypotheses]
    appended = append_hypotheses(state_root, entries)
    if advisories and not _write_advisories(state_root, advisories):
        raise OSError("advisory sidecar write failed")
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
        decision["inputs_status"] = inputs["inputs_status"]
        if strategist_inputs.should_refuse(inputs["inputs_status"]):
            # Class B reader (#1173): advice generated from a mostly empty
            # archive view is worse than none. No LLM call, no writes; the
            # watermark stays so the next run retries against fresh inputs.
            decision.update({"prompt_chars": 0, "reason": REASON_INPUTS_UNAVAILABLE,
                             "empty_inputs": strategist_inputs.empty_inputs(inputs["inputs_status"])})
            _record_decision(state_root, decision)
            return decision
        system, user = build_strategist_prompt(inputs, old)
        decision["prompt_chars"] = len(user)
        raw = (llm or _default_llm)([{"role": "system", "content": system}, {"role": "user", "content": user}], model)
        if hasattr(raw, "content"):
            raw = raw.content
        text = str(raw or "").strip()
        if text.startswith("```"):
            raise ValueError("strict JSON output must not be fenced")
        parsed = json.loads(text)
        if not validate_strategist_output(parsed):
            raise ValueError("invalid strategist-hadi-v1 output")
        max_h = _env_int("SELFEVO_STRATEGIST_MAX_HYPOTHESES", DEFAULT_H)
        max_f = _env_int("SELFEVO_STRATEGIST_MAX_FUTILITY", DEFAULT_F)
        counts = _apply(parsed, state_root, max_h, max_f)
        _atomic_json(Path(state_root) / "strategist" / "watermark.json", {"last_run": decision["timestamp"], "model": model, "total_runs": int(old.get("total_runs", 0)) + 1})
        decision.update({"success": True, "counts": counts, "hypotheses_count": counts["hypotheses_appended"], "advisories_count": counts["advisories_written"], "reason": "valid bounded advisory output applied"})
    except Exception as exc:
        decision.setdefault("prompt_chars", 0)
        decision.update({"error": str(exc)[:500], "reason": "no writes applied; watermark unchanged"})
        try:
            _append_jsonl(Path(state_root) / "strategist" / "errors.jsonl", {"timestamp": decision["timestamp"], "error": str(exc)[:500]})
        except Exception:
            LOG.exception("unable to record strategist error")
    _record_decision(state_root, decision)
    return decision

def _record_decision(state_root: Path, decision: dict[str, Any]) -> None:
    try:
        _append_jsonl(Path(state_root) / "strategist" / "decisions.jsonl", decision)
    except Exception:
        LOG.exception("unable to record strategist decision")

def inputs_report(state_root: Path, repo_root: Path) -> dict[str, Any]:
    """What :func:`run_strategist` would see and decide, without the LLM call
    and without writing anything: the operator's pre-enable check (#1182)."""
    state_root = Path(state_root)
    inputs = collect_inputs(state_root, Path(repo_root))
    _, user = build_strategist_prompt(inputs, load_watermark(state_root))
    status = inputs["inputs_status"]
    return {"timestamp": _now(), "dry_run": True, "inputs_status": status, "prompt_chars": len(user),
            "empty_inputs": strategist_inputs.empty_inputs(status), "would_refuse": strategist_inputs.should_refuse(status)}

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the eeebot strategist (#999)")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="print inputs_status and the refusal verdict; no LLM call, no writes")
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps(inputs_report(args.state_root, args.repo), ensure_ascii=False))
        return 0
    result = run_strategist(args.state_root, args.repo)
    print(json.dumps(result, ensure_ascii=False))
    # Refusing to advise is the input gate working, not a failure: exit 0 so
    # systemd does not mark the timer's service failed; the decisions.jsonl
    # row carries the reason.
    return 0 if result.get("success") or result.get("reason") == REASON_INPUTS_UNAVAILABLE else 1

if __name__ == "__main__":
    raise SystemExit(main())
