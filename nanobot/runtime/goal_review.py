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

_GOAL_REVIEW_SYSTEM_PROMPT = (
    "You are performing a periodic goal review for a bounded self-evolving "
    "runtime on a very slow host. From the goal vectors and the measured "
    "evidence in the context, formulate 1-3 concrete bounded priorities. "
    'Reply with ONLY a JSON object of the form {"priorities": [{"label": '
    '"...", "body": "...", "vector": "V1", "evidence": "E1"}]} — no prose, '
    "no markdown code fences. label: a short title, at most 40 characters, "
    "containing no colon, period, or parentheses. body: one imperative task "
    "description, at most 600 characters. Each priority MUST be one small "
    "bite: a single-function change of at most 40 lines in ONE file — never "
    "a multi-part or multi-file task (the executor is a weak model; large "
    "tasks fail). vector MUST be exactly 'V1' or 'V2' — the goal vector the "
    "priority serves; the FUTURE section is never a valid target. evidence "
    "MUST be exactly one evidence id from the '## Evidence' section (e.g. "
    "'E2') — a priority without a cited, listed evidence line will be "
    "rejected. Do not repeat existing or completed priorities from the goal "
    "text. Prefer proposing Vector-1 (self-improvement of the agent system) "
    "priorities; propose a Vector-2 (interface/transparency) priority only "
    "when no useful Vector-1 improvement is evident from the evidence. If "
    "no evidence line justifies a worthwhile bounded priority, reply with "
    'ONLY {"priorities": []}.'
)


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


# ─── bounded inputs ─────────────────────────────────────────────────────────


def _load_goal_data(state_dir: Path) -> dict[str, Any] | None:
    """The R30 channel file (``goals/goal_text.json``) as a dict with a
    non-empty ``text``, or ``None`` — with no channel file there is nowhere
    to append, so the review must no-op BEFORE any LLM call."""
    data = _read_json(Path(state_dir) / "goals" / "goal_text.json", None)
    if not isinstance(data, dict) or not str(data.get("text") or "").strip():
        return None
    return data


def _derived_priorities_path(state_dir: Path) -> Path:
    return Path(state_dir) / "goals" / "derived_priorities.json"


def read_derived_priorities(state_dir: Path) -> list[dict[str, Any]]:
    """Loop-derived priorities accepted by past reviews, not yet folded into
    the operator's goal_text canon (#860) — read from the harness-owned
    ``derived_priorities.json`` sidecar deploy never touches. Each entry has
    ``label``/``body``/``vector``/``added_utc``; no priority number (numbers
    are assigned dynamically at merge time by :func:`merged_goal_text`).
    Malformed entries are dropped individually; fail-open to ``[]``."""
    data = _read_json(_derived_priorities_path(Path(state_dir)), None)
    if not isinstance(data, dict):
        return []
    raw = data.get("priorities")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        body = str(entry.get("body") or "").strip()
        vector = str(entry.get("vector") or "").strip().upper()
        try:
            number = int(entry.get("number") or 0)
        except (TypeError, ValueError):
            number = 0
        if not label or not body or vector not in ("V1", "V2") or number <= 0:
            continue  # number is required (#860 review: stable demand ids)
        out.append(
            {
                "label": label,
                "body": body,
                "vector": vector,
                "number": number,
                "added_utc": str(entry.get("added_utc") or ""),
            }
        )
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
        derived = read_derived_priorities(state_dir)
        if not derived:
            return raw_text
        # #860 review: skip derived entries whose label the operator has
        # since baked into goal_text itself (or that a reseed re-added) —
        # otherwise the merged text would carry the label twice and demand
        # would mint two items for the same work. Operator canon wins.
        existing = _existing_priority_labels(raw_text)
        entries = [
            {
                "label": d["label"],
                "body": d["body"],
                "vector": d["vector"],
                "number": d["number"],
            }
            for d in derived
            if _normalize_label(d["label"]) not in existing
        ]
        if not entries:
            return raw_text
        new_text, _titles = append_priorities(raw_text, entries)
        return new_text
    except Exception:
        return raw_text


def _collect_evidence(
    state_dir: Path, selfevo_repo: Path | None, snapshot: dict[str, Any], now: datetime
) -> dict[str, str]:
    """Evidence lines keyed ``E1``.. — the citable ground truth. Sources:
    the scorecard snapshot's ``gaps`` (the open goal-gap demand, #765),
    usage/decay evidence (#761), and (#878) harness-verdict-supported
    hypotheses. Bounded to :data:`_MAX_EVIDENCE_LINES`; fail-open per
    source."""
    lines: list[str] = []
    try:
        for gap in snapshot.get("gaps") or []:
            if not isinstance(gap, dict):
                continue
            evidence = str(gap.get("evidence") or "").strip()
            if evidence:
                lines.append(evidence)
    except Exception:
        pass
    try:
        from nanobot.runtime import usage_evidence

        stale = usage_evidence.stale_artifacts(
            Path(state_dir), selfevo_repo, older_than_days=_DECAY_DAYS, now=now
        )
        for record in stale[:5]:
            rel = str(record.get("path") or "").strip()
            since = str(record.get("stale_since") or "").strip()[:10]
            if rel:
                lines.append(
                    f"decay: {rel} has no harness-observed use or modification "
                    f"since {since or 'unknown'} ({_DECAY_DAYS}+ days; goal vector V2)"
                )
    except Exception:
        pass
    # #878: a hypothesis the harness-computed verdict marked "supported"
    # (measured evidence only — never the instance's own claim, see
    # ``hypothesis_verdict``'s trust-boundary note) surfaces here exactly
    # like any other evidence source — it still has to be CITED by id and
    # pass ``validate_priority`` like every other candidate; this is the
    # smallest correct integration point, not a separate mint path.
    try:
        from nanobot.runtime import hypothesis_backlog

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
        pass
    return {f"E{i}": line for i, line in enumerate(lines[:_MAX_EVIDENCE_LINES], start=1)}


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
) -> str:
    """Bounded review context: goal vectors verbatim, scorecard digest, the
    citable evidence lines (id-keyed), recent integration history."""
    parts = [
        "## Goal vectors (verbatim)",
        goal_text.strip()[:_MAX_GOAL_CHARS] or "(no goal text)",
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


def validate_priority(
    candidate: Any,
    evidence: dict[str, str],
    existing_labels: set[str],
) -> tuple[dict[str, str] | None, str]:
    """Validate one produced priority fail-closed. Returns
    ``(normalized, "")`` or ``(None, reason)``. Requirements: a usable
    label (parseable by the done-detection regexes), a bounded body, a
    ``V1``/``V2`` vector reference, an evidence reference that appears in
    the presented inputs, and no duplicate of an existing entry."""
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
    if not _evidence_cited(str(candidate.get("evidence") or ""), evidence):
        return None, "evidence_not_in_inputs"
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


def _record_review(
    state_dir: Path,
    outcome: str,
    *,
    inputs_hash: str = "",
    produced: list[str] | None = None,
    rejected: list[dict[str, str]] | None = None,
) -> None:
    """One ``phase: "goal_review"`` ledger row per review run — inputs hash,
    produced titles, rejections with reasons. Best-effort, never raises."""
    with contextlib.suppress(Exception):
        append_event(
            state_dir,
            {
                "phase": "goal_review",
                "outcome": outcome,
                "inputs_hash": inputs_hash,
                "produced": list(produced or []),
                "rejected": list(rejected or []),
            },
        )


# ─── public entrypoint ──────────────────────────────────────────────────────


def maybe_goal_review(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    now: datetime | None = None,
) -> list[str] | None:
    """Run the periodic goal-review if enabled and due (#768).

    Returns ``None`` on a hard no-op (kill switch off, watermark not yet
    elapsed, or an internal error) — in the switch-off case NOTHING is
    written, not even the watermark. Otherwise returns the list of appended
    priority titles (possibly empty) and always leaves exactly one
    ``goal_review`` ledger row. Never raises into the caller."""
    if not _enabled():
        return None
    try:
        state_dir = Path(state_dir)
        now = now or datetime.now(timezone.utc)
        if not _due(state_dir, now):
            return None
        # Advance the watermark FIRST: whatever happens below, the next
        # attempt is a day away — a wedged review must not burn one LLM
        # call per 30-min scorecard recompute.
        _write_watermark(state_dir, now)

        goal_data = _load_goal_data(state_dir)
        if goal_data is None:
            # No R30 channel file — nowhere to append; no LLM call.
            _record_review(state_dir, "no_goal_text")
            return []
        goal_text = str(goal_data.get("text") or "")
        # #860: dedup/context see goal_text + already-derived priorities
        # merged in — a priority accepted yesterday (living only in
        # derived_priorities.json now) must still block a re-mint today.
        merged_text = merged_goal_text(state_dir, goal_text)

        snapshot = _read_json(state_dir / "scorecard" / "latest.json", None)
        if not isinstance(snapshot, dict):
            snapshot = {}
        evidence = _collect_evidence(state_dir, selfevo_repo, snapshot, now)
        if not evidence:
            # No measured gap and no decay evidence — nothing a priority
            # could cite. Honest no-op, zero LLM calls.
            _record_review(state_dir, "no_gaps")
            return []

        context = build_context(
            merged_text, snapshot, evidence, _integration_history(state_dir, now)
        )
        inputs_hash = hashlib.sha256(context.encode("utf-8", errors="replace")).hexdigest()[:16]

        reply = _call_llm(context)
        candidates = reply.get("priorities") if isinstance(reply, dict) else None
        if not isinstance(candidates, list):
            _record_review(state_dir, "invalid_reply", inputs_hash=inputs_hash)
            return []

        existing_labels = _existing_priority_labels(merged_text)
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
            if _normalize_label(normalized["label"]) in {
                _normalize_label(a["label"]) for a in accepted
            }:
                _reject(candidate, "duplicate")
                continue
            accepted.append(normalized)

        if not accepted:
            _record_review(
                state_dir, "no_valid_priorities", inputs_hash=inputs_hash, rejected=rejected
            )
            return []

        # #860: numbering continues past merged_text's highest "Priority N"
        # (same base append_priorities would use) and is ASSIGNED + STORED
        # at accept time, so a derived priority's rendered title — and thus
        # its demand item id — stays stable even when a deploy reseed later
        # changes the operator's priority count (#860 review finding). The
        # merged render is discarded — only the titles feed the ledger/
        # return payload. goal_text.json is READ-ONLY here; the accepted
        # entries land in derived_priorities.json, which deploy_release.sh
        # never touches (the actual #860 fix).
        base_number = _next_priority_number(merged_text)
        for offset, cand in enumerate(accepted):
            cand["number"] = base_number + offset
        _, titles = append_priorities(merged_text, accepted)
        now_iso = _iso(now)
        derived_entries = read_derived_priorities(state_dir) + [
            {
                "label": cand["label"],
                "vector": cand["vector"],
                "body": cand["body"],
                "number": cand["number"],
                "added_utc": now_iso,
            }
            for cand in accepted
        ]
        _write_derived_priorities(state_dir, derived_entries)

        _record_review(
            state_dir, "appended", inputs_hash=inputs_hash, produced=titles, rejected=rejected
        )
        return titles
    except Exception:
        with contextlib.suppress(Exception):
            _record_review(Path(state_dir), "error")
        return None
