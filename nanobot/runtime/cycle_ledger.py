"""Minimal cycle ledger: one flat append-only JSONL file per self-evolving cycle.

Issue #720 implements the #704 ledger design (``docs/changes/
704-ledger-artifact-memory/design.md``) in its MINIMAL form. #704 specified
five ledgers/artifacts, two of them net-new (a per-day ``done/`` and
``failure/`` split). This module deliberately does NOT build that split —
it appends every phase of every cycle (write-ahead start, dedup decision,
gate decision, terminal outcome) to a SINGLE flat file,
``<state_dir>/ledger/cycles.jsonl``, with one ``phase`` field distinguishing
row kinds and one enum ``outcome`` field on the terminal row. This is the
deviation from the design doc: no done/failure split, no per-day filename
sharding by content. Splitting into `done`/`failure` ledgers (if ever
needed by #705/#710) can be done by filtering this single file's `outcome`
field — nothing here forecloses that.

Grounded in the KB pattern mined for #720 (ralph ``progress.txt`` / auto-
research ``results.tsv``): a single flat append-only log, not a database.

Rotation reuses the shape of ``nanobot.observability.llm_telemetry.
_rotate_and_prune`` (#675/#693), adapted for a single active filename rather
than daily-named files: on each append, if ``cycles.jsonl`` already exists
and was last modified on a PRIOR day, it is gzip-archived to
``cycles-YYYY-MM-DD.jsonl.gz`` (named after its own last-modified day) and a
fresh ``cycles.jsonl`` is started; any ``cycles-*.jsonl.gz`` older than the
retention window (``CYCLE_LEDGER_RETENTION_DAYS``, default 90) is pruned.

Everything here is best-effort / fail-open: a ledger write must never crash
the bridge or change its control flow.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_LEDGER_SUBDIR = "ledger"
_LEDGER_FILENAME = "cycles.jsonl"
_DEFAULT_RETENTION_DAYS = 90

# Enum contract (#720 acceptance criteria: "Every bridge cycle ... leaves
# exactly one terminal ledger row with an enum outcome"). An invalid/unknown
# outcome value is coerced to 'failed' (fail-closed on the classification,
# fail-open on the write) rather than raising or silently writing free text.
# 'promotion_candidate' (#812): a green runtime-slice cycle that produced a
# pending promotion candidate instead of integrating to main — not a success
# (main never moved) and not a failure (the gate passed). Kept distinct so
# fitness/analytics don't miscount it as either.
VALID_OUTCOMES = frozenset({
    "success", "partial", "failed", "skipped-duplicate", "promotion_candidate",
})
VALID_DEDUP_DECISIONS = frozenset({"proceeded", "skipped_duplicate", "skipped_recent_failure"})

# #1118: a NEW, purely additive tri-state field on the terminal row —
# ``outcome`` above is untouched (byte-identical values/semantics for every
# existing consumer). ``verdict`` answers a narrower question ``outcome``
# cannot: was this cycle's own work a healthy result?
#   accept       — integrated (and, when a claim exists, it held).
#   reject       — a clean negative: verified already-done/not-applicable,
#                  or the work measurably failed (e.g. a policy/gate
#                  violation) — a HEALTHY, deterministic cycle.
#   inconclusive — blocked or ambiguous: infra/harness trouble, a timeout,
#                  or no signal either way.
# Derivation lives in nanobot.runtime.bridge (deterministic, code-only —
# never a new LLM call); this module only validates and stores the result.
VALID_VERDICTS = frozenset({"accept", "reject", "inconclusive"})


def _ledger_dir(state_dir: Path) -> Path:
    return Path(state_dir) / _LEDGER_SUBDIR


def _retention_days() -> int:
    raw = os.environ.get("CYCLE_LEDGER_RETENTION_DAYS", "").strip()
    if not raw:
        return _DEFAULT_RETENTION_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_RETENTION_DAYS


def _day_str(name: str) -> str | None:
    """Extract the YYYY-MM-DD stem from a ``cycles-*.jsonl.gz`` filename."""
    prefix = "cycles-"
    suffix = ".jsonl.gz"
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return None
    candidate = name[len(prefix) : -len(suffix)]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return None
    return candidate


def _rotate_and_prune(ledger_dir: Path, active_path: Path, today: str, retention_days: int) -> None:
    """Archive a stale active file and prune expired archives.

    Mirrors ``llm_telemetry._rotate_and_prune``'s shape (gzip prior content,
    prune expired ``.gz`` files, best-effort per file) but adapted to a single
    active filename: the active file is rotated by ITS OWN last-modified day
    rather than by filename, since there is only one filename to rotate.
    """
    try:
        if active_path.exists():
            mtime_day = datetime.fromtimestamp(
                active_path.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            if mtime_day != today:
                gz_path = ledger_dir / f"cycles-{mtime_day}.jsonl.gz"
                with open(active_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                active_path.unlink()
    except Exception:
        pass

    try:
        from datetime import timedelta

        cutoff_ordinal = (datetime.now(timezone.utc).date() - timedelta(days=retention_days)).toordinal()
    except Exception:
        return

    for path in ledger_dir.glob("cycles-*.jsonl.gz"):
        day = _day_str(path.name)
        if not day:
            continue
        try:
            day_ordinal = datetime.strptime(day, "%Y-%m-%d").date().toordinal()
            if day_ordinal < cutoff_ordinal:
                path.unlink(missing_ok=True)
        except Exception:
            continue


def append_event(state_dir: Path, event: dict) -> None:
    """Append one JSON line to the cycle ledger. Best-effort — never raises.

    Adds ``ts`` if not already present. Fail-open: any failure (unwritable
    dir, disk full, permission error, ...) is swallowed so the ledger can
    never break the bridge cycle it is observing.
    """
    with contextlib.suppress(Exception):
        record = dict(event)
        record.setdefault("ts", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

        ledger_dir = _ledger_dir(state_dir)
        ledger_dir.mkdir(parents=True, exist_ok=True)
        active_path = ledger_dir / _LEDGER_FILENAME
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _rotate_and_prune(ledger_dir, active_path, today, _retention_days())

        with open(active_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def record_cycle_started(
    state_dir: Path,
    cycle_id: str,
    request_id: str,
    branch: str | None,
) -> None:
    """Write-ahead marker: append BEFORE the subagent is spawned (claw0 pattern).

    A crashed/timed-out cycle leaves this row with no matching terminal
    ``outcome`` row — a deterministic, queryable recovery signal instead of
    inference from file mtimes.
    """
    append_event(
        state_dir,
        {
            "phase": "started",
            "cycle_id": cycle_id or "",
            "request_id": request_id or "",
            "branch": branch or None,
        },
    )


def record_dedup_decision(
    state_dir: Path,
    cycle_id: str,
    decision: str,
    matched_against: str | None,
) -> None:
    """Log the pre-spawn dedup heuristic's own decision (#720 piece 4).

    ``decision`` is one of :data:`VALID_DEDUP_DECISIONS` — coerced to
    ``'proceeded'`` if unrecognized (an unknown decision must not silently
    read as a suppression).
    """
    if decision not in VALID_DEDUP_DECISIONS:
        decision = "proceeded"
    append_event(
        state_dir,
        {
            "phase": "dedup",
            "cycle_id": cycle_id or "",
            "decision": decision,
            "matched_against": matched_against or None,
        },
    )


def record_gate_decision(
    state_dir: Path,
    cycle_id: str,
    allowed: bool,
    reason: str | None,
    violations: list[str] | None = None,
) -> None:
    """Log the mutation-surface guard / smoke gate's allow-or-block decision (#720 piece 2)."""
    append_event(
        state_dir,
        {
            "phase": "gate",
            "cycle_id": cycle_id or "",
            "allowed": bool(allowed),
            "reason": reason or None,
            "violations": list(violations or []),
        },
    )


def record_cycle_outcome(
    state_dir: Path,
    cycle_id: str,
    outcome: str,
    reason: str | None,
    files_changed: list[str] | None,
    branch: str | None,
    *,
    verdict: str | None = None,
    verdict_reason: str | None = None,
    executor_llm_error: bool = False,
    lane: str | None = None,
) -> None:
    """Write the terminal, exactly-once-per-cycle row with an enum ``outcome``.

    #1281: ``executor_llm_error`` (keyword-only, additive like ``verdict``)
    records that the cycle's executor died on its LLM call *whatever the
    outcome*. For the no-work case ``reason`` already says
    ``executor_llm_error`` (#1280); this flag exists for the other case —
    the subagent had edited files before the call died, the auto-commit
    safety net (#666) committed them and the gate integrated the cycle —
    which until #1281 left no trace in the ledger and could only be counted
    by joining telemetry to results. Written only when true, so every row
    without the key keeps its pre-#1281 shape.

    Must be called in the SAME step that writes the bridge result / performs
    the merge — never deferred — so the ledger and git state never diverge
    (KB warning this design is built to avoid). ``outcome`` not in
    :data:`VALID_OUTCOMES` is coerced to ``'failed'`` (fail-closed
    classification; the write itself still never raises).

    #1118: ``verdict`` (keyword-only, appended LAST) is a NEW optional
    sibling field — ``accept``/``reject``/``inconclusive``, see
    :data:`VALID_VERDICTS`. It is purely additive: every existing positional
    call site keeps working unchanged and omits it, in which case the row
    carries no ``verdict``/``verdict_reason`` key at all (not even ``None``)
    — identical to the pre-#1118 row shape, so no existing exact-dict-
    equality assertion or key-count check anywhere can observe a change.
    An unrecognized ``verdict`` value is dropped the same way (fail-closed
    on the classification, fail-open on the write, mirroring ``outcome``'s
    own coercion above — except here "coercion" means "omit" rather than a
    forced fallback value, since an absent verdict is itself a valid,
    honest state for any caller not yet updated for #1118).
    ``verdict_reason`` (e.g. ``already_done``) is recorded only alongside a
    valid ``verdict`` and is always optional/free-form.
    """
    if outcome not in VALID_OUTCOMES:
        outcome = "failed"
    row = {
        "phase": "outcome",
        "cycle_id": cycle_id or "",
        "outcome": outcome,
        "reason": reason or None,
        "files_changed": list(files_changed or []),
        "branch": branch or None,
    }
    if verdict in VALID_VERDICTS:
        row["verdict"] = verdict
        if verdict_reason:
            row["verdict_reason"] = str(verdict_reason)[:200]
    if executor_llm_error:
        row["executor_llm_error"] = True
    if lane:
        # #1411: additive-only, like verdict/executor_llm_error above — a row
        # written without ``lane`` (every pre-#1411 call site) is byte-
        # identical to before.
        row["lane"] = str(lane)
    if files_changed is not None:
        try:
            from nanobot.runtime.demand import classify_change_tier
            row["change_tier"] = classify_change_tier(files_changed)
        except Exception:
            pass
    append_event(
        state_dir,
        row,
    )

def read_events(state_dir: Path) -> list[dict]:
    """Read all rows from the ACTIVE ledger file. Best-effort — never raises.

    Returns the parsed JSON rows of the current (unrotated) ledger file,
    oldest first; an unreadable file yields an empty list and malformed
    lines are skipped. Rotated files are intentionally not read: callers
    using this for same-day checks (the explore daily cap) only need
    today's rows, and the active file always contains today.
    """
    rows: list[dict] = []
    with contextlib.suppress(Exception):
        active_path = _ledger_dir(state_dir) / _LEDGER_FILENAME
        with open(active_path, encoding="utf-8") as fh:
            for line in fh:
                with contextlib.suppress(Exception):
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
    return rows


def successful_cycle_ids(state_dir: Path) -> set[str]:
    """Return the ``cycle_id`` of every terminal row with ``outcome: success``,
    read ACROSS the rotation: every ``cycles-YYYY-MM-DD.jsonl.gz`` archive
    plus the active file. Best-effort — never raises; an unreadable archive
    or malformed line is skipped (#1215).

    Unlike :func:`read_events`, this answers a question that is NOT
    same-day: "did the cycle behind this result artifact ever integrate?"
    Result artifacts outlive the day they were written (they migrate to
    ``subagents/archive/`` and stay readable for hundreds of cycles), so a
    reader that opened only the active file would call every integrated
    attempt older than today "never integrated" (#1178/#1207: rotation
    narrows every reader that only opens the live file).
    """
    ids: set[str] = set()

    def _collect(lines) -> None:
        for line in lines:
            with contextlib.suppress(Exception):
                row = json.loads(line)
                if (
                    isinstance(row, dict)
                    and row.get("phase") == "outcome"
                    and row.get("outcome") == "success"
                    and row.get("cycle_id")
                ):
                    ids.add(str(row["cycle_id"]))

    ledger_dir = _ledger_dir(state_dir)
    with contextlib.suppress(Exception):
        for gz_path in sorted(ledger_dir.glob("cycles-*.jsonl.gz")):
            with contextlib.suppress(Exception):
                with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                    _collect(fh)
    with contextlib.suppress(Exception):
        with open(ledger_dir / _LEDGER_FILENAME, encoding="utf-8") as fh:
            _collect(fh)
    return ids


def record_explore_started(state_dir: Path, cycle_id: str, candidates_count: int, declared_measurement: str) -> None:
    append_event(state_dir, {
        'phase': 'explore_started',
        'cycle_id': cycle_id,
        'candidates_count': candidates_count,
        'declared_measurement': declared_measurement,
    })

def record_explore_candidate(state_dir: Path, cycle_id: str, cand_cycle_id: str, score: float) -> None:
    append_event(state_dir, {
        'phase': 'explore_candidate',
        'cycle_id': cycle_id,
        'cand_cycle_id': cand_cycle_id,
        'score': score,
    })

def record_explore_selected(state_dir: Path, cycle_id: str, winner_branch: str) -> None:
    append_event(state_dir, {
        'phase': 'explore_selected',
        'cycle_id': cycle_id,
        'winner_branch': winner_branch,
    })
