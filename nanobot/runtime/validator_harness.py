"""Validator harness — run built validators, consume their findings (#925).

Root cause this module addresses: the loop *builds* validator-class scripts
(``check_*``/``validate_*``/``audit_*``/``analyze_*``/``verify_*`` under the
instance repo's ``scripts/``) but nothing ever *runs* them afterward. Usage
evidence (``nanobot.runtime.usage_evidence``) only records consumption that
happens organically, and birth-use deliberately does not count (#800/#802
anti-farming rules) — so genuinely useful artifacts sit unconfirmed forever
and eventually decay. This module is a bounded, periodic, read-only runner
that actually EXERCISES those scripts and feeds their findings back into the
loop as real demand, making the resulting usage evidence genuine rather than
farmed.

Design, one call to :func:`run_validator_harness` per invocation:

1. Select up to :func:`_max_k` (env ``SELFEVO_VALIDATOR_HARNESS_MAX``,
   default 5) scripts matching the allowlist
   ``scripts/(check|validate|audit|analyze|verify)_*.py``, least-recently-run
   first. Rotation state persists in
   ``<state_dir>/validator_harness/rotation.json`` (same served-map schema
   style as ``llm_proposer``'s #902 demand rotation: ``{"served":
   {"<rel_path>": "<iso-ts>"}}``, pruned to the current candidate set on
   every call). Unlike #902 (which stamps at *presentation* time), this
   stamps at *execution* time only — a script that was merely SELECTED but
   never actually run (total-budget exhaustion) must not appear more
   recently run than it truly was.
2. BIRTH-WINDOW EXCLUSION (#800/#802 semantics, unchanged): a script whose
   git creation commit (``git log --diff-filter=A --format=%cI``) is younger
   than :data:`_BIRTH_WINDOW_HOURS` (24h) is skipped — running (and thus
   confirming) a script the instance JUST wrote would be indistinguishable
   from the birth-use self-test #800/#802 already ban for the pycache/output
   signals. Fail-open here means "cannot determine age -> skip" (never run
   something whose age is unknown), matching the module's overall
   skip-means-not-run discipline.
3. Each selected script is run with ``<venv python> <script> --json`` when
   the script's own text mentions ``--json`` (cheap textual check — this is
   a SELECTION heuristic, not a security boundary), else a plain invocation.
   ``subprocess`` with ``cwd=selfevo_repo``, a per-script timeout
   (:data:`_PER_SCRIPT_TIMEOUT`, 60s) and a total per-invocation budget
   (:data:`_TOTAL_BUDGET_SECONDS`, 240s) that stops selecting further runs
   once exhausted. Environment is inherited unchanged (no env= override).
   stdout/stderr are captured, capped to :data:`_MAX_OUTPUT_BYTES`.
4. READ-ONLY DISCIPLINE: ``git status --porcelain`` is compared before and
   after each run; if a script dirtied the instance repo's working tree, it
   is restored (``git checkout -- .``) and the run is flagged
   ``repo_dirtied`` — surfaced by ``demand._validator_defect_items`` as its
   own defect ("validator mutates repo"), never silently tolerated.
5. Results are appended to ``<state_dir>/validator_harness/last_runs.jsonl``
   (one JSON line per run; findings are parsed with a deliberately tiny,
   fail-open heuristic — the count of items in any top-level JSON
   list/dict field literally named ``findings``/``alerts``/``missing``/
   ``failures``; anything else falls back to "no findings parsed", never a
   crash). The file is trimmed on write to the newest
   :data:`_MAX_LAST_RUNS_LINES` lines so it never grows unbounded.
   ``demand._validator_defect_items`` reads this sidecar and turns a
   non-zero exit or a positive findings count into bounded ``defect`` demand
   — this is what makes the usage genuine: a validator that finds a real
   problem generates real follow-up work, not just a timestamp bump.
6. Usage evidence: for every script actually RUN (regardless of exit code),
   ``usage_evidence.record_validator_run`` is called — see that function's
   own docstring, and the ``"validator"`` entry in
   ``usage_evidence.HARNESS_SIGNALS``, for the full #789 non-forgeable trust
   argument (this module is product-runtime code invoked only by a
   root-deployed systemd timer as `eeepc-agent`; the instance loop cannot
   reach this call).

Fail-open and bounded throughout: any error in selection, a single script's
execution, or evidence recording never raises into the caller and never
blocks any other script's run or the bridge loop. Read-only w.r.t. the
instance repo tree beyond the guaranteed-restored guard above; this module's
own writes are confined to ``<state_dir>/validator_harness/`` plus the
usage-evidence sidecar and (via demand's own defect wiring) the demand
mechanics — never a git commit, never a mutation of the instance repo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROTATION_SCHEMA = "validator-harness-rotation-v1"
_LAST_RUNS_FILENAME = "last_runs.jsonl"
_MAX_LAST_RUNS_LINES = 500  # bounded growth (#925), same discipline as scorecard/benchmark history

_MAX_K_ENV = "SELFEVO_VALIDATOR_HARNESS_MAX"
_DEFAULT_MAX_K = 5

_PER_SCRIPT_TIMEOUT = 60.0  # seconds, hard cap per script
_TOTAL_BUDGET_SECONDS = 240.0  # seconds, hard cap for the whole invocation

_BIRTH_WINDOW_HOURS = 24  # #800/#802: never run (and thus never confirm) a just-created script

_MAX_OUTPUT_BYTES = 64 * 1024  # captured stdout/stderr cap per run
_MAX_SCAN_BYTES = 200_000  # bounded read for the cheap "--json" text check

# scripts/(check|validate|audit|analyze|verify)_*.py — the built-validator class
_ALLOWLIST_RE = re.compile(r"^(check|validate|audit|analyze|verify)_.*\.py$")

# Tiny, deliberately narrow findings-count heuristic (#925 design: "keep the
# parse heuristic tiny and fail-open to raw exit code"). Only a top-level
# JSON object with one of these list/dict fields counts; anything else
# yields None (no findings parsed), never a crash or a guess.
_FINDINGS_KEYS = ("findings", "alerts", "missing", "failures")


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _max_k() -> int:
    """#925: ``SELFEVO_VALIDATOR_HARNESS_MAX`` env override for K, default
    5; unset, empty, non-numeric, or non-positive falls back to the
    default."""
    raw = os.environ.get(_MAX_K_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_K
    try:
        value = int(raw)
    except Exception:
        return _DEFAULT_MAX_K
    return value if value > 0 else _DEFAULT_MAX_K


# ─── selection ───────────────────────────────────────────────────────────


def _candidate_scripts(selfevo_repo: Path) -> list[Path]:
    """Allowlisted validator-class scripts in the instance repo, sorted for
    determinism. Fail-open: no ``scripts/`` dir or any error yields ``[]``."""
    try:
        scripts_dir = Path(selfevo_repo) / "scripts"
        if not scripts_dir.is_dir():
            return []
        return sorted(p for p in scripts_dir.glob("*.py") if _ALLOWLIST_RE.match(p.name))
    except Exception:
        return []


def _git_creation_iso(selfevo_repo: Path, rel: str) -> str | None:
    """Author-independent creation date of ``rel`` (the committer date of
    the FIRST commit that added it) — ``None`` on any failure (fail-open;
    the caller treats an unknown creation date as "in the birth window",
    i.e. never run something whose age it cannot establish)."""
    try:
        result = subprocess.run(
            [
                "git", "-C", str(selfevo_repo), "log",
                "--diff-filter=A", "--format=%cI", "--", rel,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            return None
        return lines[-1]  # oldest add-commit = creation
    except Exception:
        return None


def _in_birth_window(selfevo_repo: Path, rel: str, now: datetime) -> bool:
    """True iff ``rel`` was created less than :data:`_BIRTH_WINDOW_HOURS`
    ago, OR its creation date cannot be determined at all (fail-open toward
    NOT running it — #800/#802: birth-use must never confirm)."""
    created = _parse_ts(_git_creation_iso(selfevo_repo, rel))
    if created is None:
        return True
    return (now - created) < timedelta(hours=_BIRTH_WINDOW_HOURS)


# ─── rotation state (least-recently-run) ────────────────────────────────


def _rotation_path(state_dir: Path) -> Path:
    return Path(state_dir) / "validator_harness" / "rotation.json"


def _load_rotation(state_dir: Path) -> dict[str, Any]:
    """Load rotation state; missing/corrupt/malformed all fail-open to
    "nothing run yet" rather than aborting the harness invocation."""
    try:
        path = _rotation_path(state_dir)
        if not path.is_file():
            return {"schema_version": _ROTATION_SCHEMA, "served": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("served"), dict):
            return {"schema_version": _ROTATION_SCHEMA, "served": {}}
        return data
    except Exception:
        return {"schema_version": _ROTATION_SCHEMA, "served": {}}


def _write_rotation(state_dir: Path, data: dict[str, Any]) -> None:
    """Write-temp-then-``os.replace`` so a crash mid-write never leaves a
    half-written ``rotation.json`` behind for the next invocation."""
    try:
        path = _rotation_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        pass


def _rotation_key(script: Path, served: dict[str, str]) -> tuple[int, str, str]:
    """Sort key for least-recently-run-first: never-run scripts (no
    rotation entry) sort before any run one; among run ones, oldest
    timestamp first; ties broken by path for determinism."""
    rel = f"scripts/{script.name}"
    parsed = _parse_ts(served.get(rel))
    if parsed is None:
        return (0, "", rel)
    return (1, _iso(parsed), rel)


# ─── execution ───────────────────────────────────────────────────────────


def _accepts_json_flag(script: Path) -> bool:
    """Cheap textual check (not a security boundary): does the script's own
    source mention ``--json`` anywhere in its first :data:`_MAX_SCAN_BYTES`
    bytes? Fail-open to ``False`` (plain invocation) on any read error."""
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
        return "--json" in text[:_MAX_SCAN_BYTES]
    except Exception:
        return False


def _parse_findings_count(stdout: str) -> int | None:
    """Deliberately tiny findings heuristic: a top-level JSON object with a
    ``findings``/``alerts``/``missing``/``failures`` field that is a list or
    dict yields that field's length; anything else (unparseable, not an
    object, no matching field) yields ``None`` — the caller then falls back
    to the raw exit code alone, never a fabricated count."""
    try:
        data = json.loads(stdout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in _FINDINGS_KEYS:
        value = data.get(key)
        if isinstance(value, (list, dict)):
            return len(value)
    return None


def _git_status_porcelain(repo: Path) -> str | None:
    """``None`` means "could not determine" (git unavailable / not a repo) —
    kept distinct from ``""`` (clean) so the read-only check below never
    flags a false "dirtied" when it simply cannot tell."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception:
        return None


def _git_restore(repo: Path) -> None:
    """Discard any working-tree change a validator script made — the
    harness runs scripts read-only; a script that mutates the repo gets
    restored AND flagged as its own defect (see
    ``demand._validator_defect_items``), never silently tolerated. Two
    steps: ``checkout`` reverts modifications to TRACKED files, ``clean``
    removes any new untracked file/directory a script created — either
    alone leaves the tree dirty (a plain ``checkout`` never touches
    untracked additions)."""
    try:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "--", "."],
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(repo), "clean", "-fd", "--", "."],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        pass


def _run_one(script: Path, selfevo_repo: Path, timeout: float) -> dict[str, Any]:
    """Run a single validator script under the read-only/bounded discipline
    described in the module docstring. Never raises — timeouts and any
    other execution error are captured as ``exit_code: None`` records
    rather than propagated."""
    rel = f"scripts/{script.name}"
    started = datetime.now(timezone.utc)
    pre_status = _git_status_porcelain(selfevo_repo)

    cmd = [sys.executable, str(script)]
    if _accepts_json_flag(script):
        cmd.append("--json")

    exit_code: int | None
    stdout = ""
    stderr = ""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(selfevo_repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        exit_code = proc.returncode
        stdout = (proc.stdout or "")[:_MAX_OUTPUT_BYTES]
        stderr = (proc.stderr or "")[:_MAX_OUTPUT_BYTES]
    except subprocess.TimeoutExpired:
        exit_code = None
        stderr = f"timeout after {timeout:.0f}s"
    except Exception as exc:
        exit_code = None
        stderr = f"error: {exc}"

    finished = datetime.now(timezone.utc)

    post_status = _git_status_porcelain(selfevo_repo)
    repo_dirtied = (
        pre_status is not None and post_status is not None and pre_status != post_status
    )
    if repo_dirtied:
        _git_restore(selfevo_repo)

    return {
        "path": rel,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_s": round((finished - started).total_seconds(), 3),
        "exit_code": exit_code,
        "findings_count": _parse_findings_count(stdout),
        "repo_dirtied": repo_dirtied,
        "stderr_tail": stderr[-2000:],
    }


def _last_runs_path(state_dir: Path) -> Path:
    return Path(state_dir) / "validator_harness" / _LAST_RUNS_FILENAME


def _append_last_run(state_dir: Path, record: dict[str, Any]) -> None:
    """Append one run record and trim the file to the newest
    :data:`_MAX_LAST_RUNS_LINES` lines so it never grows unbounded.
    Fail-open: a sidecar write failure never breaks the run loop."""
    try:
        path = _last_runs_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_LAST_RUNS_LINES:
            path.write_text(
                "\n".join(lines[-_MAX_LAST_RUNS_LINES:]) + "\n", encoding="utf-8"
            )
    except Exception:
        pass


# ─── entrypoint ──────────────────────────────────────────────────────────


def run_validator_harness(state_dir: Path, selfevo_repo: Path) -> dict[str, Any]:
    """Run one bounded validator-harness invocation. See the module
    docstring for the full design. Returns
    ``{"selected": [...], "ran": [...], "skipped_birth_window": [...],
    "errors": [...]}`` (repo-relative ``scripts/*.py`` paths). Fail-open:
    any unexpected error yields whatever was collected so far plus an
    ``"errors"`` note, never a raised exception."""
    result: dict[str, Any] = {
        "selected": [],
        "ran": [],
        "skipped_birth_window": [],
        "errors": [],
    }
    try:
        state_dir = Path(state_dir)
        selfevo_repo = Path(selfevo_repo)
        if not selfevo_repo.is_dir():
            return result

        candidates = _candidate_scripts(selfevo_repo)
        if not candidates:
            return result

        now = datetime.now(timezone.utc)
        eligible: list[Path] = []
        for script in candidates:
            rel = f"scripts/{script.name}"
            if _in_birth_window(selfevo_repo, rel, now):
                result["skipped_birth_window"].append(rel)
            else:
                eligible.append(script)

        rotation = _load_rotation(state_dir)
        all_rels = {f"scripts/{p.name}" for p in candidates}
        served: dict[str, str] = {
            k: v for k, v in dict(rotation.get("served") or {}).items() if k in all_rels
        }

        if eligible:
            eligible.sort(key=lambda s: _rotation_key(s, served))
            selected = eligible[: _max_k()]
            result["selected"] = [f"scripts/{p.name}" for p in selected]

            deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
            for script in selected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                record = _run_one(script, selfevo_repo, min(_PER_SCRIPT_TIMEOUT, remaining))
                rel = record["path"]
                served[rel] = record["finished_at"]
                _append_last_run(state_dir, record)
                result["ran"].append(rel)
                try:
                    from nanobot.runtime import usage_evidence

                    usage_evidence.record_validator_run(
                        state_dir, rel, when=_parse_ts(record["finished_at"]) or now
                    )
                except Exception:
                    result["errors"].append(f"usage_evidence_write_failed:{rel}")

        _write_rotation(state_dir, {"schema_version": _ROTATION_SCHEMA, "served": served})
        return result
    except Exception:
        result["errors"].append("harness_failed")
        return result


def _default_state_root() -> Path:
    return Path(os.environ.get("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state"))


def _default_repo(state_root: Path) -> Path:
    return state_root.parent / "eeebot-self-evolving"


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the systemd oneshot timer
    (``eeebot-validator-harness.service``). Explicit ``--state-root``/
    ``--repo`` args (the unit passes both explicitly) rather than bridge.py's
    env-derivation style, so this module stays independently testable/
    invocable without coupling to the bridge's own env-file — falls back to
    ``$STATE_DIR`` (bridge's own default) / ``<state-root>/../eeebot-self-evolving``
    when the flags are omitted, same convention ``scripts/cleanup_subagent_queue.py``
    uses for its ``--state-root``."""
    parser = argparse.ArgumentParser(
        description="Run the validator harness — execute built validator-class "
        "scripts and record their findings (#925)."
    )
    parser.add_argument(
        "--state-root", type=str, default=None,
        help="State directory root (default: $STATE_DIR or "
        "/var/lib/eeepc-agent/self-evolving-agent/state)",
    )
    parser.add_argument(
        "--repo", type=str, default=None,
        help="Instance repo path (default: <state-root>/../eeebot-self-evolving)",
    )
    args = parser.parse_args(argv)

    state_root = Path(args.state_root) if args.state_root else _default_state_root()
    repo = Path(args.repo) if args.repo else _default_repo(state_root)

    result = run_validator_harness(state_root, repo)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
