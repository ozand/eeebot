"""Knowledge-lift harness (#1093): measures the incremental value of knowledge context.

Runs matched A/B evaluations of tasks with knowledge context (lessons card +
reflection hints) vs stripped knowledge context against an executor runner.
Results land in a parent-owned sidecar protected by FITNESS_SIDECARS.
Protected against tampering via atomic rewrite-at-exit.
Budget-gated by weekly cap, knowledge corpus digest watermark, per-run timeout,
and global kill-switch env (SELFEVO_KNOWLEDGE_LIFT_ENABLED, default OFF).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Environment kill-switch (default OFF)
ENV_ENABLED = "SELFEVO_KNOWLEDGE_LIFT_ENABLED"

# Sidecar paths relative to state_dir
SIDECAR_REL = "knowledge_lift/evals.jsonl"
WATERMARK_REL = "knowledge_lift/watermark.json"

# Operational constraints & sizing
MAX_FILE_BYTES = 50_000
MAX_CASES_PER_SET = 20
MAX_ASSERTIONS_PER_CASE = 10
MAX_WEEKLY_RUNS = 20
DEFAULT_CASE_TIMEOUT_S = 30.0
DEFAULT_TOTAL_TIMEOUT_S = 120.0
_MAX_HISTORY_ROWS = 500
_MAX_RUN_SECONDS = 120.0

_RESERVED_KEYS = {
    "case_id",
    "prompt",
    "assertions",
    "timeout_seconds",
}

_ALLOWED_ASSERTIONS = {
    "contains",
    "not_contains",
    "exit_code_zero",
}


def is_enabled() -> bool:
    """Return True only if SELFEVO_KNOWLEDGE_LIFT_ENABLED is set to truthy."""
    val = os.environ.get(ENV_ENABLED, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def validate_eval_plan(raw: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Schema-validate a knowledge eval set.

    Fails closed: returns ([], error_message) on any malformed or oversized
    input.
    """
    if not isinstance(raw, dict):
        return [], "eval plan must be a JSON object"

    cases = raw.get("cases")
    if not isinstance(cases, list):
        return [], "eval plan must contain a 'cases' list"

    if len(cases) == 0:
        return [], "cases list is empty"

    if len(cases) > MAX_CASES_PER_SET:
        return [], f"cases list exceeds maximum of {MAX_CASES_PER_SET}"

    validated_cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for idx, c in enumerate(cases):
        if not isinstance(c, dict):
            return [], f"case[{idx}] must be an object"

        case_id = c.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            return [], f"case[{idx}] missing valid 'case_id'"
        case_id = case_id.strip()
        if case_id in seen_ids:
            return [], f"duplicate case_id: {case_id}"
        seen_ids.add(case_id)

        prompt = c.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return [], f"case[{case_id}] missing valid 'prompt'"

        assertions = c.get("assertions")
        if not isinstance(assertions, list) or len(assertions) == 0:
            return [], f"case[{case_id}] must have non-empty 'assertions' list"
        if len(assertions) > MAX_ASSERTIONS_PER_CASE:
            return [], f"case[{case_id}] exceeds max assertions ({MAX_ASSERTIONS_PER_CASE})"

        valid_assertions: list[dict[str, Any]] = []
        for a_idx, a in enumerate(assertions):
            if not isinstance(a, dict):
                return [], f"case[{case_id}] assertion[{a_idx}] must be an object"
            atype = a.get("type")
            if atype not in _ALLOWED_ASSERTIONS:
                return [], f"case[{case_id}] assertion[{a_idx}] unknown type: {atype}"
            if atype in ("contains", "not_contains"):
                val = a.get("value")
                if not isinstance(val, str) or not val:
                    return [], f"case[{case_id}] assertion[{a_idx}] missing 'value'"
            valid_assertions.append(a)

        timeout_s = c.get("timeout_seconds", DEFAULT_CASE_TIMEOUT_S)
        if not isinstance(timeout_s, (int, float)) or not (0 < timeout_s <= DEFAULT_CASE_TIMEOUT_S):
            return [], f"case[{case_id}] timeout exceeds maximum"

        validated_cases.append({
            "case_id": case_id,
            "prompt": prompt,
            "assertions": valid_assertions,
            "timeout_seconds": float(timeout_s),
        })

    return validated_cases, None


def compute_knowledge_digest(repo_or_state: Path) -> str:
    """Digest only the bounded files that feed production knowledge context."""
    hasher = hashlib.sha256()
    try:
        root = Path(repo_or_state)
        paths = [root / "lessons" / "lessons.yaml", root / "lessons" / "errors.yaml"]
        paths.append(root / "reflector" / "reflections.jsonl")
        for path in paths:
            try:
                if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                    continue
                hasher.update(path.name.encode("utf-8"))
                hasher.update(path.read_bytes())
            except Exception:
                continue
    except Exception:
        pass
    return hasher.hexdigest()


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    )
    try:
        json.dump(data, temp_file, indent=2, ensure_ascii=False)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_file.close()
        os.replace(temp_file.name, path)
    except Exception:
        if os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except Exception:
                pass
        raise


def _row_well_formed(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("case_id"), str)
        and bool(row.get("case_id"))
        and isinstance(row.get("with_pass"), bool)
        and isinstance(row.get("without_pass"), bool)
        and isinstance(row.get("delta_pass"), int)
        and isinstance(row.get("delta_tokens"), int)
        and isinstance(row.get("ts"), str)
    )


def _read_eval_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES * 40:
            return rows
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if _row_well_formed(row):
                    rows.append(row)
    except Exception:
        pass
    return rows[-_MAX_HISTORY_ROWS:]


def _atomic_write_eval_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    )
    try:
        # Keep tail rows within limit
        for row in rows[-_MAX_HISTORY_ROWS:]:
            temp_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_file.close()
        os.replace(temp_file.name, path)
    except Exception:
        if os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except Exception:
                pass
        raise


def _check_assertions(assertions: list[dict[str, Any]], result: dict[str, Any]) -> bool:
    output = str(result.get("output", ""))
    exit_code = result.get("exit_code", 0)
    for a in assertions:
        atype = a.get("type")
        val = str(a.get("value", ""))
        if atype == "contains" and val not in output:
            return False
        if atype == "not_contains" and val in output:
            return False
        if atype == "exit_code_zero" and exit_code != 0:
            return False
    return True


def _bounded_runner_call(
    runner: Callable[[str, bool, float], dict[str, Any]],
    prompt: str,
    with_knowledge: bool,
    timeout_s: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def invoke() -> None:
        try:
            raw = runner(prompt, with_knowledge, timeout_s)
            result.update(raw if isinstance(raw, dict) else {"output": str(raw)})
        except Exception as exc:
            result.update({"output": "", "exit_code": 1, "tokens": 0, "error": type(exc).__name__})

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    thread.join(min(timeout_s, DEFAULT_CASE_TIMEOUT_S))
    if thread.is_alive():
        return {"output": "", "exit_code": 1, "tokens": 0, "error": "timeout"}
    return result or {"output": "", "exit_code": 1, "tokens": 0, "error": "empty_result"}


def run_eval_case(
    case: dict[str, Any],
    runner: Callable[[str, bool, float], dict[str, Any]],
    prompt_builder: Callable[[str, bool], str] | None = None,
) -> dict[str, Any]:
    """Run a single test case under A (with knowledge) and B (without knowledge).

    runner signature: (prompt, with_knowledge, timeout_s) -> {output: str, exit_code: int, tokens: int, error?: str}
    """
    case_id = case["case_id"]
    prompt = case["prompt"]
    assertions = case["assertions"]
    timeout_s = case["timeout_seconds"]

    # Run with knowledge
    t0 = time.monotonic()
    try:
        res_with = _bounded_runner_call(runner, prompt_builder(prompt, True) if prompt_builder else prompt, True, timeout_s)
    except Exception as e:
        res_with = {"output": "", "exit_code": 1, "tokens": 0, "error": str(e)}
    dur_with = round(time.monotonic() - t0, 3)
    pass_with = (res_with.get("error") is None) and _check_assertions(assertions, res_with)
    tokens_with = int(res_with.get("tokens", 0) or 0)

    # Run without knowledge
    t0 = time.monotonic()
    try:
        res_without = _bounded_runner_call(runner, prompt_builder(prompt, False) if prompt_builder else prompt, False, timeout_s)
    except Exception as e:
        res_without = {"output": "", "exit_code": 1, "tokens": 0, "error": str(e)}
    dur_without = round(time.monotonic() - t0, 3)
    pass_without = (res_without.get("error") is None) and _check_assertions(assertions, res_without)
    tokens_without = int(res_without.get("tokens", 0) or 0)

    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "case_id": case_id,
        "with_pass": pass_with,
        "without_pass": pass_without,
        "with_tokens": tokens_with,
        "without_tokens": tokens_without,
        "with_duration_s": dur_with,
        "without_duration_s": dur_without,
        "delta_pass": 1 if (pass_with and not pass_without) else (-1 if (not pass_with and pass_without) else 0),
        "delta_tokens": tokens_with - tokens_without,
        "ts": now_iso,
    }


def execute_knowledge_lift(
    state_dir: Path,
    eval_plan_raw: dict[str, Any],
    *,
    runner: Callable[[str, bool, float], dict[str, Any]],
    selfevo_repo: Path | None = None,
    prompt_builder: Callable[[str, bool], str] | None = None,
    total_timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Execute knowledge lift evaluation if enabled and within budget limits."""
    if not is_enabled() and not force:
        return {"status": "disabled", "rows_written": 0}

    cases, err = validate_eval_plan(eval_plan_raw)
    if err or not cases:
        return {"status": "rejected", "error": err or "invalid plan", "rows_written": 0}

    state_dir = Path(state_dir)
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    one_week_ago_ts = (now - timedelta(days=7)).timestamp()

    watermark_path = state_dir / WATERMARK_REL
    watermark = _read_json(watermark_path, {}) or {}

    # Check weekly cap
    run_history = watermark.get("runs", [])
    recent_runs = [t for t in run_history if isinstance(t, (int, float)) and t >= one_week_ago_ts]
    if len(recent_runs) >= MAX_WEEKLY_RUNS and not force:
        return {"status": "rate_limited", "reason": "weekly_cap_exceeded", "rows_written": 0}

    # Check knowledge corpus digest watermark
    current_digest = compute_knowledge_digest(selfevo_repo or state_dir)
    last_digest = watermark.get("knowledge_digest")
    if last_digest == current_digest and not force:
        return {"status": "watermarked", "reason": "knowledge_unchanged", "rows_written": 0}

    # Parent atomic load of existing sidecar rows
    sidecar_path = state_dir / SIDECAR_REL
    existing_rows = _read_eval_rows(sidecar_path)

    new_rows: list[dict[str, Any]] = []
    start_total = time.monotonic()
    total_timeout_s = min(max(float(total_timeout_s), 0.0), _MAX_RUN_SECONDS)

    for c in cases:
        if (time.monotonic() - start_total) >= total_timeout_s:
            break
        row = run_eval_case(c, runner, prompt_builder=prompt_builder)
        new_rows.append(row)

    if not new_rows:
        return {"status": "timeout_before_runs", "rows_written": 0}

    # Parent atomic rewrite of sidecar: existing + new rows
    combined_rows = existing_rows + new_rows
    _atomic_write_eval_rows(sidecar_path, combined_rows)

    # Update watermark atomically
    recent_runs.append(now_ts)
    watermark["runs"] = recent_runs
    watermark["knowledge_digest"] = current_digest
    watermark["last_run_utc"] = now.isoformat()
    _atomic_write_json(watermark_path, watermark)

    return {
        "status": "completed",
        "rows_written": len(new_rows),
        "total_rows": len(combined_rows),
    }


def read_knowledge_lift_summary(state_dir: Path) -> dict[str, Any]:
    """Read knowledge lift summary metrics from sidecar (reporting-only)."""
    sidecar_path = Path(state_dir) / SIDECAR_REL
    rows = _read_eval_rows(sidecar_path)
    if not rows:
        return {
            "total_evals": 0,
            "pass_lift": 0,
            "token_lift_avg": 0.0,
            "net_benefit": True,
            "latest_ts": None,
        }

    total = len(rows)
    pass_lift = sum(int(r.get("delta_pass", 0) or 0) for r in rows)
    token_diffs = [int(r.get("delta_tokens", 0) or 0) for r in rows]
    avg_token_diff = round(sum(token_diffs) / total, 1) if total > 0 else 0.0
    latest_ts = rows[-1].get("ts")

    # Net benefit is negative if lift is strictly negative
    net_benefit = pass_lift >= 0

    return {
        "total_evals": total,
        "pass_lift": pass_lift,
        "token_lift_avg": avg_token_diff,
        "net_benefit": net_benefit,
        "latest_ts": latest_ts,
    }


def negative_delta_demand(state_dir: Path, *, limit: int | None = 1) -> list[dict[str, str]]:
    """Return negative lift findings as defect demand items if knowledge hurts."""
    summary = read_knowledge_lift_summary(state_dir)
    if summary.get("total_evals", 0) == 0 or summary.get("net_benefit", True):
        return []

    pass_lift = summary.get("pass_lift", 0)
    token_avg = summary.get("token_lift_avg", 0.0)

    evidence = (
        f"Knowledge lift A/B evaluation shows negative impact: "
        f"net pass delta {pass_lift} across {summary.get('total_evals')} cases, "
        f"average token overhead {token_avg}. Knowledge context may be degrading execution."
    )
    item = {
        "summary": "Knowledge context negative lift detected in A/B evaluations",
        "evidence": evidence,
        "affected_path": "nanobot/runtime/reflection_context.py",
    }
    return [item] if limit is None else [item][:limit]
