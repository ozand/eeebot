"""Decoupled per-cycle transcript reflector (#1007)."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from nanobot.observability.llm_telemetry import call_context, record_llm_call, record_llm_prompt
from nanobot.runtime.model_registry import resolve_model

_MAX_CYCLES = 10
_MAX_RECOMMENDATIONS = 3
_JOURNAL_TAIL = 10
_MAX_TRANSCRIPT_CHARS = 48_000
_MAX_LEDGER_CHARS = 12_000
_MAX_JOURNAL_CHARS = 12_000
_VALID_FINDINGS = {"wasted_steps", "error_pattern", "tool_misuse", "good_practice"}
_VALID_RECOMMENDATIONS = {"skill_candidate", "instruction_change", "approach_hint"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iter_jsonl(path: Path):
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[call-arg]
            for line in fh:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except Exception:
                    continue
    except Exception:
        return


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(_iter_jsonl(path))


def _files(directory: Path, pattern: str) -> list[Path]:
    return sorted([*directory.glob(pattern), *directory.glob(pattern + ".gz")])


def _ledger_rows(state_dir: Path) -> list[dict[str, Any]]:
    directory = state_dir / "ledger"
    rows: list[dict[str, Any]] = []
    for path in [directory / "cycles.jsonl", *_files(directory, "cycles-*.jsonl")]:
        if path.is_file():
            rows.extend(_read_jsonl(path))
    return rows


def _prompt_records(
    state_dir: Path, candidates: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Stream only prompt files near the bounded candidates' ledger dates."""
    directory = state_dir / "llm_calls" / "prompts"
    candidate_ids = {str(row.get("cycle_id") or "") for row in candidates}
    days: set[str] = set()
    for row in candidates:
        try:
            date = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00")).date()
            for offset in (-1, 0, 1):
                days.add((date + timedelta(days=offset)).isoformat())
        except (TypeError, ValueError):
            continue
    latest: dict[str, dict[str, Any]] = {}
    paths = [path for path in _files(directory, "*.jsonl") if path.stem[:10] in days]
    for path in paths:
        for record in _iter_jsonl(path):
            cycle_id = str(record.get("cycle_id") or "").strip()
            if cycle_id not in candidate_ids:
                continue
            sequence = record.get("seq") if isinstance(record.get("seq"), int) else -1
            previous = latest.get(cycle_id)
            if previous is None or sequence >= int(previous.get("seq") or -1):
                latest[cycle_id] = record
    return latest


def _watermark_path(state_dir: Path) -> Path:
    return state_dir / "reflector" / "watermark.json"


def _load_watermark(state_dir: Path) -> str:
    try:
        value = json.loads(_watermark_path(state_dir).read_text(encoding="utf-8"))
        return str(value.get("last_processed") or "") if isinstance(value, dict) else ""
    except Exception:
        return ""


def _save_watermark(state_dir: Path, cycle_id: str) -> None:
    path = _watermark_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".watermark.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"last_processed": cycle_id, "timestamp": _now()}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _append_journal(state_dir: Path, row: dict[str, Any]) -> None:
    path = state_dir / "reflector" / "reflections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _journal_tail(state_dir: Path) -> list[dict[str, Any]]:
    return _read_jsonl(state_dir / "reflector" / "reflections.jsonl")[-_JOURNAL_TAIL:]


def _completed_cycles(rows: list[dict[str, Any]], watermark: str) -> list[dict[str, Any]]:
    outcomes = [row for row in rows if row.get("phase") == "outcome" and row.get("cycle_id")]
    outcomes.sort(key=lambda row: str(row.get("ts") or ""))
    if not watermark:
        return outcomes
    for index, row in enumerate(outcomes):
        if str(row.get("cycle_id")) == watermark:
            return outcomes[index + 1 :]
    return outcomes


def _messages(cycle_id: str, transcript: dict[str, Any], ledger: list[dict[str, Any]], journal: list[dict[str, Any]]) -> list[dict[str, str]]:
    system = (
        "Analyze every message, skill, command, tool call/result, error, retry, and detour in this completed cycle. "
        "Return ONLY strict JSON with keys cycle_id, summary, findings, recommendations, followed_previous, and optional mermaid. "
        "finding kind must be wasted_steps, error_pattern, tool_misuse, or good_practice. "
        "recommendation kind must be skill_candidate, instruction_change, or approach_hint. "
        "Each finding has kind/detail; each recommendation has kind/detail/evidence; return at most three recommendations. "
        "Recommendations are steering only: do not edit files, invent evidence, or claim scorecard value."
    )
    user = (
        f"CYCLE_ID: {cycle_id}\nTRANSCRIPT:\n{json.dumps(transcript, ensure_ascii=False)[:_MAX_TRANSCRIPT_CHARS]}\n"
        f"LEDGER:\n{json.dumps(ledger, ensure_ascii=False)[:_MAX_LEDGER_CHARS]}\n"
        f"PRIOR REFLECTIONS:\n{json.dumps(journal, ensure_ascii=False)[:_MAX_JOURNAL_CHARS]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_output(value: Any, cycle_id: str) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if not isinstance(value, dict) or str(value.get("cycle_id") or "") != cycle_id:
        return None
    if not isinstance(value.get("summary"), str) or not isinstance(value.get("findings"), list) or not isinstance(value.get("recommendations"), list):
        return None
    findings = []
    for item in value["findings"]:
        if not isinstance(item, dict) or item.get("kind") not in _VALID_FINDINGS or not str(item.get("detail") or "").strip():
            return None
        findings.append({"kind": item["kind"], "detail": str(item["detail"])[:1000]})
    recommendations = []
    for item in value["recommendations"][:_MAX_RECOMMENDATIONS]:
        if not isinstance(item, dict) or item.get("kind") not in _VALID_RECOMMENDATIONS or not str(item.get("detail") or "").strip():
            return None
        recommendations.append({"kind": item["kind"], "detail": str(item["detail"])[:1000], "evidence": str(item.get("evidence") or "")[:1000]})
    result = {
        "cycle_id": cycle_id,
        "summary": value["summary"][:2000],
        "findings": findings,
        "recommendations": recommendations,
        "followed_previous": value.get("followed_previous") if isinstance(value.get("followed_previous"), list) else [],
    }
    if isinstance(value.get("mermaid"), str):
        result["mermaid"] = value["mermaid"][:4000]
    return result


def _default_llm(messages: list[dict[str, str]], model: str, cycle_id: str) -> str:
    from openai import OpenAI
    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        raise RuntimeError("litellm credentials not configured")
    started = time.monotonic()
    response = OpenAI(base_url=base_url, api_key=api_key, timeout=120).chat.completions.create(model=model, messages=messages, max_tokens=1600, temperature=0.2)
    choice = response.choices[0]
    content = getattr(getattr(choice, "message", None), "content", "") or ""
    usage_obj = getattr(response, "usage", None)
    usage = {key: int(getattr(usage_obj, key, 0) or 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    with call_context(cycle_id, "reflector"):
        record_llm_call(model=model, duration_ms=(time.monotonic() - started) * 1000, usage=usage, finish_reason=getattr(choice, "finish_reason", ""), retries=0)
        record_llm_prompt(messages=messages, content=content, reasoning_content=None, finish_reason=getattr(choice, "finish_reason", ""), model=model, prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"])
    return content


def run_reflector(state_dir: Path, *, llm: Callable[[list[dict[str, str]], str], Any] | None = None, max_cycles: int = _MAX_CYCLES) -> dict[str, int]:
    state_dir = Path(state_dir)
    rows = _ledger_rows(state_dir)
    candidates = _completed_cycles(rows, _load_watermark(state_dir))[:max(1, int(max_cycles))]
    result = {"candidates": len(candidates), "processed": 0, "skipped_pruned": 0, "errors": 0}
    skipped_ids = {
        str(row.get("cycle_id") or "")
        for row in _read_jsonl(state_dir / "reflector" / "reflections.jsonl")
        if row.get("status") == "skipped_pruned"
    }
    # A previously skipped cycle is eligible again when its retained plain or
    # gzip transcript is now discoverable; only keep the skip terminal when no
    # transcript exists in either archive form.
    result["candidates"] = len(candidates)
    prompts = _prompt_records(state_dir, candidates)
    candidates = [
        row for row in candidates
        if str(row.get("cycle_id") or "") not in skipped_ids
        or str(row.get("cycle_id") or "") in prompts
    ]
    result["candidates"] = len(candidates)
    proposed = {str(row.get("cycle_id")): row for row in rows if row.get("phase") == "proposed"}
    for outcome in candidates:
        cycle_id = str(outcome["cycle_id"])
        transcript = prompts.get(cycle_id)
        if not transcript:
            _append_journal(state_dir, {"cycle_id": cycle_id, "timestamp": _now(), "summary": "Transcript already pruned; cycle skipped.", "findings": [], "recommendations": [], "followed_previous": [], "status": "skipped_pruned"})
            _save_watermark(state_dir, cycle_id)
            result["skipped_pruned"] += 1
            continue
        context_rows = [row for row in rows if str(row.get("cycle_id") or "") == cycle_id]
        if cycle_id in proposed:
            context_rows.insert(0, proposed[cycle_id])
        try:
            messages = _messages(cycle_id, transcript, context_rows, _journal_tail(state_dir))
            model = resolve_model("reflector", strip_openai=True)
            response = llm(messages, model) if llm else _default_llm(messages, model, cycle_id)
            parsed = _parse_output(response, cycle_id)
            if parsed is None:
                raise ValueError("malformed reflector output")
            _append_journal(state_dir, {**parsed, "timestamp": _now()})
            _save_watermark(state_dir, cycle_id)
            result["processed"] += 1
        except Exception as exc:
            _append_journal(state_dir, {"cycle_id": cycle_id, "timestamp": _now(), "summary": "Reflector error; cycle will be retried.", "findings": [], "recommendations": [], "followed_previous": [], "status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]})
            result["errors"] += 1
            break
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Reflect on completed self-evolving cycles")
    parser.add_argument("--state-root", type=Path, default=None)
    args = parser.parse_args()
    state = args.state_root or Path(os.environ.get("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state"))
    print("reflector: " + json.dumps(run_reflector(state), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
