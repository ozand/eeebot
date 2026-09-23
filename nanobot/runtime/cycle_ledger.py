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
# 'push_pending' (#1709): a gate-passed cycle whose final push exhausted its
# transient-error retries — the branch is kept and nothing about the WORK
# failed, only the last network hop. Kept distinct from 'failed' so
# exit_streak, futility and skipped_recent_failure cooling don't count it
# (see their own docstrings/readers, updated alongside this).
# 'pushed_late' / 'superseded' / 'abandoned' (#1709 increment 2): the three
# ways a 'push_pending' cycle gets resolved at the next cycle-start boundary
# (see bridge._finish_pending_pushes). 'pushed_late' is a genuine success —
# main advanced, just on a later cycle — and is folded into demand.py's
# completed-demand sidecar the same as 'success'. 'superseded' (origin/main
# moved past the row's recorded base — never merged/rebased automatically)
# and 'abandoned' (the branch no longer exists) are neither a success nor a
# failure of the original work; excluded from futility's attempt counting.
#: 'paused-supplier' (#1765): the executor's LLM call died with zero commits
#: and the classifier (nanobot.runtime.bridge._classify_llm_error) found a
#: supplier-side signal (connection refused/reset, timeout, HTTP
#: 429/500/502/503/504, "no deployments available", a missing route) rather
#: than evidence of OUR OWN defect. Distinct from 'failed' so
#: repeat_failure_rate, wasted_attempts, demand cooling and futility (each at
#: their own call site) never count a supplier outage as a defect of the
#: work or the proposal. A supplier REJECTING our request (context length,
#: malformed tool call, invalid params, our own schema errors) still records
#: 'failed' — that class is our defect and must keep failing loudly.
VALID_OUTCOMES = frozenset({
    "success", "partial", "failed", "skipped-duplicate", "promotion_candidate",
    "push_pending", "pushed_late", "superseded", "abandoned", "paused-supplier",
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


#: ADR-028 rules 2-3 (#1811): the diary's opening-entry write is committed
#: and pushed on main, before the cycle branch is cut -- there is no gate
#: verdict for it to survive, by construction. "integrated" is the only
#: success outcome; every other value is a fail-open no-op (the cycle still
#: proceeds) and must still be journalled -- a silent no-op path is a
#: defect, not an acceptable branch (#1811 AC).
VALID_DIARY_OPEN_OUTCOMES = frozenset({
    "integrated", "refused", "malformed", "push_failed", "commit_failed",
})


def record_diary_open_entry(
    state_dir: Path,
    cycle_id: str,
    outcome: str,
    commit_sha: str | None,
    reason: str,
) -> None:
    """Log the outcome of writing this cycle's opening diary entry (#1811).

    ``outcome`` is coerced to ``"commit_failed"`` if unrecognized -- an
    unknown outcome must not silently read as a success.
    """
    if outcome not in VALID_DIARY_OPEN_OUTCOMES:
        outcome = "commit_failed"
    append_event(
        state_dir,
        {
            "phase": "diary_open_entry",
            "cycle_id": cycle_id or "",
            "outcome": outcome,
            "commit_sha": commit_sha or None,
            "reason": reason or None,
        },
    )


#: #1852 (ADR-031 rule 5): the planning session's own outcome, journalled
#: unconditionally -- a failed session must fail open (the cycle proceeds
#: on the ranked queue as before) and say so here, not silently. "integrated"
#: is the only success outcome, mirroring diary-open-entry's shape above.
VALID_PLANNING_OUTCOMES = frozenset({
    "integrated", "refused", "malformed", "no_plan", "spawn_failed", "commit_failed", "timed_out",
})


def record_planning_session(
    state_dir: Path,
    cycle_id: str,
    outcome: str,
    *,
    iterations_used: int | None,
    iterations_planned: int | None,
    reason: str = "",
    task_writing_read: bool | None = None,
    task_writing_source: str | None = None,
    task_writing_path: str | None = None,
    task_writing_bytes: int | None = None,
    task_writing_sha256: str | None = None,
) -> None:
    """Log one planning-session run (#1852): whether its plan reached the
    diary, how many of its 20 ticks it used, and its own forecast for the
    cycle it is planning for.

    ``iterations_planned`` is left ``None`` (never coerced to 0) when the
    session did not produce one -- a missing forecast must stay
    distinguishable from a forecast of zero (#1850-class distinction).
    ``outcome`` is coerced to ``"malformed"`` if unrecognized.
    """
    if outcome not in VALID_PLANNING_OUTCOMES:
        outcome = "malformed"
    append_event(
        state_dir,
        {
            "phase": "planning_session",
            "cycle_id": cycle_id or "",
            "outcome": outcome,
            "iterations_used": int(iterations_used) if isinstance(iterations_used, int) else None,
            "iterations_planned": int(iterations_planned) if isinstance(iterations_planned, int) else None,
            "reason": reason or None,
            **({"task_writing_read": task_writing_read} if isinstance(task_writing_read, bool) else {}),
            **({"task_writing_source": task_writing_source} if task_writing_source else {}),
            **({"task_writing_path": task_writing_path} if task_writing_path else {}),
            **({"task_writing_bytes": task_writing_bytes} if isinstance(task_writing_bytes, int) else {}),
            **({"task_writing_sha256": task_writing_sha256} if task_writing_sha256 else {}),
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
    lesson_candidate: dict | None = None,
    verdict: str | None = None,
    verdict_reason: str | None = None,
    executor_llm_error: bool = False,
    lane: str | None = None,
    prompt_fit_rung: str | None = None,
    change_shape: str | None = None,
    main_sha_before: str | None = None,
    real_result: dict | None = None,
    llm_error_classification: dict | None = None,
    iterations_used: int | None = None,
    iterations_limit: int | None = None,
    iterations_predicted: int | None = None,
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

    #1709: ``main_sha_before`` (keyword-only, additive) is written only when
    given — a ``push_pending`` row carries the ``origin/main`` sha the cycle
    merged against, so ``bridge._finish_pending_pushes`` can later tell
    whether ``origin/main`` moved (-> ``superseded``) or is unchanged
    (-> safe to redo the push -> ``pushed_late``) without re-deriving it.
    Omitted by every other caller — byte-identical row shape otherwise.

    #1748: ``real_result`` (keyword-only, additive) carries the five inputs
    ``bridge._is_real_result`` reads from a result artifact
    (``result_status``, ``status``, ``terminal_reason``, ``materialized_from``,
    a derived ``blocker_reason``) plus the derived ``is_real_result`` boolean
    — see ``bridge._real_result_ledger_inputs``, which computes this from the
    same values the caller already has rather than re-reading the artifact.
    Written as a nested ``real_result`` dict only when given (never partial —
    the caller always supplies every key); omitted entirely by any caller
    that does not pass it, exactly like ``lesson_candidate`` above. Result
    artifacts are pruned within ~29 days; this row is the only place this
    question stays answerable afterward, and backfill for rows written
    before this change is not possible (the artifacts they'd need are
    already gone for the oldest of them).

    #1765: ``llm_error_classification`` (keyword-only, additive) carries the
    classifier's decision (``"class": "paused-supplier"|"failed"``) and the
    raw executor LLM-call error text it read, together, whenever the cycle
    carried one — see ``bridge._classify_llm_error``. Recorded so a
    misclassification is auditable after the fact even though the ledger is
    append-only and the row itself cannot be corrected in place. Written as
    a nested dict only when given and non-empty; omitted entirely otherwise,
    the same additive convention as ``real_result``/``lesson_candidate``.
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
    if isinstance(lesson_candidate, dict):
        row["lesson_candidate"] = {
            "condition_met": bool(lesson_candidate.get("condition_met")),
            "queued": bool(lesson_candidate.get("queued")),
            **({"refusal_reason": str(lesson_candidate["refusal_reason"])[:120]}
               if lesson_candidate.get("refusal_reason") else {}),
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
    if prompt_fit_rung:
        row["prompt_fit_rung"] = str(prompt_fit_rung)[:40]
    if change_shape in {"feature", "maintenance", "documentation", "testing", "performance", "knowledge", "unclassified"}:
        row["change_shape"] = change_shape
    if main_sha_before:
        row["main_sha_before"] = str(main_sha_before)
    if isinstance(real_result, dict) and real_result:
        row["real_result"] = {
            "result_status": real_result.get("result_status"),
            "status": real_result.get("status"),
            "terminal_reason": real_result.get("terminal_reason"),
            "materialized_from": real_result.get("materialized_from"),
            "blocker_reason": real_result.get("blocker_reason"),
            "is_real_result": bool(real_result.get("is_real_result")),
        }
    if isinstance(llm_error_classification, dict) and llm_error_classification:
        # #1765: the classifier's decision AND the raw error text it read,
        # together — so a misclassification is auditable after the fact
        # instead of just a bare outcome/reason with the evidence discarded.
        row["llm_error_classification"] = {
            "class": str(llm_error_classification.get("class") or ""),
            "raw_error": str(llm_error_classification.get("raw_error") or "")[:400],
        }
    if iterations_used is not None and isinstance(iterations_used, int):
        # #1850: record the cycle's actual iteration consumption against the limit
        # active in this cycle, plus the fraction consumed and forecast placeholder.
        row["iterations_used"] = iterations_used
        if iterations_limit is not None and isinstance(iterations_limit, int) and iterations_limit > 0:
            row["iterations_limit"] = iterations_limit
            row["iteration_fraction"] = round(iterations_used / iterations_limit, 4)
        if iterations_predicted is not None and isinstance(iterations_predicted, int):
            row["iterations_predicted"] = iterations_predicted
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


def read_events_across_rotation(
    state_dir: Path, *, phases: "frozenset[str] | set[str] | None" = None, max_archives: int = 8,
) -> list[dict]:
    """Rows across rotation, OLDEST FIRST: up to *max_archives* newest
    ``cycles-YYYY-MM-DD.jsonl.gz`` archives, then the active file.

    Unlike :func:`read_events` (same-day only, by design, for same-day
    checks -- see its own docstring), this is for a caller that needs a
    window spanning more than one day: a rolling-window report, a
    multi-day trend. #1178/#1207's "rotation narrows every reader" class
    is a reader that opens only the live file; the opposite failure
    (unbounded memory from reading the ledger's entire history) is
    avoided here by capping at *max_archives*, not by narrowing back to
    the live file the way :func:`read_events` does on purpose.

    *phases* restricts rows to those ``phase`` values (``None`` = every
    row). Best-effort — never raises; a corrupt archive, an unreadable
    line, or a missing directory yields fewer rows, never an exception.
    """
    ledger_dir = _ledger_dir(state_dir)
    rows: list[dict] = []

    def _collect(lines) -> None:
        for line in lines:
            with contextlib.suppress(Exception):
                row = json.loads(line)
                if isinstance(row, dict) and (phases is None or row.get("phase") in phases):
                    rows.append(row)

    with contextlib.suppress(Exception):
        archives = sorted(ledger_dir.glob("cycles-*.jsonl.gz"))
        for gz_path in archives[-max_archives:]:
            with contextlib.suppress(Exception):
                with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as fh:
                    _collect(fh)
    with contextlib.suppress(Exception):
        with open(ledger_dir / _LEDGER_FILENAME, encoding="utf-8") as fh:
            _collect(fh)
    return rows


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
