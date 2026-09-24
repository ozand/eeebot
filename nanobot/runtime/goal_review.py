"""Periodic, bounded, LLM-driven goal-review (#768).

Replaces the last manual link in the demand-driven loop (#760): the operator
hand-writing "Priority N — ..." entries into goal_text. At most once per day
(own watermark, ``<state_dir>/goal_review/last_run.json`` — NOT the 10-min
cycle), and only behind the ``SELFEVO_GOAL_REVIEW_ENABLED`` kill switch
(default OFF — absent/falsy is a hard no-op), this module asks the LLM to
formulate 1-3 concrete bounded priorities from the goal vectors and the
loop's own measured evidence, then APPENDS the validated ones — as if they
had landed in goal_text's "Current priority targets" section, the SAME R30
channel operator seeding uses (``<state_dir>/goals/goal_text.json``) — so
the wake-up mechanics (#760) and done-detection (#748/#773) are untouched.
Since #860 the append target is actually a separate harness-owned sidecar
(see the "Canon split" note below); readers merge it in before parsing so
this remains invisible to them.

Grounding (the difference from the retired hypothesis generator):

- **Inputs are measurements, not vibes.** The context is bounded and built
  from durable state only: the goal vectors verbatim, the latest scorecard
  snapshot (#765) including its ``gaps`` (the same gap list
  ``demand._goal_gap_items`` presents as ``goal-gap`` demand — read from
  the persisted snapshot here, never via ``collect_demand``, which would
  recurse into the scorecard recompute this function rides), usage/decay
  evidence (#761), and recent integration history from the rotation-aware
  ledger reader (``scorecard._ledger_rows``).
- **Fail-closed validation** (the #751 serves-validator pattern): every
  produced priority MUST cite (a) the goal vector it serves (``V1``/``V2``
  only — the FUTURE section can never be served) and (b) one evidence id
  actually presented in the inputs (``E1``, ``E2``, ...). A priority
  missing either is REJECTED with a recorded reason; zero valid priorities
  is an honest no-op.
- **Append-only, dedup, operator-safe.** Operator entries are never
  rewritten or removed; new entries continue numbering (dynamically, at
  merge time — see the "Canon split" note below) from the highest existing
  "Priority N" anywhere in the merged text (including the Completed
  paragraph, so retired numbers are never reused) and are formatted
  ``(<letter>) Priority N — <label>: <body>`` — the exact shape
  ``demand._PRIORITY_PATTERN`` / ``cycle_planning._priority_label_prefix``
  parse, so generated priorities flow through demand collection and
  done-detection identically to operator-seeded ones. A candidate whose
  label matches an existing entry is rejected as a duplicate.
- **One small bite per priority** (the P15/P16 host-model lesson): the
  prompt requires each priority to be a single-function change of at most
  ~40 lines in one file — never a multi-part task.
- **Ledger.** Every review appends one ``phase: "goal_review"`` row
  (``inputs_hash``, produced titles, rejections with reasons, outcome) via
  the same ``append_event`` helper every other phase uses.

Wiring: invoked from ``scorecard.compute_scorecard``'s recompute path
(the ``run_heldout``/``update_explorer`` pattern), wrapped fail-open — a
review bug must never break the scorecard or demand collection. Everything
here is fail-open/fail-closed by design and never raises into the caller.

**#879 tech-tree soft bias.** ``tech_tree.current_direction`` (the loop's
current preferred improvement domain, e.g. "proposer-quality") is
consulted to softly REORDER the LLM's own candidate list before
validation — a stable sort, nothing dropped, mirroring the #815
V1-over-V2 demand bias exactly: only the ``_MAX_PRIORITIES`` cap can ever
leave a non-aligned candidate unaccepted. An accepted priority whose own
text matches the current direction's tokens is tagged
``direction: "<name>"`` in ``derived_priorities.json`` (attribution only;
untagged/no match is the common case and is never penalized).

**Canon split (#860).** ``goal_text.json`` is the OPERATOR's canon — every
release's ``deploy_release.sh`` unconditionally reseeds it from the repo,
which used to erase every accepted priority this module ever appended (the
"Priority 17" minted three days running). Accepted priorities are now
appended-only to a separate, harness-owned sidecar,
``<state_dir>/goals/derived_priorities.json`` (see
:func:`read_derived_priorities`), that deploy never touches. Priority
NUMBERS are never stored there — they are assigned dynamically, at merge
time, by :func:`merged_goal_text`, which folds the derived entries onto
whatever goal_text the operator currently has using the exact same
:func:`append_priorities` insertion/numbering logic. The only two readers
that need to see derived priorities (``demand._priority_items`` and
``llm_proposer._load_goal_text``) call :func:`merged_goal_text` before
parsing; ``goal_text.json`` itself is never written by this module anymore.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime.cycle_ledger import append_event
from nanobot.runtime.operator_documents import (
    STATE_TEXT,
    derived_priorities_path,
    resolve_charter,
    resolve_derived_priorities,
    resolve_operator_priorities_metadata,
)
from nanobot.runtime.role_prompt import load_role_text

ENABLED_ENV = "SELFEVO_GOAL_REVIEW_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}

_REVIEW_INTERVAL_HOURS = 24
_WATERMARK_SCHEMA = "goal-review-watermark-v1"

_MAX_PRIORITIES = 3
_MAX_LABEL_CHARS = 40  # cycle_planning._PRIORITY_LABEL_PATTERN caps at 40
_MAX_BODY_CHARS = 600
_MAX_GOAL_CHARS = 6000
_MAX_SNAPSHOT_CHARS = 2500
_MAX_EVIDENCE_LINES = 12
_MAX_HISTORY_ROWS = 10
_MIN_EVIDENCE_SUBSTRING = 12
_DECAY_DAYS = 14  # kept in sync with demand._DECAY_DAYS

# #860: harness-owned canon for accepted priorities, separate from the
# operator's goal_text.json (which deploy_release.sh reseeds every release).
# No priority numbers stored — see merged_goal_text.
_DERIVED_PRIORITIES_SCHEMA = "derived-priorities-v1"
_DERIVED_PRIORITIES_MAX = 10

# Label must survive cycle_planning._priority_label_prefix
# (``Priority\s+\d+\s*[—–-]\s*[^:.(]{1,40}``) for done-detection: no colon,
# period, or parenthesis anywhere in the label.
_LABEL_FORBIDDEN_CHARS = set(":.()")

_PRIORITY_MARKER = "Current priority targets:"
_COMPLETED_MARKER = "\n\nCompleted"

# Same regex family as demand._PRIORITY_PATTERN /
# llm_proposer._PRIORITY_PATTERN — one entry per
# "(A) Priority N — Title: instructions" line.
_PRIORITY_PATTERN = re.compile(
    r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)",
    re.DOTALL,
)
_PRIORITY_NUM_RE = re.compile(r"Priority\s+(\d+)")
_EVIDENCE_ID_RE = re.compile(r"^[Ee](\d{1,3})$")

# #1596: stable, replayable names for the sources that actually contributed
# at least one citable line to a review. They are ledger provenance, not
# demand kinds or priority metadata.
# ADR-020 Rule 2: Night contour candidates require at least one citation (link)
# to a ledger row, commit, or measurement.
_EVIDENCE_SOURCES = frozenset({
    "scorecard_gaps",
    "decay",
    "supported_hypotheses",
    "night_contour_candidates",
})

# Citations: ledger cycle (e.g. cycle-eeb3c220d25b), git commit (7-40 hex chars),
# or measurement (e.g. 42ms, 3.5s, 85%, 120MB, 14.2kb, 100ops).
_CYCLE_CITATION_RE = re.compile(r"\bcycle-[A-Za-z0-9][A-Za-z0-9_-]+\b", re.I)
_COMMIT_CITATION_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)
_MEASUREMENT_CITATION_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds?|%|percent|bytes?|kb|mb|gb|tb|ops|ops/s|cycles?)\b",
    re.I,
)
_DIRECTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# #1729 (ADR-022 rule 2): the role text lives in ``roles/goal-review.md`` at
# the release root; ``llm_proposer.propose`` prepends identity, soul and the
# charter for this role.
_GOAL_REVIEW_SYSTEM_PROMPT = load_role_text("goal-review")[0]


# ─── small shared helpers (same shapes as demand.py / scorecard.py) ─────────


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


def _enabled() -> bool:
    """Hard kill switch, default OFF (#768 rollout control surface): only an
    explicit truthy value enables the review; absent/falsy is a no-op."""
    return os.environ.get(ENABLED_ENV, "0").strip().lower() in _TRUTHY


# ─── daily watermark ────────────────────────────────────────────────────────


def _watermark_path(state_dir: Path) -> Path:
    return Path(state_dir) / "goal_review" / "last_run.json"


def _due(state_dir: Path, now: datetime) -> bool:
    """True iff no valid watermark exists or :data:`_REVIEW_INTERVAL_HOURS`
    have elapsed since the recorded last run. A malformed watermark reads as
    due (fail-open toward reviewing — the run itself rewrites it)."""
    data = _read_json(_watermark_path(state_dir), None)
    last = _parse_ts(data.get("last_run_utc")) if isinstance(data, dict) else None
    if last is None:
        return True
    return (now - last) >= timedelta(hours=_REVIEW_INTERVAL_HOURS)


def _write_watermark(state_dir: Path, now: datetime) -> None:
    _write_json(
        _watermark_path(state_dir),
        {"schema_version": _WATERMARK_SCHEMA, "last_run_utc": _iso(now)},
    )


# #944: product-root charter filename. goals.md ships in the release tree
# and is read from the release root (e.g. /opt/.../current/goals.md) —
# immutable under ProtectSystem=strict. State/goals/goal_text.json now holds
# operator-seeded priorities ONLY (no charter text).
_GOALS_MD_FILENAME = "goals.md"


def read_charter_text(release_root: "Path | None") -> str:
    """Read the immutable operator charter from ``goals.md`` in the
    ``release_root`` (the deployed release tree, e.g.
    ``/opt/eeepc-agent/runtimes/self-evolving-agent/current``). Returns
    the charter text, or ``""`` when the file is absent, unreadable, or
    ``release_root`` is ``None`` — callers must treat empty as
    unavailable. Fail-open: never raises.

    ADR-034 rule 2: delegates to :func:`operator_documents.resolve_charter`,
    the charter's one resolver — this wrapper exists only so the 7 existing
    callers keep their ``str``-returning contract unchanged."""
    res = resolve_charter(release_root)
    return res.text.strip() if res.state == STATE_TEXT else ""


def active_goal_id(state_dir: "Path | str") -> str:
    """The active goal's id, from the operator's ``goals/goal_text.json``.

    #1222: the coordinator used to rewrite ``goals/registry.json`` /
    ``active.json`` every cycle; those files froze on 2026-08-22 when it was
    deleted (#916/#923) and four readers kept treating them as live. The
    operator canon — ``goal_text.json``, seeded by ``deploy_release.sh`` —
    has carried ``goal_id`` all along, so it is the one source now. Returns
    ``""`` when the file is absent, unreadable or has no id. Fail-open:
    never raises.

    ADR-034 rule 2: delegates to
    :func:`operator_documents.resolve_operator_priorities_metadata` — the
    operator-priorities document's one resolver — instead of constructing
    the ``goal_text.json`` path itself."""
    return resolve_operator_priorities_metadata(state_dir).goal_id


# ─── bounded inputs ─────────────────────────────────────────────────────────


def _load_goal_data(
    state_dir: Path, release_root: "Path | None" = None
) -> "dict[str, Any] | None":
    """Charter text for the goal review context.

    #944: when ``release_root`` is given and ``goals.md`` exists there,
    the immutable charter is read from that file (release-tree read-only
    path). Derived priorities are folded in separately by
    :func:`merged_goal_text` in :func:`maybe_goal_review`.

    ADR-034 rule 2/3: charter + derived, exactly as before this migration
    (rule 3's own table: "review runs on charter + derived" when operator
    priorities are absent/unreadable — goal review's inputs were never
    supposed to include them). The operator's priority text
    (``goal_text.json``) is NEVER used as a charter SUBSTITUTE either — the
    #944-era fallback that mislabeled it as the charter is deleted, not
    kept. Returns ``None`` when the charter is absent/unreadable — the
    review must no-op BEFORE any LLM call.
    """
    charter = read_charter_text(release_root)
    if not charter:
        return None
    return {"text": charter}


def _derived_priorities_path(state_dir: Path) -> Path:
    """The writer's path (``_write_derived_priorities`` below) — ADR-034
    rule 2 is about readers; this delegates to
    :func:`operator_documents.derived_priorities_path` so reader and writer
    never drift onto two different paths, but the resolver owns the path."""
    return derived_priorities_path(Path(state_dir))


def read_derived_priorities(state_dir: Path) -> list[dict[str, Any]]:
    """Loop-derived priorities accepted by past reviews, not yet folded into
    the operator's goal_text canon (#860) — from the harness-owned
    ``derived_priorities.json`` sidecar deploy never touches. Each entry has
    ``label``/``body``/``vector``/``added_utc``; no priority number (numbers
    are assigned dynamically at merge time by :func:`merged_goal_text`).

    ADR-034 rule 2: a thin dict-shaped adapter over
    :func:`operator_documents.resolve_derived_priorities` — the resolver
    locates, cap-checks and validates the file; this function never reads
    it itself. Malformed entries are dropped individually (by the
    resolver); fail-open to ``[]`` when absent/unreadable."""
    res = resolve_derived_priorities(state_dir)
    if res.state != STATE_TEXT:
        return []
    out: list[dict[str, Any]] = []
    for e in res.entries:
        item: dict[str, Any] = {
            "label": e.title,
            "body": e.instructions,
            "vector": e.vector,
            "number": e.number,
            "added_utc": e.added_utc,
        }
        # #879: which tech-tree investment direction was current at mint
        # time, when the priority's own text matched it — additive,
        # OMITTED entirely (not just "") for any entry that predates this
        # field or never matched one, so existing exact-shape comparisons
        # of older entries are unaffected.
        if e.direction:
            item["direction"] = e.direction
        out.append(item)
    return out


def _write_derived_priorities(state_dir: Path, priorities: list[dict[str, Any]]) -> None:
    """Persist ``priorities`` capped to :data:`_DERIVED_PRIORITIES_MAX`,
    keeping the NEWEST entries (dropping the oldest beyond the cap — callers
    always append new entries at the end of the list). Deliberately NOT
    wrapped in try/except: a persist failure must propagate to
    ``maybe_goal_review``'s outer handler so the ledger records "error",
    never "appended"-without-persist. Known bounded tradeoff: an evicted
    still-open entry leaves the dedup baseline and could be re-minted with
    a fresh number (needs {cap} accumulated at 1-3 accepts/day)."""
    capped = priorities[-_DERIVED_PRIORITIES_MAX:]
    _write_json(
        _derived_priorities_path(Path(state_dir)),
        {"schema_version": _DERIVED_PRIORITIES_SCHEMA, "priorities": capped},
    )


def active_derived_priorities(state_dir: Path, raw_text: str) -> list[dict[str, Any]]:
    """Return the list of derived priorities active against ``raw_text``.

    Skips derived entries whose label or number already exists in ``raw_text``.
    This exposes the exact derived priorities that :func:`merged_goal_text`
    would fold in, preserving structural origin for callers that need
    to distinguish operator charter entries from self-derived entries (#1665).
    """
    try:
        derived = read_derived_priorities(state_dir)
        if not derived:
            return []
        existing = _existing_priority_labels(raw_text)
        existing_numbers = _existing_priority_numbers(raw_text)
        return [
            {
                "label": d["label"],
                "body": d["body"],
                "vector": d["vector"],
                "number": d["number"],
            }
            for d in derived
            if _normalize_label(d["label"]) not in existing
            and int(d.get("number") or 0) not in existing_numbers
        ]
    except Exception:
        return []


def _derived_priorities_as_text(state_dir: Path) -> str:
    """Every currently-listed derived priority (``derived_priorities.json``,
    open or completed alike — this is a dedup/numbering baseline, not a
    reader's filtered view), rendered in the same ``"(<letter>) Priority N
    — Label (VECTOR): Body"`` shape :func:`append_priorities` writes.

    ADR-034 rule 4: a standalone text, on its own, never appended onto the
    operator's — :func:`_existing_priority_labels`/:func:`_next_priority_number`
    are called on this AND on the operator's raw text separately, and their
    results unioned, rather than on one blob :func:`merged_goal_text` used
    to build (#860/#1665's merge this migration replaces)."""
    entries = read_derived_priorities(state_dir)
    if not entries:
        return ""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lines = [
        f"({letters[i % 26]}) Priority {e['number']} — {e['label']} ({e['vector']}): {e['body']}"
        for i, e in enumerate(entries)
    ]
    return _PRIORITY_MARKER + "\n" + "\n".join(lines)


def merged_goal_text(state_dir: Path, raw_text: str) -> str:
    """``raw_text`` (the operator's goal_text) with every derived priority
    (#860) folded in via the SAME :func:`append_priorities` insertion/
    numbering logic goal_review uses when minting — never duplicated. With
    no derived priorities this returns ``raw_text`` completely UNCHANGED
    (byte-identical), so a harness with the kill switch off, or with no
    accepted priorities yet, sees zero behavior change. The two priority
    readers (``demand._priority_items``, ``llm_proposer._load_goal_text``)
    call this before parsing so a deploy's goal_text reseed can never erase
    a derived priority out from under them. Fail-open to ``raw_text``."""
    try:
        entries = active_derived_priorities(state_dir, raw_text)
        if not entries:
            return raw_text
        new_text, _titles = append_priorities(raw_text, entries)
        return new_text
    except Exception:
        return raw_text


def _collect_evidence(
    state_dir: Path,
    selfevo_repo: Path | None,
    snapshot: dict[str, Any],
    now: datetime,
    *,
    sources: set[str] | None = None,
    source_status: dict[str, str] | None = None,
    scorecard_available: bool = True,
) -> dict[str, str]:
    """Evidence lines keyed ``E1``.. — the citable ground truth.

    When ``sources`` is supplied, it is populated only with a stable source
    name that contributed at least one line.

    ADR-020 Rule 1: Scorecard gaps remain diagnostic metrics and ranking inputs,
    but cease to be a source of work. They are NOT added as citable evidence lines,
    preventing metric self-pumping. Scorecard readability is still tracked in
    ``source_status["scorecard_gaps"]`` for decision provenance (#1596).
    """
    lines: list[str] = []
    source_starts: dict[str, int] = {}
    scorecard_readable = scorecard_available and isinstance(snapshot.get("gaps"), list)
    try:
        # Validate that gaps structure is readable if present
        for gap in snapshot.get("gaps") or []:
            if not isinstance(gap, dict):
                continue
    except Exception:
        scorecard_readable = False
    if source_status is not None:
        source_status["scorecard_gaps"] = "complete" if scorecard_readable else "unavailable"
    # ADR-020 Rule 1: No gap lines are added to `lines`. Scorecard gaps do not mint priorities.
    decay_start = len(lines)
    decay_available = True
    try:
        from nanobot.runtime import usage_evidence

        if selfevo_repo:
            stale, decay_status = usage_evidence.stale_artifacts_status(
                Path(state_dir), selfevo_repo, older_than_days=_DECAY_DAYS, now=now
            )
            decay_available = decay_status == "complete"
            for record in stale[:5]:
                rel = str(record.get("path") or "").strip()
                since = str(record.get("stale_since") or "").strip()[:10]
                if rel:
                    lines.append(
                        f"decay: {rel} has no harness-observed use or modification "
                        f"since {since or 'unknown'} ({_DECAY_DAYS}+ days; goal vector V2)"
                    )
    except Exception:
        decay_available = False
    if source_status is not None:
        source_status["decay"] = "complete" if decay_available else "unavailable"
    if sources is not None and len(lines) > decay_start:
        sources.add("decay")
        source_starts["decay"] = decay_start
    hypothesis_start = len(lines)
    hypothesis_available = True
    # #878: a hypothesis the harness-computed verdict marked "supported"
    # (measured evidence only — never the instance's own claim, see
    # ``hypothesis_verdict``'s trust-boundary note) surfaces here exactly
    # like any other evidence source — it still has to be CITED by id and
    # pass ``validate_priority`` like every other candidate; this is the
    # smallest correct integration point, not a separate mint path.
    try:
        from nanobot.runtime import hypothesis_backlog

        _lifecycle, lifecycle_status = hypothesis_backlog._load_lifecycle_status(state_dir)
        hypothesis_available = lifecycle_status != "corrupt"
        for hyp in hypothesis_backlog.supported_hypotheses(state_dir):
            title = str(hyp.get("title") or "").strip()
            if not title:
                continue
            source = str((hyp.get("evidence") or {}).get("source") or "measured")
            lines.append(
                f"supported hypothesis: {title} (harness verdict: supported, "
                f"source: {source}; goal vector V1)"
            )
    except Exception:
        hypothesis_available = False
    if source_status is not None:
        source_status["supported_hypotheses"] = (
            "complete" if hypothesis_available else "unavailable"
        )
    if sources is not None and len(lines) > hypothesis_start:
        sources.add("supported_hypotheses")
        source_starts["supported_hypotheses"] = hypothesis_start
    # ADR-020 Rule 2: Night contour candidates (reflector and curator)
    # propose candidates with citations (ledger cycle, commit, measurement)
    # into the existing demand/goal review pipeline.
    night_start = len(lines)
    night_available = True
    try:
        from nanobot.runtime import knowledge_curator

        curator_pool = knowledge_curator.load_reflector_pool(Path(state_dir))
        # Reflector pool is available if load_reflector_pool succeeded (returns dict)
        clusters = curator_pool.get("clusters") or []
        for cluster in clusters:
            detail = str(cluster.get("detail") or "").strip()
            if not detail:
                continue
            cycles = cluster.get("cycles") or []
            cycles_str = f" [cycles: {', '.join(cycles[:3])}]" if cycles else ""
            lines.append(
                f"night contour: {detail}{cycles_str} (reflector/curator recommendation; goal vector V1)"
            )
    except Exception:
        night_available = False
    if source_status is not None:
        source_status["night_contour_candidates"] = (
            "complete" if night_available else "unavailable"
        )
    if sources is not None and len(lines) > night_start:
        sources.add("night_contour_candidates")
        source_starts["night_contour_candidates"] = night_start

    limited_lines = lines[:_MAX_EVIDENCE_LINES]
    if sources is not None:
        sources.intersection_update(
            source for source, start in source_starts.items() if start < len(limited_lines)
        )
    return {f"E{i}": line for i, line in enumerate(limited_lines, start=1)}


def _integration_history(state_dir: Path, now: datetime) -> list[str]:
    """Bounded digest of recent terminal ledger outcomes, via the
    rotation-aware reader ``scorecard._ledger_rows`` (#773 lesson — a
    single-file read goes blind at midnight). Fail-open to ``[]``."""
    try:
        from nanobot.runtime import scorecard

        rows = [r for r in scorecard._ledger_rows(Path(state_dir), now) if r.get("phase") == "outcome"]
        rows.sort(key=lambda r: str(r.get("ts") or ""))
        out: list[str] = []
        for row in rows[-_MAX_HISTORY_ROWS:]:
            outcome = str(row.get("outcome") or "unknown")
            reason = str(row.get("reason") or "").strip()
            branch = str(row.get("branch") or row.get("cycle_id") or "").strip()
            out.append(f"{outcome}: {reason or branch or '(no detail)'}"[:160])
        return out
    except Exception:
        return []


def _snapshot_digest(snapshot: dict[str, Any]) -> str:
    """The snapshot's metric sections as compact JSON (``gaps`` excluded —
    they are presented separately as citable evidence), bounded."""
    try:
        sections = {
            k: v
            for k, v in snapshot.items()
            if k in ("loop", "cost", "quality", "value", "heldout", "window_days")
        }
        text = json.dumps(sections, ensure_ascii=False, sort_keys=True)
        return text[:_MAX_SNAPSHOT_CHARS]
    except Exception:
        return ""


def build_context(
    goal_text: str,
    snapshot: dict[str, Any],
    evidence: dict[str, str],
    history: list[str],
    derived_text: str = "",
) -> str:
    """Bounded review context: goal vectors verbatim, derived priorities
    (own section, ADR-034 rule 4 — never folded into the goal text),
    scorecard digest, the citable evidence lines (id-keyed), recent
    integration history."""
    parts = [
        "## Goal vectors (verbatim)",
        goal_text.strip()[:_MAX_GOAL_CHARS] or "(no goal text)",
        "",
        "## Derived priorities (source: derived; already accepted by past reviews)",
        derived_text.strip()[:_MAX_GOAL_CHARS] or "(none)",
        "",
        "## Scorecard snapshot (last 7 days)",
        _snapshot_digest(snapshot) or "(no scorecard snapshot)",
        "",
        "## Evidence (cite exactly one id per priority)",
        "\n".join(f"- {eid}: {line}" for eid, line in evidence.items()) or "(none)",
        "",
        "## Recent integration history (most recent last)",
        "\n".join(f"- {line}" for line in history) or "(no ledger history)",
    ]
    return "\n".join(parts)


# ─── fail-closed validation (the #751 serves-validator pattern) ─────────────


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().lower())


# #815: a goal_review-minted entry's title carries a trailing "(V1)"/"(V2)"
# tag (see ``append_priorities``) — stripped here so a future candidate's
# tag-free label still matches it for dedup (the vector tag is metadata,
# not part of the title's identity).
_TRAILING_VECTOR_TAG_RE = re.compile(r"\s*\((V1|V2)\)\s*$")


def _existing_priority_labels(goal_text: str) -> set[str]:
    """Normalized titles of every existing "(X) Priority N — Title:" entry —
    the dedup baseline (operator entries are never touched, only avoided)."""
    labels: set[str] = set()
    idx = goal_text.find(_PRIORITY_MARKER)
    section = goal_text[idx + len(_PRIORITY_MARKER):] if idx != -1 else goal_text
    for m in _PRIORITY_PATTERN.finditer(section):
        title = _TRAILING_VECTOR_TAG_RE.sub("", m.group(2))
        labels.add(_normalize_label(title))
    return labels


_PRIORITY_NUMBER_RE = re.compile(r"Priority\s+(\d+)\b")


def _existing_priority_numbers(goal_text: str) -> set[int]:
    """Every ``Priority N`` number appearing ANYWHERE in ``goal_text`` — the
    "Current priority targets:" section and the free-form "Completed (do not
    repeat):" sentence alike (#1640: a stray derived entry whose number was
    already used, active or completed, must never be re-appended. The
    Completed sentence is operator-authored prose with no fixed shape
    ("Priority N (description, commit X)" vs. this module's own
    "Priority N — Title;" render), so it cannot be parsed for a label the
    way :func:`_existing_priority_labels` parses the structured Current
    section — the number is the only thing both shapes share)."""
    return {int(n) for n in _PRIORITY_NUMBER_RE.findall(goal_text)}


def _record_guard_key_event(
    state_dir: Path, outcome: str, key: str, against: str = "",
) -> None:
    """Persist goal-label matching outcome without changing admission policy."""
    try:
        append_event(state_dir, {
            "phase": "guard_key_match", "guard": "priority_labels",
            "outcome": outcome, "key": str(key or "")[:200],
            "against": str(against or "")[:200],
        })
    except Exception:
        pass


def _evidence_cited(evidence_ref: str, evidence: dict[str, str]) -> bool:
    """True iff ``evidence_ref`` names a presented evidence line: an id
    (``E2``, case-insensitive) or a verbatim quote (≥
    :data:`_MIN_EVIDENCE_SUBSTRING` chars appearing inside a line). A
    reference that appears nowhere in the inputs fails — fail-closed."""
    ref = (evidence_ref or "").strip()
    if not ref:
        return False
    if _EVIDENCE_ID_RE.match(ref):
        return ref.upper() in {k.upper() for k in evidence}
    if len(ref) >= _MIN_EVIDENCE_SUBSTRING:
        return any(ref in line or line in ref for line in evidence.values())
    return False


def _has_required_citation(text: str) -> bool:
    """True iff text contains at least one citation to a ledger cycle ID,
    git commit, or measurement. (ADR-020 Rule 2: candidate without at least
    one link is rejected)."""
    return bool(
        _CYCLE_CITATION_RE.search(text)
        or _COMMIT_CITATION_RE.search(text)
        or _MEASUREMENT_CITATION_RE.search(text)
    )


def _is_night_contour_evidence(evidence_ref: str, evidence: dict[str, str]) -> bool:
    """True iff the cited evidence line originates from the night contour
    (reflector / curator)."""
    ref = (evidence_ref or "").strip()
    if not ref:
        return False
    ref_norm = ref.upper()
    for k, v in evidence.items():
        if k.upper() == ref_norm or ref in v or v in ref:
            if "night contour:" in v or "reflector" in v or "curator" in v:
                return True
    return False


def validate_priority(
    candidate: Any,
    evidence: dict[str, str],
    existing_labels: set[str],
) -> tuple[dict[str, str] | None, str]:
    """Validate one produced priority fail-closed. Returns
    ``(normalized, "")`` or ``(None, reason)``. Requirements: a usable
    label (parseable by the done-detection regexes), a bounded body, a
    ``V1``/``V2`` vector reference, an evidence reference that appears in
    the presented inputs, and no duplicate of an existing entry.

    ADR-020 Rule 2: Night contour candidates (reflector and curator)
    require at least one link/citation (ledger cycle, commit, or measurement);
    a candidate without at least one link is rejected on validation."""
    if not isinstance(candidate, dict):
        return None, "not_an_object"
    label = str(candidate.get("label") or "").strip()
    if not label or len(label) > _MAX_LABEL_CHARS:
        return None, "invalid_label"
    if any(ch in _LABEL_FORBIDDEN_CHARS for ch in label):
        return None, "invalid_label"
    body = str(candidate.get("body") or "").strip()
    if not body:
        return None, "invalid_body"
    body = re.sub(r"\s+", " ", body)[:_MAX_BODY_CHARS]
    vector = str(candidate.get("vector") or "").strip().upper()
    if vector not in ("V1", "V2"):
        return None, "missing_vector_reference"
    evidence_ref = str(candidate.get("evidence") or "")
    if not _evidence_cited(evidence_ref, evidence):
        return None, "evidence_not_in_inputs"
    # ADR-020 Rule 2: candidate citing night contour without at least one link is rejected.
    if _is_night_contour_evidence(evidence_ref, evidence):
        combined_citation_text = f"{label} {body} {evidence_ref}"
        ref_norm = evidence_ref.strip().upper()
        for k, v in evidence.items():
            if k.upper() == ref_norm:
                combined_citation_text += f" {v}"
                break
        if not _has_required_citation(combined_citation_text):
            return None, "missing_citation"
    if _normalize_label(label) in existing_labels:
        return None, "duplicate"
    return {"label": label, "body": body, "vector": vector}, ""


# ─── append through the R30 channel ─────────────────────────────────────────


def _next_priority_number(goal_text: str) -> int:
    """One past the highest "Priority N" mentioned ANYWHERE in the text —
    including the Completed paragraph, so retired numbers are never reused."""
    numbers = [int(m.group(1)) for m in _PRIORITY_NUM_RE.finditer(goal_text)]
    return (max(numbers) + 1) if numbers else 1


def _next_entry_letter(goal_text: str, offset: int) -> str:
    idx = goal_text.find(_PRIORITY_MARKER)
    section = goal_text[idx + len(_PRIORITY_MARKER):] if idx != -1 else ""
    count = sum(1 for _ in _PRIORITY_PATTERN.finditer(section)) + offset
    return chr(ord("A") + count) if count < 26 else "Z"


def append_priorities(goal_text: str, accepted: list[dict[str, str]]) -> tuple[str, list[str]]:
    """Append ``accepted`` priorities to the "Current priority targets"
    section append-only: existing text is never rewritten or reordered; new
    ``(<letter>) Priority N — <label> (<vector>): <body>`` lines are
    inserted after the last existing entry (before the Completed paragraph
    when present). The inline ``(V1)``/``(V2)`` tag is placed at the END of
    the label, right before the colon — never between the priority number
    and the em-dash, which would break ``_PRIORITY_PATTERN`` (here and in
    ``demand.py``) and ``cycle_planning._PRIORITY_LABEL_PATTERN`` — so
    ``demand._priority_items`` can parse the vector back out later (#815).
    Returns ``(new_text, titles)`` — ``titles`` stay tag-free (used for the
    ledger row and the caller-facing return value only)."""
    number = _next_priority_number(goal_text)
    entry_lines: list[str] = []
    titles: list[str] = []
    for offset, cand in enumerate(accepted):
        letter = _next_entry_letter(goal_text, offset)
        # #860 review: an entry may carry a preassigned stable number (a
        # derived priority stores the number it was minted with, so its
        # rendered title — and thus its demand item id — never shifts when
        # a deploy reseed changes the operator's priority count).
        n = int(cand.get("number") or 0) or (number + offset)
        entry_lines.append(
            f"({letter}) Priority {n} — {cand['label']} ({cand['vector']}): {cand['body']}"
        )
        titles.append(f"Priority {n} — {cand['label']}")
    block = "\n" + "\n".join(entry_lines)

    idx = goal_text.find(_PRIORITY_MARKER)
    if idx == -1:
        return goal_text.rstrip() + "\n\n" + _PRIORITY_MARKER + block, titles
    section_start = idx + len(_PRIORITY_MARKER)
    completed_pos = goal_text.find(_COMPLETED_MARKER, section_start)
    insert_at = completed_pos if completed_pos != -1 else len(goal_text)
    return goal_text[:insert_at].rstrip("\n") + block + goal_text[insert_at:], titles


# ─── LLM call (reuses the proposer's provider plumbing, #707) ───────────────


def _call_llm(context: str) -> dict[str, Any] | None:
    """One chat completion through ``llm_proposer.propose`` — the same
    LiteLLM gateway/env/reply-extraction plumbing the proposer uses (#707);
    no new client code. Fails open to ``None``."""
    from nanobot.runtime import llm_proposer

    return llm_proposer.propose(context, system_prompt=_GOAL_REVIEW_SYSTEM_PROMPT)


# ─── ledger ─────────────────────────────────────────────────────────────────


def goal_review_retention(row: Any) -> dict[str, Any]:
    """Read retained review provenance without confusing legacy with empty.

    New #1596 rows contain both keys, including ``[]`` and ``None`` for a
    known-empty source set or known-absent Direction. Older rows omit them,
    and therefore return ``status="unavailable"`` rather than fabricated
    empty facts. This is the sole reader contract for #1642's future replay.
    """
    if not isinstance(row, dict) or row.get("phase") != "goal_review":
        return {"status": "unavailable", "evidence_sources": None, "direction": None}
    raw_sources = row.get("evidence_sources")
    if (
        row.get("retention_status") != "complete"
        or "evidence_sources" not in row
        or "direction_at_review" not in row
    ):
        return {"status": "unavailable", "evidence_sources": None, "direction": None}
    if (
        not isinstance(raw_sources, list)
        or len(set(raw_sources)) != len(raw_sources)
        or any(not isinstance(source, str) or source not in _EVIDENCE_SOURCES for source in raw_sources)
    ):
        return {"status": "unavailable", "evidence_sources": None, "direction": None}
    raw_direction = row.get("direction_at_review")
    if raw_direction is not None and (
        not isinstance(raw_direction, str)
        or not _DIRECTION_NAME_RE.fullmatch(raw_direction)
    ):
        return {"status": "unavailable", "evidence_sources": None, "direction": None}
    return {
        "status": "complete",
        "evidence_sources": tuple(raw_sources),
        "direction": raw_direction,
    }


def _record_review(
    state_dir: Path,
    outcome: str,
    *,
    inputs_hash: str = "",
    produced: list[str] | None = None,
    rejected: list[dict[str, str]] | None = None,
    evidence_sources: set[str] | None = None,
    evidence_available: bool = False,
    direction_at_review: str | None = None,
    direction_available: bool = False,
) -> None:
    """One ``phase: "goal_review"`` ledger row per review run.

    New rows retain the citable-source set and selected Direction as they were
    at the decision point. ``None`` means no Direction was present then. The
    record is complete only when all source reads and Direction validation
    completed; otherwise it explicitly remains unavailable. Legacy rows omit
    the fields and also read as unavailable via :func:`goal_review_retention`.
    """
    with contextlib.suppress(Exception):
        append_event(
            state_dir,
            {
                "phase": "goal_review",
                "outcome": outcome,
                "inputs_hash": inputs_hash,
                "produced": list(produced or []),
                "rejected": list(rejected or []),
                # Explicitly distinguish a captured empty source set from an
                # unavailable capture. Legacy rows lack this status entirely.
                "retention_status": (
                    "complete" if evidence_available and direction_available else "unavailable"
                ),
                "evidence_sources": sorted(evidence_sources) if evidence_sources is not None else None,
                "direction_at_review": direction_at_review,
            },
        )


# ─── public entrypoint ──────────────────────────────────────────────────────


def maybe_goal_review(
    state_dir: Path,
    selfevo_repo: "Path | None",
    *,
    now: "datetime | None" = None,
    release_root: "Path | None" = None,
) -> "list[str] | None":
    """Run the periodic goal-review if enabled and due (#768).

    ``release_root`` is the deployed release tree path (e.g.
    ``/opt/eeepc-agent/runtimes/self-evolving-agent/current``); when
    present and ``goals.md`` exists there, the immutable charter is read
    from that file (#944). Callers that do not supply this fall back to
    the pre-#944 behavior (charter embedded in goal_text.json).

    Returns ``None`` on a hard no-op (kill switch off, watermark not yet
    elapsed, or an internal error) — in the switch-off case NOTHING is
    written, not even the watermark. Otherwise returns the list of appended
    priority titles (possibly empty) and always leaves exactly one
    ``goal_review`` ledger row. Never raises into the caller."""
    if not _enabled():
        return None
    # Preserve whatever is known if a later review step fails. ``None`` for
    # sources is distinct from a known-empty set: the source scan never ran.
    evidence_sources: set[str] | None = None
    evidence_available = False
    current_direction: str | None = None
    direction_available = False
    try:
        state_dir = Path(state_dir)
        now = now or datetime.now(timezone.utc)
        if not _due(state_dir, now):
            return None
        # Advance the watermark FIRST: whatever happens below, the next
        # attempt is a day away — a wedged review must not burn one LLM
        # call per 30-min scorecard recompute.
        _write_watermark(state_dir, now)

        if release_root is None:
            configured_root = os.environ.get("RELEASE_ROOT", "").strip()
            release_root = Path(configured_root) if configured_root else None
        # Direction is retained even if the goal channel is absent. It is a
        # read-only fact, not a source of evidence or demand.
        try:
            from nanobot.runtime import tech_tree

            candidate_direction, direction_status = tech_tree.current_direction_status(state_dir)
            if direction_status == "complete" and (
                candidate_direction is None
                or (
                    isinstance(candidate_direction, str)
                    and _DIRECTION_NAME_RE.fullmatch(candidate_direction)
                )
            ):
                direction_available = True
                current_direction = candidate_direction
            else:
                current_direction = None
        except Exception:
            current_direction = None
        goal_data = _load_goal_data(state_dir, release_root)
        if goal_data is None:
            # No R30 channel file — no source scan or LLM call. The row says
            # unavailable rather than fabricating an empty source set.
            _record_review(
                state_dir, "no_goal_text", evidence_sources=evidence_sources,
                evidence_available=evidence_available,
                direction_at_review=current_direction, direction_available=direction_available,
            )
            return []
        goal_text = str(goal_data.get("text") or "")
        # ADR-034 rule 4: dedup/context/numbering see the operator's raw
        # text AND the derived list's own entries, resolved SEPARATELY —
        # never folded into one blob first (that was merged_goal_text,
        # #860/#1665's merge this migration replaces). A priority accepted
        # yesterday (living only in derived_priorities.json now) still
        # blocks a re-mint today; it just does so via its own text rather
        # than text appended onto the operator's.
        derived_text = _derived_priorities_as_text(state_dir)

        snapshot_path = state_dir / "scorecard" / "latest.json"
        snapshot = _read_json(snapshot_path, None)
        scorecard_available = isinstance(snapshot, dict)
        if not scorecard_available:
            snapshot = {}

        # #1596: Direction was captured before goal loading. It remains a
        # ranking preference only; recording it does not create evidence,
        # demand, a scheduler, or a gate.
        evidence_sources = set()
        source_status: dict[str, str] = {}
        evidence = _collect_evidence(
            state_dir, selfevo_repo, snapshot, now, sources=evidence_sources,
            source_status=source_status, scorecard_available=scorecard_available,
        )
        evidence_available = all(
            source_status.get(source) == "complete" for source in _EVIDENCE_SOURCES
        )
        if not evidence:
            # ADR-020 Rule 1: With gaps no longer citable evidence, if there is
            # no decay or supported hypothesis evidence, nothing a priority
            # could cite. Honest no-op, zero LLM calls.
            _record_review(
                state_dir, "no_gaps", evidence_sources=evidence_sources,
                evidence_available=evidence_available,
                direction_at_review=current_direction, direction_available=direction_available,
            )
            return []

        context = build_context(
            goal_text, snapshot, evidence, _integration_history(state_dir, now),
            derived_text=derived_text,
        )
        inputs_hash = hashlib.sha256(context.encode("utf-8", errors="replace")).hexdigest()[:16]

        reply = _call_llm(context)
        candidates = reply.get("priorities") if isinstance(reply, dict) else None
        if not isinstance(candidates, list):
            _record_review(
                state_dir, "invalid_reply", inputs_hash=inputs_hash,
                evidence_sources=evidence_sources, evidence_available=evidence_available,
                direction_at_review=current_direction, direction_available=direction_available,
            )
            return []

        # #879: use the Direction captured at this review's decision point
        # to softly reorder candidates. It is still a ranking preference;
        # nothing is dropped before the existing cap/validation path.
        if current_direction:
            def _direction_rank(cand: Any) -> int:
                if not isinstance(cand, dict):
                    return 1
                text = f"{cand.get('label', '')} {cand.get('body', '')}"
                try:
                    return 0 if tech_tree.matches_direction(text, state_dir, current_direction) else 1
                except Exception:
                    return 1

            candidates = sorted(candidates, key=_direction_rank)

        # ADR-034 rule 4: dedup baseline unions labels from BOTH lists,
        # each scanned on its own text — never one merged blob.
        existing_labels = _existing_priority_labels(goal_text) | _existing_priority_labels(derived_text)
        _record_guard_key_event(
            state_dir, "baseline", "priority_labels", str(len(existing_labels)),
        )
        accepted: list[dict[str, str]] = []
        rejected: list[dict[str, str]] = []

        def _reject(candidate: Any, reason: str) -> None:
            label = ""
            if isinstance(candidate, dict):
                label = str(candidate.get("label") or "").strip()[:_MAX_LABEL_CHARS]
            rejected.append({"label": label, "reason": reason})

        for candidate in candidates:
            if len(accepted) >= _MAX_PRIORITIES:
                _reject(candidate, "exceeds_max")
                continue
            normalized, reason = validate_priority(candidate, evidence, existing_labels)
            if normalized is None:
                _reject(candidate, reason)
                continue
            _record_guard_key_event(state_dir, "hit", str(candidate.get("label") or ""), "priority_labels")
            if _normalize_label(normalized["label"]) in {
                _normalize_label(a["label"]) for a in accepted
            }:
                _reject(candidate, "duplicate")
                continue
            # #879: tag with the direction it was minted under ONLY when
            # its own text actually matches the current direction's
            # tokens — an attribution, not a blanket stamp (a priority
            # that landed despite not matching stays untagged).
            direction_tag = ""
            if current_direction:
                text = f"{normalized['label']} {normalized['body']}"
                try:
                    if tech_tree.matches_direction(text, state_dir, current_direction):
                        direction_tag = current_direction
                except Exception:
                    direction_tag = ""
            normalized["direction"] = direction_tag
            accepted.append(normalized)

        if not accepted:
            _record_review(
                state_dir, "no_valid_priorities", inputs_hash=inputs_hash, rejected=rejected,
                evidence_sources=evidence_sources, evidence_available=evidence_available,
                direction_at_review=current_direction, direction_available=direction_available,
            )
            return []

        # #860: numbering continues past the highest "Priority N" in EITHER
        # list (ADR-034 rule 4: each checked on its own text, never a merged
        # blob) and is ASSIGNED + STORED at accept time, so a derived
        # priority's rendered title — and thus its demand item id — stays
        # stable even when a deploy reseed later changes the operator's
        # priority count (#860 review finding). append_priorities' own
        # render is discarded — only the titles it derives from the
        # already-assigned ``number`` feed the ledger/return payload, so
        # which text it renders onto is immaterial here; goal_text.json is
        # READ-ONLY in this function either way — the accepted entries land
        # in derived_priorities.json, which deploy_release.sh never touches
        # (the actual #860 fix).
        base_number = max(_next_priority_number(goal_text), _next_priority_number(derived_text))
        for offset, cand in enumerate(accepted):
            cand["number"] = base_number + offset
        _, titles = append_priorities(goal_text, accepted)
        now_iso = _iso(now)

        def _derived_entry(cand: dict[str, Any]) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "label": cand["label"],
                "vector": cand["vector"],
                "body": cand["body"],
                "number": cand["number"],
                "added_utc": now_iso,
            }
            if cand.get("direction"):
                entry["direction"] = cand["direction"]
            return entry

        derived_entries = read_derived_priorities(state_dir) + [
            _derived_entry(cand) for cand in accepted
        ]
        _write_derived_priorities(state_dir, derived_entries)

        _record_review(
            state_dir, "appended", inputs_hash=inputs_hash, produced=titles, rejected=rejected,
            evidence_sources=evidence_sources, evidence_available=evidence_available,
            direction_at_review=current_direction, direction_available=direction_available,
        )
        return titles
    except Exception:
        with contextlib.suppress(Exception):
            _record_review(
                Path(state_dir), "error", evidence_sources=evidence_sources,
                evidence_available=evidence_available,
                direction_at_review=current_direction, direction_available=direction_available,
            )
        return None
