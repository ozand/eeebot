"""Trusted, bounded A/B evaluation of instance-authored skills (#941).

``skills/<name>/evals/evals.json`` is only a test plan: it is instance-authored
and therefore steering input, never fitness. The parent harness runs each case
with and without the named skill and writes only its own mechanical verdict to
a protected sidecar. The sidecar is rewritten atomically at the end of a run;
rows a child writes while running are discarded by that rewrite, following the
validator-harness parent-owned pattern. A detached child can still write after
the final rewrite, so the next run rewrites the file again.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ENABLED_ENV = "SELFEVO_SKILL_EVAL_ENABLED"
SIDECAR_REL = "skill_fitness/evals.jsonl"
MAX_EVAL_BYTES = 256_000
MAX_CASES = 20
MAX_RUN_SECONDS = 240.0
MAX_CASE_SECONDS = 30.0
MAX_WEEKLY_RUNS = 10
SCHEMA = "skill-evals-v1"

Runner = Callable[[str, bool, Path, float], dict[str, Any]]


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "0").strip() == "1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _git_sha(repo: Path, rel: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H", "--", rel],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _sidecar_path(state_dir: Path) -> Path:
    return Path(state_dir) / SIDECAR_REL


def _load_rows(state_dir: Path) -> list[dict[str, Any]]:
    try:
        path = _sidecar_path(state_dir)
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("schema") == SCHEMA and row.get("eval_id"):
                    rows.append(row)
            except Exception:
                continue
        return rows[-MAX_WEEKLY_RUNS * MAX_CASES :]
    except Exception:
        return []


def _rewrite_rows(state_dir: Path, prior: list[dict[str, Any]], current: list[dict[str, Any]]) -> None:
    """Parent-only atomic rewrite; never append child-controlled rows."""
    try:
        rows = (prior + current)[-MAX_WEEKLY_RUNS * MAX_CASES :]
        path = _sidecar_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def load_cases(repo: Path, skill: str) -> list[dict[str, Any]] | None:
    """Validate and return an eval plan, or ``None`` (fail closed)."""
    try:
        path = Path(repo) / "skills" / skill / "evals" / "evals.json"
        if not path.is_file() or path.stat().st_size > MAX_EVAL_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases = raw.get("evals") if isinstance(raw, dict) else raw
        if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
            return None
        valid: list[dict[str, Any]] = []
        for case in cases:
            if not isinstance(case, dict):
                return None
            if not all(key in case for key in ("id", "prompt", "expected_output", "assertions")):
                return None
            if not isinstance(case["id"], str) or not case["id"].strip():
                return None
            if not isinstance(case["prompt"], str) or not isinstance(case["expected_output"], str):
                return None
            if not isinstance(case["assertions"], list) or not all(isinstance(x, str) for x in case["assertions"]):
                return None
            valid.append(case)
        return valid
    except Exception:
        return None


def _default_runner(prompt: str, with_skill: bool, skill_path: Path, timeout: float) -> dict[str, Any]:
    """No implicit model call: production enables this only with an injected runner."""
    return {"output": "", "tokens": 0, "duration": 0.0, "error": "runner_not_configured"}


def _grade(case: dict[str, Any], result: dict[str, Any]) -> bool:
    output = str(result.get("output") or "")
    expected = str(case["expected_output"])
    if expected not in output:
        return False
    return all(assertion in output for assertion in case["assertions"])


def _weekly_runs(rows: list[dict[str, Any]], now: datetime) -> int:
    cutoff = now - timedelta(days=7)
    return len({str(r.get("run_id")) for r in rows if _parse(r.get("ts")) and _parse(r.get("ts")) >= cutoff})


def evaluate_skill(
    state_dir: Path,
    repo: Path,
    skill: str,
    *,
    runner: Runner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one bounded A/B batch. Returns a non-fitness diagnostic result when off."""
    now = now or _now()
    result: dict[str, Any] = {"skill": skill, "ran": False, "reason": "disabled", "cases": []}
    if not enabled():
        return result
    cases = load_cases(repo, skill)
    if cases is None:
        result["reason"] = "invalid_eval_plan"
        return result
    skill_path = Path(repo) / "skills" / skill / "SKILL.md"
    commit = _git_sha(Path(repo), f"skills/{skill}/SKILL.md")
    prior = _load_rows(Path(state_dir))
    if any(r.get("skill") == skill and r.get("skill_commit") == commit for r in prior):
        result["reason"] = "unchanged"
        return result
    if _weekly_runs(prior, now) >= MAX_WEEKLY_RUNS:
        result["reason"] = "weekly_cap"
        return result
    run_id = hashlib.sha256(f"{skill}:{commit}:{_iso(now)}".encode()).hexdigest()[:16]
    fn = runner or _default_runner
    started = time.monotonic()
    current: list[dict[str, Any]] = []
    try:
        for case in cases:
            if time.monotonic() - started >= MAX_RUN_SECONDS:
                break
            pair: dict[str, Any] = {"id": case["id"]}
            for mode in (False, True):
                call_started = time.monotonic()
                try:
                    raw = fn(case["prompt"], mode, skill_path, MAX_CASE_SECONDS)
                    raw = raw if isinstance(raw, dict) else {"output": str(raw)}
                except Exception as exc:
                    raw = {"output": "", "error": type(exc).__name__}
                duration = float(raw.get("duration", time.monotonic() - call_started) or 0.0)
                pair["with" if mode else "without"] = {
                    "pass": _grade(case, raw),
                    "tokens": int(raw.get("tokens", 0) or 0),
                    "duration": round(duration, 3),
                }
            pair["delta"] = int(pair["with"]["pass"]) - int(pair["without"]["pass"])
            current.append({"schema": SCHEMA, "run_id": run_id, "skill": skill, "skill_commit": commit, "eval_id": case["id"], "ts": _iso(now), **pair})
        _rewrite_rows(Path(state_dir), prior, current)
    except Exception:
        _rewrite_rows(Path(state_dir), prior, current)
    result.update({"ran": bool(current), "reason": "ok", "run_id": run_id, "cases": current})
    return result


def fitness_rows(state_dir: Path, skill: str | None = None) -> list[dict[str, Any]]:
    rows = _load_rows(Path(state_dir))
    return [r for r in rows if skill is None or r.get("skill") == skill]


def negative_delta_demand(state_dir: Path, *, limit: int = 1) -> list[dict[str, str]]:
    """Return bounded defect-shaped items for skills whose measured delta is negative."""
    out: list[dict[str, str]] = []
    for skill in sorted({str(r.get("skill")) for r in _load_rows(Path(state_dir))}):
        rows = [r for r in _load_rows(Path(state_dir),) if r.get("skill") == skill]
        if rows and any(int(r.get("delta", 0)) < 0 for r in rows):
            digest = hashlib.sha256(f"skill-eval:{skill}".encode()).hexdigest()[:12]
            out.append({"kind": "defect", "id": f"skill-eval-{digest}", "summary": f"skill fails its own evals: {skill}", "evidence": "harness-measured with/without delta is negative", "affected_path": f"skills/{skill}"})
            if len(out) >= limit:
                break
    return out
