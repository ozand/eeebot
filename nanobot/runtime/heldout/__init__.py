"""Held-out verification pack — sandboxed behavioral checks the instance
cannot see (#780).

The instance writes its own tests and the gate runs exactly those tests — a
textbook reward-hacking surface (SpecBench, weco.ai 2026-05: up to 100pp
visible-vs-held-out gap; their C-compiler case scored 97% on visible tests
via a lookup table and 0% on held-out). This package is the private half of
the evaluation split: behavioral checkers (see :mod:`.checkers`) that
exercise instance artifacts against their PUBLIC contracts on fixtures
generated at runtime in isolated tmpdirs.

**Invisibility (#603 invariant, same placement rule as the scorecard).**
The pack lives in the product runtime and ships with the product release
ONLY — it is never copied into the instance workspace, never referenced
from any instance-facing prompt, and its fixtures exist only inside
tmpdirs for the duration of one check. The loop learns *that* an artifact
failed and the checker's evidence string (WHAT is broken), never *how* the
check works — so it cannot optimize against the checker.

**Runner.** :func:`run_heldout` copies each registered artifact that exists
in the instance repo into an isolated tmpdir (at its repo-relative path, so
``state/...`` cwd-relative conventions hold), runs the checker, and records
``pass|fail|skip`` + evidence + the script's content hash to
``<state_dir>/heldout/results.json`` (schema ``heldout-results-v1``).
Sandbox: subprocess via ``sys.executable``, ``cwd=tmpdir``, 30s timeout,
env stripped to a minimal PATH + tmpdir-only PYTHONPATH/HOME/TMPDIR — no
state_dir, no secrets, no network assumptions. The persisted results also
carry ``regressions`` (#841): artifacts whose status flipped from
``pass`` (previous run) to ``fail`` (this run) — a churn signal distinct
from raw pass/fail counts; still-failing and newly-checked artifacts do
not count. A changed artifact's first non-skip verdict is confirmed with
up to :data:`_FLAKY_CONFIRM_RUNS` identical re-runs (#842); if the status
flips across those runs the artifact is non-deterministic noise, not
signal, so its recorded verdict is downgraded to ``skip`` (excluded from
the gate) and it is logged in the persisted ``flaky`` list.

**Cadence.** Full runs are gated by the ``usage_evidence`` HEAD+time
watermark (re-run only when the instance HEAD moved or
:data:`_RECHECK_HOURS` passed); within a run, an artifact whose content
hash is unchanged from the last results reuses the recorded verdict without
re-executing anything — idle stays free.

Everything is deterministic (NO LLM call) and fail-open: a checker
exception or timeout records ``skip`` with a reason; any runner error
returns the previous results — never raises into the scorecard/demand
callers (a heldout bug must never break the loop).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime.heldout import checkers as _checkers
from nanobot.runtime.heldout.checkers import CheckContext

HELDOUT_SCHEMA = "heldout-results-v1"

_RECHECK_HOURS = 6  # same cadence as usage_evidence._RESCAN_HOURS
_CHECK_TIMEOUT_SECONDS = 30.0
_MAX_EVIDENCE_CHARS = 240  # same bound as demand._MAX_EVIDENCE_CHARS

_VALID_STATUSES = ("pass", "fail", "skip")

_FLAKY_CONFIRM_RUNS = 2  # total checker runs for a non-skip verdict; a status
                         # flip across identical runs => flaky => excluded (skip).
                         # Bounded to respect the 2GB host; re-runs happen only
                         # for CHANGED pass/fail artifacts (watermark + content-hash
                         # gating already skip unchanged ones).


def _results_path(state_dir: Path) -> Path:
    return Path(state_dir) / "heldout" / "results.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


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


def _git_head(selfevo_repo: Path | None) -> str | None:
    if not selfevo_repo:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(selfevo_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:
        return None


def _load_results(state_dir: Path) -> dict[str, Any]:
    data = _read_json(_results_path(state_dir), None)
    if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
        return {"schema_version": HELDOUT_SCHEMA, "results": {}}
    return data


def _content_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:16]


def _check_one(artifact: str, source: str, checker: Any, now: datetime) -> dict[str, Any]:
    """Run one checker in a fresh isolated tmpdir. The script copy lives at
    its repo-relative path inside the tmpdir so cwd-relative ``state/...``
    conventions hold. Timeout → ``skip``; any checker exception → ``skip``
    with the exception as reason (fail-open, never raises)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="heldout-"))
    try:
        script = tmp_dir / artifact
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(source, encoding="utf-8")
        ctx = CheckContext(tmp_dir=tmp_dir, script=script, timeout=_CHECK_TIMEOUT_SECONDS)
        status, evidence = checker(ctx)
        if status not in _VALID_STATUSES:
            status, evidence = "skip", f"checker returned invalid status {status!r}"
    except subprocess.TimeoutExpired:
        status = "skip"
        evidence = f"timed out after {_CHECK_TIMEOUT_SECONDS:.0f}s"
    except Exception as exc:  # fail-open: a checker bug is never a verdict
        status = "skip"
        evidence = f"checker error: {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {
        "status": status,
        "evidence": str(evidence or "").strip()[:_MAX_EVIDENCE_CHARS],
        "content_hash": _content_hash(source),
        "ts": _iso(now),
    }


def _check_stable(artifact: str, source: str, checker: Any, now: datetime) -> dict[str, Any]:
    """Wrap :func:`_check_one` with bounded re-runs (#842) to catch a
    non-deterministic checker verdict: a status that flips across identical
    re-runs of the same source is noise, not signal, so it is excluded from
    the gate as ``skip`` rather than trusted as pass or fail.

    A first-run ``skip`` is returned unchanged — skip is already excluded
    from the gate, so spending re-runs on it buys nothing. Otherwise, up to
    :data:`_FLAKY_CONFIRM_RUNS` - 1 additional runs are made; if any disagrees
    with the first run's status, the result is downgraded to ``skip`` with a
    ``flaky`` marker and evidence describing the flip. All runs agreeing
    returns the first (stable) result unchanged."""
    first = _check_one(artifact, source, checker, now)
    if first["status"] == "skip":
        return first
    for _ in range(_FLAKY_CONFIRM_RUNS - 1):
        other = _check_one(artifact, source, checker, now)
        if other["status"] != first["status"]:
            evidence = (
                f"flaky: {first['status']} then {other['status']} "
                f"across {_FLAKY_CONFIRM_RUNS} identical runs"
            )
            return {
                "status": "skip",
                "evidence": evidence[:_MAX_EVIDENCE_CHARS],
                "content_hash": first["content_hash"],
                "ts": _iso(now),
                "flaky": True,
            }
    return first


def run_heldout(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the held-out pack against the instance repo and persist results.

    Watermark (``usage_evidence`` HEAD+time pattern): a full pass runs only
    when the instance repo's HEAD moved since the last run OR more than
    :data:`_RECHECK_HOURS` hours passed; otherwise the persisted results are
    returned with zero subprocess work. Within a pass, an artifact whose
    content hash matches its last recorded result reuses that verdict
    without re-executing. Only registered artifacts that EXIST in the
    instance repo are checked; stale entries for removed artifacts are
    dropped. Fail-open: any error returns whatever results already exist —
    never raises into the caller.
    """
    try:
        state_dir = Path(state_dir)
        data = _load_results(state_dir)
        if not selfevo_repo:
            return data
        repo = Path(selfevo_repo)
        if not repo.is_dir():
            return data

        now = now or datetime.now(timezone.utc)
        head = _git_head(repo)
        checked_at = _parse_ts(data.get("checked_at_utc"))
        if (
            not force
            and head is not None
            and data.get("git_head") == head
            and checked_at is not None
            and (now - checked_at) < timedelta(hours=_RECHECK_HOURS)
        ):
            return data  # watermark no-op — idle cycles stay free

        previous: dict[str, Any] = data.get("results") or {}
        results: dict[str, Any] = {}
        for artifact, checker in _checkers.CHECKERS.items():
            try:
                src_path = repo / artifact
                if not src_path.is_file():
                    continue  # not present in the instance repo — not checked
                source = src_path.read_text(encoding="utf-8", errors="replace")
                prev = previous.get(artifact)
                if (
                    isinstance(prev, dict)
                    and prev.get("content_hash") == _content_hash(source)
                    and prev.get("status") in _VALID_STATUSES
                ):
                    results[artifact] = prev  # unchanged script — verdict reused
                    continue
                results[artifact] = _check_stable(artifact, source, checker, now)
            except Exception:
                continue  # fail-open per artifact

        try:
            regressions = sorted(
                artifact
                for artifact, res in results.items()
                if isinstance(res, dict)
                and res.get("status") == "fail"
                and isinstance(previous.get(artifact), dict)
                and previous[artifact].get("status") == "pass"
            )
        except Exception:
            regressions = []  # fail-open — a regressions bug must never break run_heldout

        try:
            flaky = sorted(
                artifact
                for artifact, res in results.items()
                if isinstance(res, dict) and res.get("flaky") is True
            )
        except Exception:
            flaky = []  # fail-open — a flaky-list bug must never break run_heldout

        data = {
            "schema_version": HELDOUT_SCHEMA,
            "git_head": head or "",
            "checked_at_utc": _iso(now),
            "results": results,
            "regressions": regressions,  # #841: artifacts that flipped pass->fail this run
            "flaky": flaky,  # #842: artifacts excluded as skip due to a non-deterministic verdict
        }
        _write_json(_results_path(state_dir), data)
        return data
    except Exception:
        try:
            return _load_results(Path(state_dir))
        except Exception:
            return {"schema_version": HELDOUT_SCHEMA, "results": {}}
