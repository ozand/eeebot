"""Harness-run A/B skill evals — measured with/without delta as skill fitness (#941).

``skills/<name>/evals/evals.json`` is instance-authored and therefore only a
test PLAN: it steers what to test, it is never fitness (same trust split as
the validator harness, #925/#928, and benchmark evidence, #813/#819 —
instance numbers are claims; harness measurements are facts). The parent
harness runs each eval case twice — once with the skill's SKILL.md loaded
into the executor context, once without, same prompt — grades both outputs
MECHANICALLY (substring assertions computed by this module's own code, never
by anything the instance wrote), and writes only its own verdict rows into a
protected sidecar.

Trust boundaries (do not weaken):

- The sidecar (:data:`SIDECAR_REL`) is a member of
  ``scorecard.FITNESS_SIDECARS`` (#789 spawn-boundary tamper detection), and
  it is rewritten ATOMICALLY by the parent at the end of every run from
  ``parent-loaded prior rows + parent-graded current rows`` — anything a
  child process appends mid-run is discarded by that rewrite (the
  validator-harness parent-owned pattern). Rows missing the parent's schema
  or an ``eval_id`` are dropped on every load, so a crude forged append never
  survives a single subsequent run either.
- This module is in ``runtime_deny._RUNTIME_DENY_ALWAYS_FILES``: the instance
  may author eval plans, never the grader.
- Assertions prefixed ``llm:`` are ADVISORY ONLY — recorded verbatim on the
  row for a human/LLM reviewer, never part of the mechanical pass verdict
  (the issue's "LLM-judged assertions recorded as advisory only").

Budget gates (all enforced before any executor call):

- Kill-switch env :data:`ENABLED_ENV`, DEFAULT OFF — with the env unset the
  module runs nothing and writes nothing.
- Content watermark (``skill_evals/watermark.json``, same
  changed-since-last-run pacing intent as the #893 tech-tree integration
  watermark): a skill whose ``SKILL.md`` + ``evals.json`` bytes are unchanged
  since its last completed run is never re-run; a changed skill runs at most
  once per change. The watermark keys on a sha256 CONTENT digest rather than
  a git sha so it also works on hosts/tests without git provenance; the
  skill's last-edit git sha is still recorded on every row for provenance.
- Hard weekly cap (:data:`MAX_WEEKLY_RUNS` completed runs per rolling 7
  days, tracked in the watermark file) and a total wall-clock budget per
  ``evaluate_skill`` call (:data:`MAX_RUN_SECONDS`).

Timeout discipline: every executor call is confined to
:data:`MAX_CASE_SECONDS` (or the remaining run budget, whichever is smaller)
by running it on a daemon thread and abandoning it on expiry — a hung eval
therefore costs at most one bounded wait and can never stall the caller (the
abandoned daemon thread cannot block process exit). The timeout is also
passed to the runner so a well-behaved runner can bound its own client call.

The default runner is a single bounded chat completion against the operator's
LiteLLM endpoint (model role ``harness`` — the local qwen executor), with the
skill content prepended to the system prompt for the with-skill arm. No
credentials configured -> every call degrades to an error row (both arms fail,
delta 0, no demand noise) — the module stays cost-free until the operator
wires the env.

Consumption: ``demand._skill_eval_defect_items`` turns a negative measured
delta ("skill fails its own evals") or a pure token cost with no pass gain
("skill costs more than it buys") into ONE bounded ``defect`` demand item per
skill, reading only this module's :func:`negative_delta_demand` over the
protected sidecar. A passing delta produces no demand and simply stands as
corroborating rows in the sidecar for fitness consumers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time

try:  # POSIX-only; the eeepc host is always Linux (same guard as bridge.py)
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX dev hosts
    fcntl = None  # type: ignore[assignment]
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ENABLED_ENV = "SELFEVO_SKILL_EVAL_ENABLED"
SIDECAR_REL = "skill_fitness/evals.jsonl"
WATERMARK_REL = "skill_evals/watermark.json"
MAX_EVAL_BYTES = 256_000
MAX_CASES = 20
MAX_RUN_SECONDS = 240.0
MAX_CASE_SECONDS = 30.0
MAX_INVOCATION_SECONDS = 600.0
MAX_WEEKLY_RUNS = 10
MAX_SIDECAR_BYTES = 2_000_000
MAX_SKILL_TEXT_CHARS = 16_000
MAX_PROMPT_CHARS = 8_000
_JOIN_GRACE_SECONDS = 0.5
_ADVISORY_PREFIX = "llm:"
SCHEMA = "skill-evals-v1"

# runner(prompt, with_skill, skill_md_path, timeout_seconds) -> {"output": str,
# "tokens": int, "duration": float} (extra keys ignored; "error" recorded).
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
    """Last-edit commit sha of ``rel`` — provenance only, '' on any error."""
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo}", "-C", str(repo),
             "log", "-1", "--format=%H", "--", rel],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _skill_digest(repo: Path, skill: str) -> str:
    """sha256 content digest of the skill's SKILL.md + evals.json bytes.

    This is the watermark key: it changes exactly when the instance changes
    what would be evaluated (the skill text or the plan). A missing file
    hashes as empty so create/delete both count as a change.
    """
    hasher = hashlib.sha256()
    for rel in ("SKILL.md", "evals/evals.json"):
        path = Path(repo) / "skills" / skill / rel
        try:
            hasher.update(path.read_bytes())
        except Exception:
            pass
        hasher.update(b"\x00")
    return hasher.hexdigest()


# ─── sidecar (parent-owned; FITNESS_SIDECARS member) ────────────────────────


def _sidecar_path(state_dir: Path) -> Path:
    return Path(state_dir) / SIDECAR_REL


def _row_well_formed(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and row.get("schema") == SCHEMA
        and isinstance(row.get("skill"), str)
        and bool(row.get("skill"))
        and isinstance(row.get("eval_id"), str)
        and bool(row.get("eval_id"))
    )


def _load_rows(state_dir: Path) -> list[dict[str, Any]]:
    try:
        path = _sidecar_path(state_dir)
        if not path.is_file() or path.stat().st_size > MAX_SIDECAR_BYTES:
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if _row_well_formed(row):
                rows.append(row)
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


# ─── watermark (pacing gate, #893 pattern) ──────────────────────────────────


def _watermark_path(state_dir: Path) -> Path:
    return Path(state_dir) / WATERMARK_REL


def _load_watermark(state_dir: Path) -> dict[str, Any]:
    try:
        path = _watermark_path(state_dir)
        if not path.is_file() or path.stat().st_size > MAX_EVAL_BYTES:
            return {"skills": {}, "runs": []}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"skills": {}, "runs": []}
        skills = raw.get("skills")
        runs = raw.get("runs")
        return {
            "skills": skills if isinstance(skills, dict) else {},
            "runs": [r for r in runs if isinstance(r, str)] if isinstance(runs, list) else [],
        }
    except Exception:
        return {"skills": {}, "runs": []}


def _save_watermark(state_dir: Path, watermark: dict[str, Any]) -> None:
    try:
        path = _watermark_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(watermark, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _runs_this_week(watermark: dict[str, Any], now: datetime) -> list[str]:
    cutoff = now - timedelta(days=7)
    kept = []
    for stamp in watermark.get("runs", []):
        parsed = _parse(stamp)
        if parsed is not None and parsed >= cutoff:
            kept.append(stamp)
    return kept


# ─── eval plan (instance-authored steering; fail-closed validation) ─────────


def load_cases(repo: Path, skill: str) -> list[dict[str, Any]] | None:
    """Validate and return an eval plan, or ``None`` (fail closed).

    Rejected outright (no partial acceptance, nothing runs): missing or
    oversized file, non-JSON, not a case list (bare or under ``"evals"``),
    empty, more than :data:`MAX_CASES` cases, any case missing
    ``id``/``prompt``/``expected_output``/``assertions`` or carrying a
    wrong-typed field.
    """
    try:
        path = Path(repo) / "skills" / skill / "evals" / "evals.json"
        if not path.is_file() or path.stat().st_size > MAX_EVAL_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases = raw.get("evals") if isinstance(raw, dict) else raw
        if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
            return None
        valid: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for case in cases:
            if not isinstance(case, dict):
                return None
            if not all(key in case for key in ("id", "prompt", "expected_output", "assertions")):
                return None
            if not isinstance(case["id"], str) or not case["id"].strip() or case["id"] in seen_ids:
                return None
            if not isinstance(case["prompt"], str) or not isinstance(case["expected_output"], str):
                return None
            if not isinstance(case["assertions"], list) or not all(isinstance(x, str) for x in case["assertions"]):
                return None
            seen_ids.add(case["id"])
            valid.append(case)
        return valid
    except Exception:
        return None


# ─── executor ───────────────────────────────────────────────────────────────


def _llm_runner(prompt: str, with_skill: bool, skill_path: Path, timeout: float) -> dict[str, Any]:
    """Default production runner: one bounded chat completion via the
    operator's LiteLLM endpoint on the local ``harness``-role executor model.
    Missing credentials degrade to an error row — never a crash, never a
    network guess."""
    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        return {"output": "", "tokens": 0, "duration": 0.0, "error": "runner_not_configured"}
    from openai import OpenAI

    from nanobot.runtime.model_registry import resolve_model

    system = (
        "You are the eeebot skill-eval executor. Complete the task directly "
        "and concisely; output only the answer."
    )
    if with_skill:
        try:
            skill_text = Path(skill_path).read_text(encoding="utf-8")[:MAX_SKILL_TEXT_CHARS]
        except Exception:
            skill_text = ""
        system += "\n\n<skill>\n" + skill_text + "\n</skill>"
    model = resolve_model("harness", strip_openai=True)
    started = time.monotonic()
    response = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout).chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt[:MAX_PROMPT_CHARS]},
        ],
        max_tokens=1_000,
        temperature=0.0,
    )
    choice = response.choices[0]
    content = getattr(getattr(choice, "message", None), "content", "") or ""
    usage = getattr(response, "usage", None)
    tokens = int(getattr(usage, "total_tokens", 0) or 0)
    return {"output": content, "tokens": tokens, "duration": time.monotonic() - started}


def _call_bounded(fn: Runner, prompt: str, with_skill: bool, skill_path: Path, timeout: float) -> dict[str, Any]:
    """Run one executor call on an abandoned-on-expiry daemon thread.

    The hard guarantee "a hung eval cannot stall the caller" lives HERE, not
    in the runner: the runner receives ``timeout`` so a well-behaved one can
    bound its own client call, but nothing obliges it to honor it. A call
    still running after ``timeout`` (+ a small grace for a runner that is
    honoring its own deadline right at the boundary) is abandoned — the
    daemon thread cannot block process exit — and graded as a timeout error.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            raw = fn(prompt, with_skill, skill_path, timeout)
            box["value"] = raw if isinstance(raw, dict) else {"output": str(raw)}
        except Exception as exc:
            box["value"] = {"output": "", "error": type(exc).__name__}

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout + _JOIN_GRACE_SECONDS)
    if thread.is_alive() or "value" not in box:
        return {"output": "", "error": "timeout"}
    return box["value"]


def _mechanical_assertions(case: dict[str, Any]) -> list[str]:
    return [a for a in case["assertions"] if not a.startswith(_ADVISORY_PREFIX)]


def _advisory_assertions(case: dict[str, Any]) -> list[str]:
    return [a for a in case["assertions"] if a.startswith(_ADVISORY_PREFIX)]


def _grade(case: dict[str, Any], result: dict[str, Any]) -> bool:
    """Mechanical verdict computed by THIS module: expected_output and every
    non-advisory assertion must appear as a substring of the output."""
    if result.get("error"):
        return False
    output = str(result.get("output") or "")
    if str(case["expected_output"]) not in output:
        return False
    return all(assertion in output for assertion in _mechanical_assertions(case))


# ─── one bounded A/B run ────────────────────────────────────────────────────


def evaluate_skill(
    state_dir: Path,
    repo: Path,
    skill: str,
    *,
    runner: Runner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one bounded A/B batch for ``skill`` if every budget gate passes.

    Returns a diagnostic dict (never fitness by itself): ``ran`` plus a
    ``reason`` of ``disabled`` / ``invalid_eval_plan`` / ``unchanged`` /
    ``weekly_cap`` / ``no_cases_run`` / ``ok``. Verdict rows land only in the
    protected sidecar via the parent-owned atomic rewrite. Never raises.
    """
    now = now or _now()
    result: dict[str, Any] = {"skill": skill, "ran": False, "reason": "disabled", "cases": []}
    if not enabled():
        return result
    cases = load_cases(repo, skill)
    if cases is None:
        result["reason"] = "invalid_eval_plan"
        return result
    state_dir = Path(state_dir)
    digest = _skill_digest(Path(repo), skill)
    watermark = _load_watermark(state_dir)
    stamped = watermark["skills"].get(skill)
    if isinstance(stamped, dict) and stamped.get("digest") == digest:
        result["reason"] = "unchanged"
        return result
    runs = _runs_this_week(watermark, now)
    if len(runs) >= MAX_WEEKLY_RUNS:
        result["reason"] = "weekly_cap"
        return result

    skill_path = Path(repo) / "skills" / skill / "SKILL.md"
    commit = _git_sha(Path(repo), f"skills/{skill}/SKILL.md")
    prior = _load_rows(state_dir)
    run_id = hashlib.sha256(f"{skill}:{digest}:{_iso(now)}".encode()).hexdigest()[:16]
    fn = runner or _llm_runner
    started = time.monotonic()
    current: list[dict[str, Any]] = []
    try:
        for case in cases:
            remaining = MAX_RUN_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                break
            pair: dict[str, Any] = {"id": case["id"]}
            for mode in (False, True):
                remaining = MAX_RUN_SECONDS - (time.monotonic() - started)
                call_timeout = min(MAX_CASE_SECONDS, max(remaining, 0.0))
                call_started = time.monotonic()
                raw = _call_bounded(fn, case["prompt"], mode, skill_path, call_timeout)
                duration = float(raw.get("duration", time.monotonic() - call_started) or 0.0)
                arm: dict[str, Any] = {
                    "pass": _grade(case, raw),
                    "tokens": int(raw.get("tokens", 0) or 0),
                    "duration": round(duration, 3),
                }
                if raw.get("error"):
                    arm["error"] = str(raw["error"])[:100]
                pair["with" if mode else "without"] = arm
            pair["delta"] = int(pair["with"]["pass"]) - int(pair["without"]["pass"])
            row = {
                "schema": SCHEMA,
                "run_id": run_id,
                "skill": skill,
                "skill_commit": commit,
                "eval_id": case["id"],
                "ts": _iso(now),
                **pair,
            }
            advisory = _advisory_assertions(case)
            if advisory:
                row["advisory"] = advisory[:10]
            current.append(row)
    finally:
        _rewrite_rows(state_dir, prior, current)
        # Stamp AFTER the attempt so a changed skill runs at most once per
        # change even when the run itself errored — cost control beats
        # retry-on-flake here (a re-run comes free with the next change).
        watermark["skills"][skill] = {"digest": digest, "ts": _iso(now), "git_sha": commit}
        watermark["runs"] = (runs + [_iso(now)])[-MAX_WEEKLY_RUNS * 4 :]
        _save_watermark(state_dir, watermark)
    result.update({
        "ran": bool(current),
        "reason": "ok" if current else "no_cases_run",
        "run_id": run_id,
        "cases": current,
    })
    return result


# ─── read side ──────────────────────────────────────────────────────────────


def fitness_rows(state_dir: Path, skill: str | None = None) -> list[dict[str, Any]]:
    """Well-formed verdict rows from the protected sidecar (read-only)."""
    rows = _load_rows(Path(state_dir))
    return [r for r in rows if skill is None or r.get("skill") == skill]


def _latest_run_rows(rows: list[dict[str, Any]], skill: str) -> list[dict[str, Any]]:
    """Rows of the skill's newest run only — a since-fixed skill must not
    linger as demand forever (same freshness rule as the validator lane)."""
    mine = [r for r in rows if r.get("skill") == skill]
    if not mine:
        return []
    latest = max(mine, key=lambda r: str(r.get("ts") or ""))
    run_id = latest.get("run_id")
    return [r for r in mine if r.get("run_id") == run_id]


def negative_delta_demand(state_dir: Path, *, limit: int | None = 3) -> list[dict[str, str]]:
    """Bounded defect-shaped items for skills whose latest measured A/B run
    is negative ("skill fails its own evals") or a pure cost (every case
    passes both arms, no case gains, and the with-skill arm spends more
    tokens: "skill costs more than it buys"). A passing delta yields nothing.
    Fail-open to ``[]``."""
    out: list[dict[str, str]] = []
    try:
        rows = _load_rows(Path(state_dir))
        for skill in sorted({str(r.get("skill")) for r in rows}):
            latest = _latest_run_rows(rows, skill)
            if not latest:
                continue
            deltas = [int(r.get("delta", 0) or 0) for r in latest]
            summary = ""
            evidence = ""
            if any(d < 0 for d in deltas):
                failing = [str(r.get("eval_id")) for r in latest if int(r.get("delta", 0) or 0) < 0]
                summary = f"skill fails its own evals: {skill}"
                evidence = (
                    "harness-measured with/without delta is negative for eval case(s): "
                    + ", ".join(failing[:5])
                )
            elif all(d == 0 for d in deltas) and latest:
                def _arm_int(row: dict[str, Any], arm: str, key: str) -> int:
                    value = row.get(arm)
                    return int(value.get(key, 0) or 0) if isinstance(value, dict) else 0

                all_pass = all(
                    isinstance(r.get("with"), dict) and r["with"].get("pass") is True
                    for r in latest
                )
                tokens_with = sum(_arm_int(r, "with", "tokens") for r in latest)
                tokens_without = sum(_arm_int(r, "without", "tokens") for r in latest)
                if all_pass and tokens_with > tokens_without:
                    summary = f"skill costs more than it buys: {skill}"
                    evidence = (
                        f"harness-measured pass delta is zero while the with-skill arm "
                        f"spent {tokens_with} tokens vs {tokens_without} without"
                    )
            if not summary:
                continue
            digest = hashlib.sha256(f"skill-eval:{skill}".encode()).hexdigest()[:12]
            out.append({
                "kind": "defect",
                "id": f"skill-eval-{digest}",
                "summary": summary,
                "evidence": evidence,
                "affected_path": f"skills/{skill}",
            })
            if limit is not None and len(out) >= limit:
                break
    except Exception:
        pass
    return out


# ─── periodic entrypoint (systemd oneshot, same shape as strategist) ────────


def _acquire_bridge_lock(state_dir: Path):
    """Non-blocking flock on ``<state_dir>/bridge.lock`` — the SAME lock file
    the bridge holds for the whole cycle (``bridge._acquire_bridge_lock``,
    #680). This harness writes a ``FITNESS_SIDECARS`` member
    (:data:`SIDECAR_REL`), and #789 hashes those sidecars at the bridge's
    spawn boundary and re-checks before the gate verdict: a timer-driven
    write landing inside that window would be recorded as an instance tamper
    incident — false blame. Holding the bridge lock makes overlap impossible;
    if the bridge is mid-cycle we skip this invocation entirely (the timer
    retries tomorrow). Returns an open handle to keep for the run, ``True``
    on platforms without ``fcntl`` (non-POSIX dev hosts; the eeepc host is
    always Linux), or ``None`` when the bridge holds the lock."""
    if fcntl is None:
        return True
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        handle = open(Path(state_dir) / "bridge.lock", "a+")
    except Exception:
        return None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def run_all(state_dir: Path, repo: Path, *, runner: Runner | None = None) -> dict[str, Any]:
    """Evaluate every skill carrying an eval plan, inside one total
    wall-clock budget (:data:`MAX_INVOCATION_SECONDS`). Per-skill gates
    (watermark, weekly cap, per-run budget) apply inside
    :func:`evaluate_skill`; skipped skills cost one digest read each.
    Holds the bridge concurrency lock for the whole run (see
    :func:`_acquire_bridge_lock`) so sidecar writes can never land inside a
    bridge #789 spawn window; a busy bridge skips the invocation."""
    summary: dict[str, Any] = {"enabled": enabled(), "skills": {}}
    if not enabled():
        return summary
    lock = _acquire_bridge_lock(Path(state_dir))
    if lock is None:
        summary["skipped"] = "bridge_busy"
        return summary
    try:
        result = _run_all_locked(state_dir, repo, summary, runner=runner)
        # #1093: reuse this existing parent-owned harness invocation and lock;
        # knowledge lift is independently default-off and therefore inert
        # unless its operator kill-switch is explicitly enabled.
        try:
            from nanobot.runtime import knowledge_lift

            result["knowledge_lift"] = knowledge_lift.run_all(
                Path(state_dir), Path(repo), lock_held=True,
            )
        except Exception:
            pass
        return result
    finally:
        if lock is not True:
            try:
                lock.close()
            except Exception:
                pass


def _run_all_locked(
    state_dir: Path, repo: Path, summary: dict[str, Any], *, runner: Runner | None = None
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        skills_dir = Path(repo) / "skills"
        names = sorted(
            p.parent.parent.name
            for p in skills_dir.glob("*/evals/evals.json")
        ) if skills_dir.is_dir() else []
    except Exception:
        names = []
    for name in names:
        if time.monotonic() - started >= MAX_INVOCATION_SECONDS:
            summary["skills"][name] = {"ran": False, "reason": "invocation_budget"}
            continue
        result = evaluate_skill(state_dir, repo, name, runner=runner)
        summary["skills"][name] = {"ran": result["ran"], "reason": result["reason"]}
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run harness A/B skill evals (#941)")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_all(args.state_root, args.repo), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
