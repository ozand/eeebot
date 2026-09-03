"""Deterministic, LLM-free demand collector for the demand-driven proposer (#760).

The loop was supply-driven: every ~10 minutes the timer asked the LLM to
"invent a task" over a value-poor workspace, and the model — asked to invent —
invented, burning 2-3 LLM calls per cycle on proposals its own self-dedup then
silently rejected (the saturation burn observed live 2026-07-15). This module
is the missing engine half of that loop inversion: a deterministic scan of
the operator queue and the loop's own telemetry that yields structured
**demand items** the LLM may only *select and refine from* — never invent
beyond. With no demand, the proposer makes ZERO LLM calls and the cycle
records an idle heartbeat instead (see ``llm_proposer.should_propose``).

Demand kinds, in trust order (see ``docs/changes/760-demand-driven-proposer/``):

- ``priority`` — remaining (non-completed) "Current priority targets" entries
  from the filtered goal_text. Reuses
  ``goal_text_utils.filter_completed_priorities_from_goal_text`` verbatim —
  done-detection is NOT reimplemented here (#748 owns it). Each entry may
  carry an explicit inline ``(V1)``/``(V2)`` tag (the goal vector it
  serves — never inferred from wording); WITHIN this kind only, items are
  stable-sorted V1-before-V2-before-untagged (#815: bias the primary
  vector, never starve the secondary one).
- ``defect`` — real, recent failures found in state artifacts:
  (a) terminal ledger ``outcome`` rows with a failed/timeout outcome in the
  last 48h (``skipped-*`` outcomes are not defects — they are the dedup stack
  working); (b) failed/blocked subagent result files carrying error text
  (bounded to the most recent :data:`_MAX_RESULT_FILES` files, following
  ``existence_index._MAX_LEDGER_RESULTS``'s bounded-read discipline);
  (c) instance-repo scripts that fail to byte-compile — watermark-gated on
  the repo's git HEAD exactly like ``system_map.update_system_map`` (own
  sidecar under ``<state_dir>/demand/``), so the scan costs nothing when
  HEAD hasn't moved; (d) held-out behavioral-check failures (#780) read
  from ``<state_dir>/heldout/results.json`` — the sidecar the product-side
  ``nanobot.runtime.heldout`` pack writes on the scorecard recompute path.
  The item carries the checker's evidence string: the loop is told WHAT is
  broken without ever seeing the checker itself (the pack is invisible to
  the instance, #603 invariant). Deduped by artifact (the results file is
  keyed by artifact path), bounded to :data:`_MAX_HELDOUT_DEFECTS`; (e) an
  existing ``scripts/*.py`` skill whose harness-observed use went idle in
  the [:data:`_REPAIR_UNUSED_MIN_DAYS`, :data:`_DECAY_DAYS`) band — younger
  than and disjoint from the ``decay`` band below — is proposed as a
  re-wire/repair target (#845, OpenSpace fix_skill) rather than left to
  decay into an archival candidate; (f) validator-harness run results (#925)
  read from ``<state_dir>/validator_harness/last_runs.jsonl`` — the sidecar
  ``nanobot.runtime.validator_harness.run_validator_harness`` maintains for
  built ``check_*``/``validate_*``/``audit_*``/``analyze_*``/``verify_*``
  scripts it actually executed. The MOST RECENT run per script wins; a
  non-zero exit becomes "validator X fails when run" and a positive findings
  count becomes "validator X reports N findings". The harness records NO
  usage evidence of its own (see its module
  docstring): a validator that finds a real problem earns follow-up work
  here, and the metric moves only if that work happens. Bounded to
  :data:`_MAX_VALIDATOR_DEFECTS`; fail-open: any error or a missing sidecar
  yields no validator demand.
- ``goal-gap`` (#765, ordered between ``defect`` and ``skill-candidate``) —
  scorecard metrics violating their goal-derived target
  (``nanobot.runtime.scorecard.goal_gaps``): the deterministic fitness
  snapshot's gap analysis, targets derived from the ORDERED goal vectors
  (V1 primary before V2 secondary within the kind; the goal's FUTURE
  section maps to no metric and generates nothing). Bounded to
  :data:`_MAX_GOAL_GAP_ITEMS`; the scorecard recompute is time-watermarked
  (30 min) so idle cycles stay cheap. The item summary is STABLE per metric
  — ``goal gap: <metric> (<vector>)``, no current value (#778: embedding
  the current value minted a fresh id every recompute, defeating both the
  completed fold and exhaustion; detail lives in ``evidence`` only), and a
  completed goal-gap id is re-presented after
  :data:`_GOAL_GAP_COMPLETED_TTL_DAYS` days — a metric can legitimately
  regress. All other kinds keep permanent completed-suppression.
- ``skill-candidate`` (#1006, ordered after ``defect`` and before ``hypothesis``) — deterministic recurring action sequences that qualify for packaging as skills.
- ``hypothesis`` — ONLY hypotheses carrying measurement evidence: a
  non-empty ``evidence`` or ``metric`` field, or an ``acceptance`` text that
  references a file path actually present in the instance repo. The chronic
  boilerplate candidates ("Use one bounded subagent-assisted review...",
  "Synthesize one new bounded improvement candidate from retired lanes") have
  none of these and MUST NOT qualify (regression-pinned in tests). #878: at
  most ONE hypothesis-kind item is ever minted per call, and even that one
  is suppressed while an active hypothesis already has an unanswered
  in-flight serving cycle (``hypothesis_backlog.has_in_flight_experiment``)
  — the closed loop (hypothesis -> experiment -> harness-measured verdict,
  see ``hypothesis_verdict``) runs at most one experiment at a time.
- ``reflection`` (#1038, ordered LAST after decay — priority > defect >
  goal-gap > skill-candidate > hypothesis > decay > reflection) —
  unconsumed self-reflection recommendations from recent cycles, bounded to
  :data:`_MAX_REFLECTION_ITEMS` and filtered to :data:`_REFLECTION_MAX_AGE_DAYS` days.
- ``decay`` (#761, ordered before reflection — priority > defect > goal-gap >
  skill-candidate > hypothesis > decay > reflection) —
  ``scripts/*.py`` artifacts whose harness-observed ``last_used`` AND
  ``last_touched`` (``nanobot.runtime.usage_evidence`` sidecar) are both
  older than :data:`_DECAY_DAYS` days, presented as demand proposing
  archival/removal — bounded to the :data:`_MAX_DECAY_ITEMS` oldest. NEVER
  auto-deleted: decay flows through the normal proposal+gate pipeline like
  any other demand. Artifacts never observed at all fall back to their git
  last-commit date; if that fails too they are skipped (fail-open toward
  not flagging). ``collect_demand`` also calls
  ``usage_evidence.refresh_usage`` + ``confirm_serves`` (both fail-open and
  watermark-cheap) so the evidence layer stays current without a separate
  scheduler hook.

Each item is ``{kind, id, summary, evidence, affected_path, vector,
direction}`` with a stable ``id`` (hash of kind+summary) used for
exhaustion tracking. ``vector`` (#815) is the goal vector the item serves;
``direction`` (#879) is the tech-tree improvement DOMAIN it corresponds
to when one can be determined (currently only ``goal-gap`` items, via an
exact metric<->lever match — see ``_goal_gap_items``) — items whose
``direction`` matches the tech-tree's current investment pick are
stable-sorted to lead within their existing vector class, a purely
cosmetic reordering that never drops or exhausts anything. Once a demand
item's proposals have been self-dedup-rejected 2+ times (matched via the
``demand_id`` recorded on ``proposer_reject`` ledger rows, #762/#760), the
item is marked exhausted in ``<state_dir>/demand/exhausted.json``
(schema-versioned, like ``hypothesis_backlog``'s lifecycle sidecar) and no
longer presented. Exhaustion resets (#771, live deadlock 2026-07-15) on any
of: a successful integration (terminal ledger ``outcome: success`` row newer
than the entry's ``exhausted_at``), a runtime release change (the release id
recorded in the entry differs from the running one), a repo HEAD move (cheap
``git rev-parse`` re-check), or :data:`_EXHAUSTION_EXPIRY_HOURS` hours
elapsing. An expired entry flips to a ``reset`` record carrying ``reset_at``
so only rejects newer than the reset can re-exhaust the item (otherwise the
old ledger rows would re-exhaust it instantly, defeating expiry). A MISSING
entry behaves the same way — manually clearing ``entries`` is an honest
reset: only rejects newer than the newest of (last success, 24h ago) count,
so stale bug-era ledger rows cannot silently resurrect an exhaustion the
operator just cleared (#771).

Completed items (#773): the ledger chain — a ``proposed`` row carrying
``demand_id`` followed by a same-``cycle_id`` terminal ``outcome: success``
row — is the authoritative done-truth for demand items (the model refines
proposal titles in demand mode, so text-based git-log evidence structurally
cannot see these integrations). ``collect_demand`` folds new pairs from the
current ledger plus the newest rotated archives (#790) into
``<state_dir>/demand/completed.json`` (append-only, rotation-proof by
construction) and drops completed ids from ALL demand
kinds before the exhausted filter — a completed item needs no exhaustion
bookkeeping at all. ``goal_text_utils.filter_completed_priorities_from_
goal_text`` consumes the same sidecar (via :func:`completed_demand_ids`)
when given a ``state_dir``.

Kill switch: ``SELFEVO_DEMAND_DRIVEN_ENABLED`` — #750 pattern, default ON
(absent, ``"1"``, or garbage all mean enabled); the literal ``"0"`` disables
demand-driven mode wholesale, restoring the pre-#760 proposer behavior (the
old code paths in ``llm_proposer`` stay intact behind this switch).

Everything here is deterministic (NO LLM call) and fail-open: a
missing/corrupt file, an unreadable directory, or any unexpected exception
degrades to "no demand from this source" — never raises into the caller.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime.state_access import Window, evidence_status, ledger_window

logger = logging.getLogger(__name__)

ENABLED_ENV = "SELFEVO_DEMAND_DRIVEN_ENABLED"

_DEFECT_WINDOW_HOURS = 48
_MAX_RESULT_FILES = 50  # bounded read, same discipline as existence_index._MAX_LEDGER_RESULTS
_MAX_RESULT_FILE_DEFECTS = 10  # #1038: capped defect output from result files
_MAX_RESULT_NOOP_FILES = 50  # #1114: bounded no-op exhaustion scan
_RESULT_DIRS = ("results", "archive")  # handled results move to archive
_MAX_LEDGER_DEFECTS = 10
_MAX_COMPILE_DEFECTS = 10
_MAX_HELDOUT_DEFECTS = 5  # #780: bounded held-out failure demand
_MAX_VALIDATOR_DEFECTS = 5  # #925: bounded validator-harness failure/findings demand
_MAX_SKILL_EVAL_DEFECTS = 3  # #941: bounded skill-eval negative-delta demand
_MAX_KNOWLEDGE_LIFT_DEFECTS = 1  # #1093: bounded knowledge-lift negative-delta demand
_MAX_CURATOR_UNSUPPORTED_ITEMS = 1  # #1094: bounded unsupported curator staging demand
# (#928 round 4) There was a _MAX_VALIDATOR_RUN_LINES = 500 here, used to
# slice the sidecar's tail before filtering. It is gone rather than unused:
# a few hundred forged rows with an unparseable path evicted every genuine
# row from that window, which made it a silencing channel far cheaper than
# the file-size guard it sat behind. The guard is the only bound now.
_MAX_VALIDATOR_SIDECAR_BYTES = 2_000_000  # #925 review: bounded read of a harness-written file
_MAX_SUMMARY_CHARS = 160
# #808: was 240; goal-gap items with a scorecard ``lever_hint`` append it
# after the evidence sentence, and 240 truncated the hint mid-word before
# reaching the "does NOT move it" instruction — the whole point of the hint.
_MAX_EVIDENCE_CHARS = 420

_MAX_DEFECT_ITEMS = 10
_MAX_PRIORITY_ITEMS = 10
_MAX_HYPOTHESIS_ITEMS = 1  # at most one active hypothesis experiment (#878)
_MAX_SKILL_CANDIDATE_ITEMS = 3  # top-N pre-computed skill candidates (#1006)
_MAX_REFLECTION_ITEMS = 5  # #1038: bounded reflection demand
_REFLECTION_MAX_AGE_DAYS = 7  # #1038: freshness window for reflector outputs

_DECAY_DAYS = 14
_MAX_DECAY_ITEMS = 5

_MAX_REPAIR_UNUSED_ITEMS = 3  # #845/#958: shared cap for repair-unused AND retirement demand
_REPAIR_UNUSED_MIN_DAYS = 3  # idle >= this but < _DECAY_DAYS => re-wire band
_SKILL_NEVER_READ_GRACE_DAYS = 3
# #958: retirement path — a never-read skill that has already been the subject
# of this many integrated repair-unused cycles (without gaining a confirmed read)
# graduates to a retirement demand item instead of another repair item.
_SKILL_RETIRE_AFTER_REPAIR_CYCLES = 2
# #958: anti-flap cooldown in days — a path retired within this window triggers
# a re-creation warning in the proposer context instead of a new retirement item.
_SKILL_RETIRE_COOLDOWN_DAYS = 30
_SKILL_RETIREMENT_COOLDOWN_SCHEMA = "skill-retirement-cooldown-v1"
# (younger than the decay/archival band; disjoint by construction — the
# invariant _REPAIR_UNUSED_MIN_DAYS < _DECAY_DAYS holds for these literals)

_MAX_GOAL_GAP_ITEMS = 5
_MAX_ARTIFACT_GAP_ITEMS = 1  # #1035: bound artifact-gap demand to 1 item
# #778: a completed goal-gap id suppresses the item only this long — a metric
# can legitimately regress, so "done" is time-boxed for this kind ONLY.
_GOAL_GAP_COMPLETED_TTL_DAYS = 7
# #925 review: a validator-harness defect summary is CONSTANT per script, so
# permanent completed-suppression would silence a validator that breaks again
# months later (and lets one deliberately self-silence after a single closed
# item). Same reasoning as the goal-gap TTL above — the condition recurs.
_VALIDATOR_COMPLETED_TTL_DAYS = 7
_VALIDATOR_SUMMARY_PREFIX = "validator scripts/"
# #928: state/validator_harness/ is the ONE writable carve-out in the
# harness's sandbox and it is shared by every validator subprocess — a
# validator can therefore append a FORGED row to last_runs.jsonl naming a
# different script's path. Re-validate ``row["path"]`` before trusting it:
# it is interpolated RAW into the item summary and passed RAW as
# ``affected_path``, and ``llm_proposer._demand_section`` renders both
# verbatim into the prompt. The character class is therefore an explicit
# allowlist rather than ``[^/]+``: the latter excludes only the traversal
# character while still admitting newlines, C0/C1 controls and bidi
# overrides — exactly the material needed to fake prompt structure — and
# it is length-bounded so a forged path cannot pad the prompt either.
#
# Deliberately duplicated rather than imported from ``validator_harness``:
# this module must not grow a dependency on the harness module. It is the
# STRICTER of the two on purpose — the harness matches its pattern against
# a real directory listing, whereas the string here is attacker-chosen.
_VALIDATOR_PATH_RE = re.compile(
    r"^scripts/(check|validate|audit|analyze|verify)_[A-Za-z0-9._-]{1,120}\.py$"
)

_EXHAUSTION_REJECTS = 2
_EXHAUSTION_EXPIRY_HOURS = 24  # was 7 days; shortened by #771 (deadlock escape)
_NOOP_OUTCOMES = {"completed_no_commit", "skipped-duplicate"}
_ESCALATION_MODEL_ENV = "SELFEVO_ESCALATION_MODEL"

_SCRIPT_DIRS = ("scripts", "surfaces")  # mirrors system_map._SCRIPT_DIRS

_EXHAUSTED_SCHEMA = "demand-exhausted-v1"
_COMPLETED_SCHEMA = "demand-completed-v1"
_COMPILE_WATERMARK_SCHEMA = "demand-py-compile-watermark-v1"

# Same regex family as llm_proposer._PRIORITY_PATTERN /
# goal_text_utils.filter_completed_priorities_from_goal_text — one entry per
# "(A) Priority N — Title: instructions" line.
_PRIORITY_PATTERN = re.compile(
    r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)",
    re.DOTALL,
)

# #815: the explicit, harness-parsed goal-vector tag convention — an inline
# "(V1)" or "(V2)" token at the end of a priority's TITLE, right before the
# colon, e.g. "Priority 11 — Loop health in dashboard (V2): ..." — NOT
# between the priority number and the em-dash, which would break
# ``_PRIORITY_PATTERN`` and ``goal_text_utils._PRIORITY_LABEL_PATTERN``
# above. Read from the title ONLY, never the instructions/body: a stray
# "(V1)"/"(V2)" mention inside free-text instructions must not
# misclassify the item. Only this explicit token is ever read — vector is
# NEVER inferred from free-text semantics. Untagged text yields "" (unknown).
_VECTOR_TAG_RE = re.compile(r"\((V1|V2)\)")


def _vector_rank(vector: str) -> int:
    """Sort key for the soft V1-before-V2 demand bias (#815): explicit V1
    first, V2 second, untagged/unknown last. Used as a stable-sort key —
    ties keep their original relative order, and no item is ever dropped
    (this reorders, it never starves V2)."""
    return {"V1": 0, "V2": 1}.get(vector, 2)

# Loose "looks like a repo file path" matcher for hypothesis acceptance text:
# something with a slash or a dot-extension, e.g. scripts/foo.py, docs/x.md.
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-./]*/[A-Za-z0-9_\-./]+|[A-Za-z0-9_\-]+\.[A-Za-z0-9]{1,5}")

# ─── doc-only / code-bearing classification ───────────────────────────────────

_DOC_ONLY_PREFIXES = ("docs/", "lessons/", "memory/")
_DOC_ONLY_BASENAMES = {"AGENTS.md"}
_DEFAULT_DOC_ONLY_24H_BUDGET = 5
_DOC_ONLY_BUDGET_ENV_VAR = "EEEBOT_DOC_ONLY_24H_BUDGET"
_LOG = logging.getLogger(__name__)


def is_doc_only_path(path: str | Path) -> bool:
    """Return True if a single path is classified as doc-only.

    Doc-only paths:
      - Any path starting with (or inside) ``docs/``, ``lessons/``, or ``memory/``
      - Any file with basename in :data:`_DOC_ONLY_BASENAMES` (i.e. ``AGENTS.md``)

    Fails open (returns False) on empty/invalid inputs.
    """
    if not path:
        return False
    normalized = str(path).replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return False
    if any(normalized.startswith(prefix) for prefix in _DOC_ONLY_PREFIXES):
        return True
    parts = normalized.split("/")
    basename = parts[-1]
    if basename in _DOC_ONLY_BASENAMES:
        return True
    return False


def is_non_confirmable_target(target: str | Path | None) -> bool:
    """Return True when a reflection target cannot produce usage evidence.

    A missing target is non-confirmable, as are targets outside the script
    surfaces that the harness/usage layer can observe. This is steering-only:
    it never blocks a demand or changes the gate.
    """
    if not target:
        return True
    normalized = str(target).replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return True
    if is_doc_only_path(normalized):
        return True
    return not bool(re.fullmatch(r"(?:scripts|surfaces)/[^/]+\.py", normalized))


def _is_test_path(path: str) -> bool:
    return str(path).replace("\\", "/").strip().lstrip("/").startswith("tests/")


def classify_change_tier(files_changed: list[str] | None) -> str:
    """Classify an integration's changed files as ``'doc-only'`` or ``'code-bearing'``.

    Classification:
      - ``'doc-only'`` iff every changed non-test path is classified as doc-only
        (and at least one such path exists)
      - ``'code-bearing'`` otherwise (mixed changes, code-only changes,
        test-only changes, and empty/None lists)

    Test files are tier-neutral (#1175, measured on #1188): 8 of the 20
    ``AGENTS.md``-only integrations of 2026-08-27..09-01 co-changed a file
    under ``tests/`` and were recorded ``code-bearing``, which lifted them out
    of the doc-only budget entirely. A test that only asserts on a document
    does not make the change code-bearing; a change that is only tests still
    does.
    """
    if not files_changed:
        return "code-bearing"
    cleaned = [f for f in files_changed if str(f).strip()]
    if not cleaned:
        return "code-bearing"
    substantive = [p for p in cleaned if not _is_test_path(p)]
    if substantive and all(is_doc_only_path(p) for p in substantive):
        return "doc-only"
    return "code-bearing"


def predict_item_change_tier(item: dict[str, Any] | None) -> str:
    """Predict an item's output tier using the integration classifier.

    Demand items carry an explicit affected path when their producer knows one;
    priority and goal-gap items may only mention paths in their bounded text.
    Missing or ambiguous paths fail open as code-bearing, preserving demand.
    """
    if not isinstance(item, dict):
        return "code-bearing"
    paths: list[str] = []
    affected = str(item.get("affected_path") or "").strip()
    if affected:
        paths.append(affected)
    else:
        for field in ("summary", "evidence"):
            for match in _PATH_TOKEN_RE.findall(str(item.get(field) or ""))[:20]:
                path = match.strip(".,;:()[]{}")
                if path and path not in paths:
                    paths.append(path)
    return classify_change_tier(paths)


def doc_only_budget_24h() -> int:
    """Return the configured 24h budget for doc-only integrations (default 5)."""
    raw = os.environ.get(_DOC_ONLY_BUDGET_ENV_VAR)
    if raw is None:
        return _DEFAULT_DOC_ONLY_24H_BUDGET
    try:
        val = int(raw.strip())
        return max(0, val)
    except Exception:
        return _DEFAULT_DOC_ONLY_24H_BUDGET


# #1238: the ``doc_only_budget`` ledger row was 27% of the live ledger because
# ``collect_demand`` runs two-to-three times per bridge invocation (gate probe
# + context build) and appended the row on every pass, restating an unchanged
# state. What carries information is a *change* of state plus one heartbeat
# per cycle, so the row is written when the state differs from the last row
# written in this process, always when something was deferred, and once at
# process start. The bridge is a ``Type=oneshot`` unit — one process per
# invocation — so "this process" is "this cycle", the same reasoning as
# ``llm_proposer._record_idle``. Process memory is the only thing cheaper than
# the row it suppresses: no state file, no scan, no read on the write path.
# Keyed by state_dir so independent state roots (test fixtures) never share
# a memo. Value: ``[state_tuple, passes_folded_since_last_written_row]``.
_doc_budget_last_row: dict[str, list] = {}


def _doc_budget_row_due(state_dir: Path, state: tuple[bool, bool, bool]) -> int:
    """Return the number of passes a row written now would stand for, or 0
    when the pass restates the last row and should be folded into the next.

    ``state`` is ``(doc_budget_exceeded, ledger_blind, doc_only_deferred > 0)``.
    A pass that deferred anything is always due — a deferral is never a no-op.
    """
    key = str(state_dir)
    memo = _doc_budget_last_row.get(key)
    deferred = state[2]
    if memo is not None and not deferred and memo[0] == state:
        memo[1] += 1
        return 0
    passes = 1 + (memo[1] if memo is not None else 0)
    _doc_budget_last_row[key] = [state, 0]
    return passes


def count_doc_only_integrations_24h(state_dir: Path, now: datetime | None = None) -> int:
    """Count doc-only ``outcome: success`` rows in the last 24 h across the live
    ledger and its rotated day archives (``state_access.ledger_window``, #1175).

    The recorded ``change_tier`` wins; rows without one are classified from
    ``files_changed``. Returns the visible count on any window status —
    ``collect_demand`` decides what a blind window means for the budget,
    ``scorecard`` reports the number as is.
    """
    now = now or datetime.now(timezone.utc)
    window = ledger_window(Path(state_dir), since_ts=_iso(now - timedelta(hours=24)), phases=frozenset({"outcome"}))
    count = 0
    for event in window.rows:
        if event.get("outcome") != "success":
            continue
        event_ts = _parse_ts(event.get("ts"))
        if event_ts is None or event_ts < now - timedelta(hours=24):
            continue
        tier = event.get("change_tier")
        if tier is None:
            tier = classify_change_tier(event.get("files_changed", []))
        if tier == "doc-only":
            count += 1
    return count


def demand_driven_enabled() -> bool:
    """#750-pattern kill switch: default ON; only the literal ``"0"`` disables."""
    return os.environ.get(ENABLED_ENV, "1").strip() != "0"


def item_id(kind: str, summary: str) -> str:
    """Stable demand-item id: kind-prefixed short hash of kind+summary."""
    digest = hashlib.sha256(f"{kind}\x00{summary}".encode("utf-8", errors="replace")).hexdigest()
    return f"{kind}-{digest[:12]}"


def _make_item(
    kind: str,
    summary: str,
    evidence: str,
    affected_path: str = "",
    vector: str = "",
    direction: str = "",
) -> dict[str, str]:
    summary = (summary or "").strip()[:_MAX_SUMMARY_CHARS]
    return {
        "kind": kind,
        "id": item_id(kind, summary),
        "summary": summary,
        "evidence": (evidence or "").strip()[:_MAX_EVIDENCE_CHARS],
        "affected_path": (affected_path or "").strip()[:200],
        # #815: which goal vector this item serves — "V1"/"V2" when the
        # source is vector-classifiable (an explicit inline (V1)/(V2) tag
        # on a priority, or the scorecard-derived vector on a goal-gap),
        # "" (unknown) otherwise. Additive-only: existing callers that omit
        # this arg get "" and are unaffected.
        "vector": (vector or "").strip(),
        # #879: which tech-tree improvement DOMAIN this item corresponds
        # to (e.g. "proposer-quality"), "" (unknown/unmapped) otherwise —
        # a SEPARATE axis from ``vector`` above. Currently only
        # ``_goal_gap_items`` populates this (an exact metric<->lever
        # correspondence; see its own docstring). Additive-only: existing
        # callers that omit this arg get "" and are unaffected.
        "direction": (direction or "").strip(),
    }


# #1175: every ledger read in this module goes through state_access.ledger_window
# (live file + dated archives, byte-capped there). Horizon of the shared
# per-cycle read that collect_demand threads through the lane helpers.
_LEDGER_ROWS_HORIZON_DAYS = 3


class LedgerRows(list):
    """Rows of one ``state_access.ledger_window`` read with the window's
    evidence status attached (see ``state_access.evidence_status``), so the
    helpers that take ``ledger_rows`` can apply the #1173 Class-A rule without
    a second read. A plain list (test doubles, older callers) reads as
    ``complete``."""

    status: str = "complete"
    notes: tuple[str, ...] = ()
    files_read: int = 0
    covered_from: str | None = None
    covered_to: str | None = None


def _ledger_rows_from(window: Window) -> LedgerRows:
    rows = LedgerRows(window.rows)
    rows.status = evidence_status(window)
    rows.notes = tuple(window.notes)
    rows.files_read = window.files_read
    rows.covered_from = window.covered_from
    rows.covered_to = window.covered_to
    return rows


def window_status(rows: Any) -> str:
    """Evidence status carried by :class:`LedgerRows`; ``complete`` for a plain list."""
    return str(getattr(rows, "status", "complete") or "complete")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_ledger_rows(state_dir: Path, *, horizon_days: int = _LEDGER_ROWS_HORIZON_DAYS) -> LedgerRows:
    """Rows of the last ``horizon_days`` from the live ledger and its rotated
    ``cycles-YYYY-MM-DD.jsonl.gz`` archives via ``state_access.ledger_window``,
    oldest first, with the window's evidence status on the result.

    A non-complete window is logged once per read so a stuck counter can be
    diagnosed from the journal; callers stay fail-open and receive whatever
    rows were readable.
    """
    since = datetime.now(timezone.utc) - timedelta(days=horizon_days)
    window = ledger_window(Path(state_dir), since_ts=_iso(since))
    rows = _ledger_rows_from(window)
    if rows.status != "complete":
        _LOG.warning(
            "ledger window %s (%s): files_read=%d files_skipped=%d bytes_read=%d",
            rows.status, ",".join(window.notes) or "-", window.files_read, window.files_skipped, window.bytes_read,
        )
    return rows


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


# ─── kind: priority ─────────────────────────────────────────────────────────


def _priority_items(state_dir: Path, selfevo_repo: Path | None) -> list[dict[str, str]]:
    """Remaining goal_text priorities, done-filtering delegated to
    ``goal_text_utils.filter_completed_priorities_from_goal_text`` (#748) —
    this preserves R30: a freshly-seeded operator priority is always demand."""
    try:
        from nanobot.runtime.goal_text_utils import filter_completed_priorities_from_goal_text

        raw_text = ""
        # 1. Charter-first from selfevo_repo/GOALS.md
        if selfevo_repo:
            try:
                from nanobot.runtime.goal_review import read_charter_text

                raw_text = read_charter_text(selfevo_repo) or ""
            except Exception:
                raw_text = ""

        # 2. Workspace goal_text.json
        if not raw_text:
            path = Path(state_dir) / "goals" / "goal_text.json"
            data = _read_json(path, None)
            if isinstance(data, dict):
                raw_text = str(data.get("text") or "")

        # 3. Fallback to goals.md
        if not raw_text:
            legacy = Path(state_dir) / "goals.md"
            if legacy.is_file():
                try:
                    raw_text = legacy.read_text(encoding="utf-8")
                except Exception:
                    raw_text = ""

        if not raw_text:
            return []
        # #860: fold in goal_review's harness-owned derived priorities
        # (survive deploy_release.sh's goal_text.json reseed) — local import,
        # goal_review must never be imported at module level here (see its
        # own docstring: it lazily imports llm_proposer, which imports this
        # module, so a module-level import here would risk a cycle).
        try:
            from nanobot.runtime import goal_review

            raw_text = goal_review.merged_goal_text(state_dir, raw_text)
        except Exception:
            pass
        filtered = filter_completed_priorities_from_goal_text(
            raw_text, selfevo_repo, state_dir=state_dir
        )
        marker = "Current priority targets:"
        idx = filtered.find(marker)
        if idx == -1:
            return []
        section = filtered[idx + len(marker):]
        items: list[dict[str, str]] = []
        for m in _PRIORITY_PATTERN.finditer(section):
            num, title, instructions = m.group(1), m.group(2).strip(), m.group(3).strip()
            # #815: explicit (V1)/(V2) tag, read from the TITLE group ONLY —
            # the convention places it at the end of the title, right before
            # the colon. Searching the instructions/body too would let a
            # stray "(V1)"/"(V2)" mention in free-text instructions
            # misclassify the item; never inferred from wording either way.
            tag = _VECTOR_TAG_RE.search(title)
            vector = tag.group(1) if tag else ""
            items.append(
                _make_item(
                    "priority",
                    f"Priority {num} — {title}",
                    instructions,
                    vector=vector,
                )
            )
        # #815: soft within-kind bias — V1 priorities before V2 before
        # untagged, via a stable sort (ties and V2-only sets are unaffected;
        # nothing is ever dropped).
        items.sort(key=lambda it: _vector_rank(it.get("vector", "")))
        return items
    except Exception:
        return []


# ─── kind: defect ───────────────────────────────────────────────────────────


def _ledger_defects(
    state_dir: Path,
    now: datetime,
    *,
    limit: int | None = _MAX_LEDGER_DEFECTS,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Terminal ledger outcome rows with a real failure in the last 48h.
    ``skipped-*`` outcomes are the dedup stack working, not defects."""
    items: list[dict[str, str]] = []
    seen_summaries: set[str] = set()
    try:
        cutoff = now - timedelta(hours=_DEFECT_WINDOW_HOURS)
        matching_rows: list[dict[str, Any]] = []
        rows = ledger_rows if ledger_rows is not None else _load_ledger_rows(state_dir)
        for row in rows:
            if not isinstance(row, dict) or row.get("phase") != "outcome":
                continue
            outcome = str(row.get("outcome") or "").strip().lower()
            if outcome.startswith("skipped"):
                continue
            if outcome not in ("failed", "timeout", "error", "harness_failed"):
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts < cutoff:
                continue
            matching_rows.append(row)

        for row in reversed(matching_rows):
            outcome = str(row.get("outcome") or "").strip().lower()
            reason = str(row.get("reason") or "").strip()
            branch = str(row.get("branch") or row.get("cycle_id") or "").strip()
            summary = f"recent cycle outcome {outcome}"
            if summary in seen_summaries:
                continue
            seen_summaries.add(summary)
            items.append(
                _make_item(
                    "defect",
                    summary,
                    f"ledger outcome row cycle_id={row.get('cycle_id') or '?'} branch={branch or '?'} reason={reason or '(none)'}",
                )
            )
        return items if limit is None else items[:limit]
    except Exception:
        return items


def _skipped_cycle_ids(
    state_dir: Path,
    now: datetime,
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Cycle ids whose terminal ledger outcome is ``skipped-*`` in the defect
    window. Their result files are dedup bookkeeping, not defects (#760
    roll-out fix: a blocked-by-dedup result masqueraded as demand and kept
    the loop calling the LLM). Fail-open: errors yield an empty set — worst
    case a bookkeeping result is presented as demand again, never a crash."""
    skipped: set[str] = set()
    try:
        cutoff = now - timedelta(hours=_DEFECT_WINDOW_HOURS)
        rows = ledger_rows if ledger_rows is not None else _load_ledger_rows(state_dir)
        for row in rows:
            if not isinstance(row, dict) or row.get("phase") != "outcome":
                continue
            if not str(row.get("outcome") or "").strip().lower().startswith("skipped"):
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is not None and ts < cutoff:
                continue
            cycle_id = str(row.get("cycle_id") or "").strip()
            if cycle_id:
                skipped.add(cycle_id)
        return skipped
    except Exception:
        return skipped


def _result_file_defects(
    state_dir: Path,
    now: datetime,
    *,
    limit: int | None = _MAX_RESULT_FILE_DEFECTS,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Failed/blocked subagent result files with error text — bounded to the
    :data:`_MAX_RESULT_FILES` most recently modified files.

    Two exclusions keep dedup bookkeeping out of demand (#760 roll-out fix):
    results whose cycle terminally ``skipped-*`` in the ledger, and
    ``blocked`` results carrying no error text at all (the bridge writes
    such placeholder results for every pre-spawn skip)."""
    items: list[dict[str, str]] = []
    try:
        results_dir = Path(state_dir) / "subagents" / "results"
        if not results_dir.is_dir():
            return []
        cutoff_ts = (now - timedelta(hours=_DEFECT_WINDOW_HOURS)).timestamp()
        skipped_cycles = _skipped_cycle_ids(state_dir, now, ledger_rows=ledger_rows)
        entries = [p for p in results_dir.glob("*.json") if p.is_file()]
        try:
            entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        for entry in entries[:_MAX_RESULT_FILES]:
            try:
                if entry.stat().st_mtime < cutoff_ts:
                    continue
                data = json.loads(entry.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            status = str(data.get("status") or "").strip().lower()
            if status not in ("failed", "blocked", "error"):
                continue
            if str(data.get("cycle_id") or "").strip() in skipped_cycles:
                continue  # dedup bookkeeping, not a defect (#760 roll-out fix)
            blocker = data.get("blocker")
            blocker_reason = blocker.get("reason", "") if isinstance(blocker, dict) else ""
            error_text = str(
                data.get("error") or data.get("error_text") or blocker_reason or ""
            ).strip()
            if status == "blocked" and not error_text:
                continue  # placeholder blocked result with no error signal
            title = str(data.get("backlog_title") or data.get("task_title") or entry.stem).strip()
            summary = f"subagent result {status}: {title}"
            items.append(
                _make_item(
                    "defect",
                    summary,
                    error_text or f"result file {entry.name} status={status}",
                )
            )
        return items if limit is None else items[:limit]
    except Exception:
        return items


def _compile_watermark_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "py_compile_watermark.json"


def _compile_defects(
    state_dir: Path,
    selfevo_repo: Path | None,
    head: str | None,
    *,
    limit: int | None = _MAX_COMPILE_DEFECTS,
) -> list[dict[str, str]]:
    """Instance-repo scripts that fail to byte-compile (syntax errors).

    Watermark-gated on the repo's git HEAD exactly like
    ``system_map.update_system_map``: when HEAD matches the sidecar's stored
    head, the cached findings are reused and NO file is even opened. Uses the
    builtin ``compile()`` (the same syntax check ``py_compile`` performs)
    rather than ``py_compile.compile`` so nothing is ever written to the
    instance repo (no ``__pycache__`` side effects).
    """
    if not selfevo_repo:
        return []
    try:
        repo = Path(selfevo_repo)
        if not repo.is_dir() or head is None:
            return []
        wm_path = _compile_watermark_path(state_dir)
        watermark = _read_json(wm_path, None)
        if (
            isinstance(watermark, dict)
            and watermark.get("git_head") == head
            and isinstance(watermark.get("failures"), list)
        ):
            failures = watermark["failures"]
        else:
            failures = []
            for dirname in _SCRIPT_DIRS:
                d = repo / dirname
                if not d.is_dir():
                    continue
                try:
                    py_files = sorted(d.glob("*.py"))
                except Exception:
                    continue
                for py_path in py_files:
                    try:
                        source = py_path.read_text(encoding="utf-8", errors="replace")
                        compile(source, str(py_path), "exec")
                    except SyntaxError as exc:
                        try:
                            rel = str(py_path.relative_to(repo)).replace("\\", "/")
                        except Exception:
                            rel = py_path.name
                        failures.append({"path": rel, "error": f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"})
                    except Exception:
                        continue
            _write_json(
                wm_path,
                {
                    "schema_version": _COMPILE_WATERMARK_SCHEMA,
                    "git_head": head,
                    "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
                    "failures": failures,
                },
            )
        items: list[dict[str, str]] = []
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            rel = str(failure.get("path") or "").strip()
            err = str(failure.get("error") or "").strip()
            if not rel:
                continue
            items.append(
                _make_item(
                    "defect",
                    f"script fails to compile: {rel}",
                    err or "py_compile failure",
                    affected_path=rel,
                )
            )
        return items if limit is None else items[:limit]
    except Exception:
        return []


def _heldout_defect_items(
    state_dir: Path, *, limit: int | None = _MAX_HELDOUT_DEFECTS
) -> list[dict[str, str]]:
    """Held-out behavioral-check failures as ``defect`` demand (#780).

    Read-only over ``<state_dir>/heldout/results.json`` — the sidecar
    ``nanobot.runtime.heldout.run_heldout`` maintains on the scorecard
    recompute path (this function never runs any check itself). One item
    per failing artifact — the results file is keyed by artifact path, so
    dedup is structural — with the checker's evidence string as the item
    evidence: the loop learns WHAT is broken, never how the check works
    (the pack is invisible to the instance, #603 invariant). ``skip``
    results never become demand (a checker timeout/bug is not an instance
    defect). Bounded to :data:`_MAX_HELDOUT_DEFECTS`; fail-open: any error
    yields no held-out demand."""
    items: list[dict[str, str]] = []
    try:
        data = _read_json(Path(state_dir) / "heldout" / "results.json", None)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict):
            return []
        for artifact in sorted(results):
            entry = results[artifact]
            if not isinstance(entry, dict) or entry.get("status") != "fail":
                continue
            evidence = str(entry.get("evidence") or "").strip()
            items.append(
                _make_item(
                    "defect",
                    f"held-out check failed: {artifact}",
                    evidence or "behavioral check failed on fixtures",
                    affected_path=artifact,
                )
            )
        return items if limit is None else items[:limit]
    except Exception:
        return items


_WHITESPACE_RUN_RE = re.compile(r"\s+")
# C0 + DEL, C1 (U+009B is an 8-bit CSI, as capable as ESC[), zero-width and
# bidi-override formatting characters, and the BOM. Newline/tab/CR are
# deliberately absent: the whitespace collapse below turns them into a
# single space rather than deleting them, so words cannot silently run
# together into a different word. Every OTHER character Python's ``\s``
# treats as whitespace is carved out of these ranges for exactly that reason,
# not just the obvious ones: U+0085 (NEL), plus \x0b, \x0c and \x1c-\x1f,
# which are all ``\s`` and all merged the words around them while they were
# being deleted here. Leaving them to the collapse yields a space instead.
_CONTROL_CHAR_RE = re.compile(
    r"[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def _sanitize_stderr_tail(text: str) -> str:
    """#928: ``stderr_tail`` is entirely script-controlled (a validator
    subprocess writes whatever it wants to stderr) and flows verbatim into
    demand ``evidence``, which the proposer places directly in an LLM
    prompt. Drop control characters, then collapse every whitespace run
    (newlines and tabs included) to a single space, so a validator cannot
    inject fake prompt structure with line breaks, or steer a terminal with
    escape sequences. ``\\s`` covers the Unicode line separators too
    (U+2028, U+2029, U+0085, U+00A0, U+3000), not just ASCII. Character
    scrubbing only — no broader prompt-injection defence is attempted, and
    none of this makes the text trustworthy: it only stops it from
    impersonating structure."""
    text = _CONTROL_CHAR_RE.sub("", text)
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()


def _validator_defect_items(
    state_dir: Path, *, limit: int | None = _MAX_VALIDATOR_DEFECTS
) -> list[dict[str, str]]:
    """Validator-harness run results as ``defect`` demand (#925).

    Read-only over ``<state_dir>/validator_harness/last_runs.jsonl`` — the
    sidecar ``nanobot.runtime.validator_harness.run_validator_harness``
    maintains (this function never runs any validator itself, mirroring
    ``_heldout_defect_items``'s read-only relationship to its own sidecar).
    Bounded by the :data:`_MAX_VALIDATOR_SIDECAR_BYTES` file-size guard —
    NOT by a line count; see the comment at the read itself for why a tail
    slice was a silencing channel. The LAST row per script path wins (append
    order = chronological, so a script's most recent verdict is what's
    presented — a since-fixed failure must not linger as demand forever).

    One item per script, priority order when more than one condition holds:
    a non-zero exit ("validator X fails when run" — or, when the harness
    marked the run ``harness_contract: "requires_arguments"`` (#934 Class
    B), "validator X cannot run under the harness: it requires
    command-line arguments" — a truthful relabeling, NOT a suppression:
    the harness invokes every script with no arguments, so a validator
    whose argparse requires a flag would otherwise be misreported as
    crashing on every run forever), then a run the harness killed at its
    per-script timeout (``harness_contract: "exceeds_time_budget"``, #934 —
    such a run has no exit code at all, so before #934 it produced no
    demand and no signal of any kind), then a positive findings count
    ("validator X reports N findings"). A clean run (exit 0, no findings)
    yields nothing — only a validator that surfaced a real problem becomes
    demand. Bounded to :data:`_MAX_VALIDATOR_DEFECTS`; fail-open: any error
    or a missing sidecar yields no validator demand."""
    items: list[dict[str, str]] = []
    try:
        path = Path(state_dir) / "validator_harness" / "last_runs.jsonl"
        if not path.is_file():
            return items
        # #925 review: this sidecar is written by the validator harness, whose
        # own state subdirectory a validator subprocess can reach — bound the
        # READ (not just the tail slice) so a huge file cannot OOM the 2GB
        # host mid-collect (same precedent as usage_evidence's file guard).
        if path.stat().st_size > _MAX_VALIDATOR_SIDECAR_BYTES:
            return items
        # NOT sliced to a fixed number of trailing lines before filtering
        # (#928 round-3 review): every validator subprocess can append here, so
        # a few hundred forged rows with an unparseable path — tens of KB, far
        # cheaper than pushing the file past the size guard above — would evict
        # every genuine row from the window. The size guard is what bounds this
        # read; the line count does not need its own.
        lines = path.read_text(encoding="utf-8").splitlines()
        latest: dict[str, dict[str, Any]] = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            rel = str(row.get("path") or "").strip()
            if not rel or not _VALIDATOR_PATH_RE.match(rel):
                # #928: last_runs.jsonl is appended to by validator
                # subprocesses sharing the harness's one writable carve-out —
                # a forged row naming a path outside the validator allowlist
                # (or a traversal attempt) must not become demand.
                continue
            latest[rel] = row  # later lines overwrite -> most recent run wins
        # #933: sort by served-map membership rather than alphabetical path
        # string to prevent attacker-controlled sidecar forging from choosing
        # which items reach the cap.  Paths the harness's rotation.json
        # records as actually having been run (``served`` map) sort BEFORE
        # paths unknown to that map.  Because the harness rewrites rotation.json
        # atomically at the end of every run — using only scripts it actually
        # executed — a validator subprocess cannot forge a durable served entry
        # for a script that never ran; such entries are erased on every harness
        # completion.  Unknown paths (forged names or brand-new scripts whose
        # very first run is in progress) sort last, after every known path.
        # Fail-open: if rotation.json is missing or malformed we fall back to
        # the previous alphabetical order rather than raising.
        _rotation_path = (
            Path(state_dir) / "validator_harness" / "rotation.json"
        )
        try:
            if _rotation_path.is_file():
                _rot_data = json.loads(
                    _rotation_path.read_text(encoding="utf-8")
                )
                _served: dict[str, Any] = (
                    _rot_data.get("served", {})
                    if isinstance(_rot_data, dict)
                    else {}
                )
            else:
                _served = {}
        except Exception:
            _served = {}
        _ordered = sorted(
            latest,
            key=lambda r: (0 if r in _served else 1, r),
        )
        for rel in _ordered:
            row = latest[rel]
            if row.get("harness_env_error"):
                # #928: the harness classified this run as blocked by its OWN
                # sandbox rather than broken (e.g. a PermissionError on a path
                # the unit makes inaccessible). Two of the three false defects
                # from the harness's first production run were exactly this.
                # The script is not at fault and the loop cannot fix a denial
                # imposed from outside it, so this yields no defect.
                #
                # Also suppresses a positive findings_count, via the elif
                # below: the run was denied, so its findings are not a verdict
                # about the script either.
                #
                # Checked HERE rather than while building `latest`, so a marked
                # run still counts as the newest verdict for its path. Skipping
                # it earlier left an older failing row as "latest", which kept
                # re-presenting a defect the newest run had superseded.
                #
                # The cost of this ordering is that a validator can forge a
                # marked row to bury a genuine failure — and note it need not
                # be its OWN: nothing binds a row to the process that wrote it,
                # so it could bury another script's defect too. That grants no
                # new capability, which is the actual reason this is
                # acceptable: a forged newest row with exit_code 0 already
                # suppresses any script's defect, and did so before this marker
                # existed. The allowlist above bounds WHICH paths can be named;
                # the sidecar being writable at all is what would have to
                # change to bound who can name them.
                continue
            exit_code = row.get("exit_code")
            findings = row.get("findings_count")
            if isinstance(exit_code, int) and exit_code != 0:
                stderr_tail = _sanitize_stderr_tail(str(row.get("stderr_tail") or ""))
                if row.get("harness_contract") == "decay_declared":
                    # #936: the script printed its own decay declaration at run
                    # time. Its non-zero exit is the CORRECT behaviour for a
                    # decayed script, not a defect — same "no false demand"
                    # guarantee that source-based exclusion provided, now
                    # enforced from runtime output instead of source text.
                    # Skip entirely (same as harness_env_error above).
                    continue
                elif row.get("harness_contract") == "requires_arguments":
                    # #934 Class B: the harness invokes every script with NO
                    # arguments, so a validator whose argparse requires a
                    # flag exits 2 on every run forever. "fails when run"
                    # would send the loop chasing a crash that does not
                    # exist; this names the real, fixable contract mismatch
                    # instead. Same affected_path/sanitisation/allowlist
                    # gating/cap as any other exit-code defect below — this
                    # is a relabeling, not a new suppression path.
                    summary = (
                        f"validator {rel} cannot run under the harness: it "
                        "requires command-line arguments"
                    )
                else:
                    summary = f"validator {rel} fails when run"
                items.append(
                    _make_item(
                        "defect",
                        summary,
                        f"exit code {exit_code}" + (f": {stderr_tail[:300]}" if stderr_tail else ""),
                        affected_path=rel,
                    )
                )
            elif row.get("harness_contract") == "exceeds_time_budget":
                # #934: a run the harness killed at _PER_SCRIPT_TIMEOUT has
                # exit_code None, so before this it produced NOTHING here —
                # the script silently burned a quarter of the invocation's
                # budget every rotation and never yielded a verdict or a
                # signal. This is the honest, fixable framing: the script
                # does not fit the harness's time contract. Checked as an
                # elif on the exit-code branch above because a timed-out run
                # has no exit code to report by construction.
                items.append(
                    _make_item(
                        "defect",
                        f"validator {rel} cannot finish within the harness's "
                        "per-script time budget",
                        f"validator harness run at {row.get('finished_at') or '?'} "
                        f"was killed at the per-script timeout: "
                        f"{_sanitize_stderr_tail(str(row.get('stderr_tail') or ''))[:300]}",
                        affected_path=rel,
                    )
                )
            elif isinstance(findings, int) and findings > 0:
                items.append(
                    _make_item(
                        "defect",
                        f"validator {rel} reports {findings} findings",
                        f"validator harness run at {row.get('finished_at') or '?'} "
                        f"found {findings} findings; see "
                        "state/validator_harness/last_runs.jsonl",
                        affected_path=rel,
                    )
                )
        return items if limit is None else items[:limit]
    except Exception:
        return items


def _knowledge_lift_defect_items(
    state_dir: Path, *, limit: int | None = _MAX_KNOWLEDGE_LIFT_DEFECTS
) -> list[dict[str, str]]:
    """Harness-measured knowledge lift negative delta as defect demand (#1093)."""
    items: list[dict[str, str]] = []
    try:
        from nanobot.runtime import knowledge_lift

        for raw in knowledge_lift.negative_delta_demand(Path(state_dir), limit=limit):
            items.append(
                _make_item(
                    "defect",
                    raw.get("summary", ""),
                    raw.get("evidence", ""),
                    affected_path=raw.get("affected_path", ""),
                )
            )
        return items if limit is None else items[:limit]
    except Exception:
        return items


def _curator_unsupported_items(
    state_dir: Path, *, limit: int | None = _MAX_CURATOR_UNSUPPORTED_ITEMS
) -> list[dict[str, str]]:
    """Demand note when curator has unsupported staged entries (overlap_flag=True) (#1094).

    Bounded, fail-open. Reads the staging manifest only; no content leak.
    At most one item is generated regardless of how many unsupported entries exist.
    """
    items: list[dict[str, str]] = []
    try:
        from nanobot.runtime.knowledge_curator import load_staged_manifest

        manifest = load_staged_manifest(Path(state_dir))
        unsupported = [
            e for e in manifest
            if e.get("overlap_flag") or e.get("verification_status") == "unsupported"
        ]
        if not unsupported:
            return items
        count = len(unsupported)
        summary = (
            f"curator: {count} staged fact(s) have no keyword overlap between "
            "support_claim and evidence source — review and re-submit with stronger evidence"
        )
        evidence = f"{count} unsupported curator entry/entries in staging manifest"
        items.append(_make_item("defect", summary, evidence))
        return items if limit is None else items[:limit]
    except Exception:
        return items


def _skill_eval_defect_items(
    state_dir: Path, *, limit: int | None = _MAX_SKILL_EVAL_DEFECTS
) -> list[dict[str, str]]:
    """Harness-measured skill-eval failures as ``defect`` demand (#941).

    Read-only over the ``skill_fitness/evals.jsonl`` sidecar the skill-eval
    harness (``nanobot.runtime.skill_eval_harness``) maintains — a
    ``FITNESS_SIDECARS`` member, so the rows here are the harness's OWN A/B
    verdicts, never anything the instance wrote (its ``evals.json`` is only
    the test plan). One item per skill whose LATEST run shows a negative
    with/without delta ("skill fails its own evals") or a pure token cost
    with no pass gain ("skill costs more than it buys"); a passing delta
    yields nothing. Items are re-made through :func:`_make_item` so ids
    follow the standard ``item_id`` scheme (completed-fold suppression works
    like every other lane). Bounded to :data:`_MAX_SKILL_EVAL_DEFECTS`;
    fail-open: any error yields no skill-eval demand."""
    items: list[dict[str, str]] = []
    try:
        from nanobot.runtime import skill_eval_harness

        for raw in skill_eval_harness.negative_delta_demand(state_dir, limit=limit):
            items.append(
                _make_item(
                    "defect",
                    raw.get("summary", ""),
                    raw.get("evidence", ""),
                    affected_path=raw.get("affected_path", ""),
                )
            )
        return items if limit is None else items[:limit]
    except Exception:
        return items


_MAX_TAMPER_DEFECTS = 5  # #789: bounded tamper demand
_MAX_TAMPER_SUSPECTS = 3  # #792: bounded perpetrator attribution
_TAMPER_SEARCH_MAX_BYTES = 200_000  # #792: cap per-file read for the search
_TAMPER_SIDECAR_NAME = "completed.json"  # the tampered sidecar's filename


def _tamper_suspect_scripts(
    selfevo_repo: Path | None, signal: str
) -> list[str]:
    """Perpetrator attribution for a tampered fitness sidecar (#792, #795).

    Text-searches the instance repo's ``scripts/*.py`` for the foreign
    signal literal ONLY — a script that merely mentions the sidecar
    filename (``completed.json``) is a legitimate READER, not a
    perpetrator (#795: the filename needle produced false suspects in the
    live echo 2026-07-18 02:34–03:46Z, after the actual hack was already
    eradicated). Each file is read once, capped at
    :data:`_TAMPER_SEARCH_MAX_BYTES`; matches are bounded to
    :data:`_MAX_TAMPER_SUSPECTS` (sorted path order). Fail-open: no repo,
    no signal, unreadable dir/file, or any error yields no suspects — the
    caller then keeps the generic wording, never a fabricated
    attribution."""
    suspects: list[str] = []
    try:
        if not selfevo_repo or not signal:
            return []
        scripts_dir = Path(selfevo_repo) / "scripts"
        if not scripts_dir.is_dir():
            return []
        for py_path in sorted(scripts_dir.glob("*.py")):
            try:
                if not py_path.is_file() or py_path.stat().st_size > _TAMPER_SEARCH_MAX_BYTES:
                    continue
                content = py_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if signal in content:
                suspects.append(f"scripts/{py_path.name}")
                if len(suspects) >= _MAX_TAMPER_SUSPECTS:
                    break
        return suspects
    except Exception:
        return suspects


def _tamper_defect_items(
    state_dir: Path,
    selfevo_repo: Path | None = None,
    *,
    limit: int | None = _MAX_TAMPER_DEFECTS,
) -> list[dict[str, str]]:
    """Fitness-sidecar tamper repairs as ``defect`` demand (#789).

    Read-only over ``<state_dir>/demand/completed.json`` — entries carrying
    ``tamper_repaired_at`` were repaired by ``usage_evidence.confirm_serves``
    after non-harness code wrote a foreign ``confirmed``/``signal`` into
    them (live reward-hack 2026-07-17: an instance script hardcoded the
    sidecar path and wrote ``signal: "operator-confirmed"``). The item tells
    the loop to remove the state-writing from its own script.

    Perpetrator attribution (#792): the likely writer is found by CONTENT
    search (:func:`_tamper_suspect_scripts`) — never from the entry's own
    ``files_changed``, which names the entry's integrated artifact (the
    VICTIM), not the script that wrote the foreign signal (live
    mis-targeting 2026-07-18: the loop tried to fix error_pattern_audit.py
    while the hack lived in approval_truth.py). With named suspects the
    item id is keyed on entry_id + the suspect set, so a corrected
    attribution mints a fresh demand id and reaches the loop even if the
    mis-attributed item was already exhausted. No suspects → generic
    wording and the stable per-entry id.

    Eradication retirement (#795): when the instance repo IS scannable
    (git HEAD known, real signal) and the bounded scan finds NO script
    carrying the signal literal, the hack is eradicated — the item is NOT
    emitted, and ``tamper_eradicated_at`` + ``tamper_eradicated_head``
    are persisted on the completed entry so subsequent passes skip it
    WITHOUT rescanning; the scan re-runs only when the instance HEAD
    differs from the recorded one (the hack could return in a new
    commit — then the stale marks are cleared and the item re-emits).
    Integrity-ledger rows and scorecard incident counts are untouched:
    only the demand item retires, history stays. Bounded to
    :data:`_MAX_TAMPER_DEFECTS`; fail-open: any error yields no tamper
    demand."""
    items: list[dict[str, str]] = []
    try:
        state_dir = Path(state_dir)
        data = _load_completed(state_dir)
        entries = data["entries"]
        head = _git_head(selfevo_repo)
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        suspects_by_signal: dict[str, list[str]] = {}
        changed = False
        for entry_id in sorted(entries):
            entry = entries[entry_id]
            if not isinstance(entry, dict) or not entry.get("tamper_repaired_at"):
                continue
            # #795: retired entry — skip without rescanning unless the
            # instance HEAD moved past the recorded eradication head.
            if entry.get("tamper_eradicated_at") and (
                not head or str(entry.get("tamper_eradicated_head") or "") == head
            ):
                continue
            signal = str(entry.get("tamper_signal") or "") or "(missing)"
            if signal not in suspects_by_signal:
                suspects_by_signal[signal] = _tamper_suspect_scripts(
                    selfevo_repo, "" if signal == "(missing)" else signal
                )
            suspects = suspects_by_signal[signal]
            if not suspects and head and signal != "(missing)":
                # #795: scanned clean — the signal literal is gone from
                # every instance script. Retire the item; record when and
                # at which HEAD so later passes skip it scan-free.
                entry["tamper_eradicated_at"] = now_iso
                entry["tamper_eradicated_head"] = head
                changed = True
                continue
            if suspects and entry.get("tamper_eradicated_at"):
                # HEAD moved and the hack returned: clear the stale marks.
                entry.pop("tamper_eradicated_at", None)
                entry.pop("tamper_eradicated_head", None)
                changed = True
            summary = f"fitness sidecar tampered: {entry_id}"
            if suspects:
                evidence = (
                    f"signal '{signal}' written by non-harness code into "
                    f"demand/{_TAMPER_SIDECAR_NAME} — found in "
                    f"{', '.join(suspects)}; remove its state-writing"
                )
            else:
                evidence = (
                    f"signal '{signal}' written by non-harness code into "
                    f"demand/{_TAMPER_SIDECAR_NAME}; find and remove "
                    "state-writing from the responsible instance script"
                )
            item = _make_item(
                "defect", summary, evidence,
                affected_path=suspects[0] if suspects else "",
            )
            if suspects:
                # #792: key the id on entry_id + perpetrator set so a
                # corrected attribution is a FRESH demand item, immune to
                # exhaustion earned by the mis-attributed predecessor.
                item["id"] = item_id(
                    "defect", f"{summary} [{','.join(sorted(suspects))}]"
                )
            items.append(item)
        if changed:
            data["entries"] = entries
            _write_json(_completed_path(state_dir), data)
        return items if limit is None else items[:limit]
    except Exception:
        return items


# ─── kind: goal-gap (#765) ──────────────────────────────────────────────────


def _artifact_gap_items(
    state_dir: Path, selfevo_repo: Path | None, *, limit: int | None = None
) -> list[dict[str, str]]:
    """Emit a V2 artifact-gap only when a goal explicitly names a surface."""
    if not selfevo_repo or not Path(selfevo_repo).is_dir():
        return []
    try:
        goal_text = Path(selfevo_repo, "goals.md")
        if not goal_text.is_file():
            return []
        text = goal_text.read_text(encoding="utf-8", errors="replace")
        names_surface = any(token in text.lower() for token in ("dashboard", "tui", "status interface", "owner-facing"))
        if not names_surface:
            return []
        surfaces = Path(selfevo_repo) / "surfaces"
        if surfaces.is_dir() and any(surfaces.glob("*.py")):
            return []
        item = _make_item(
            "artifact-gap", "create an owner-facing status surface (V2)",
            "goals.md names an owner-facing interface but no surfaces/*.py artifact exists; create or revive one for operator transparency",
            vector="V2",
        )
        return [item] if limit != 0 else []
    except Exception:
        return []


def _goal_gap_items(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    limit: int | None = None,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Scorecard metrics violating their goal-derived target, as demand
    (#765). The gap list comes from ``scorecard.goal_gaps`` — deterministic,
    time-watermarked, targets declared in ``scorecard._TARGETS`` from the
    ORDERED goal vectors: within this kind V1 (primary) gaps come before V2
    (secondary) gaps; the goal's FUTURE section maps to no metric and can
    never generate an item. The scorecard itself lives in the product
    runtime + ``state_dir`` (never the instance workspace, #603 invariant:
    the instance cannot redefine its own fitness). The summary is STABLE per
    metric (#778) — it MUST NOT embed the current metric value, or every
    30-min recompute mints a fresh id for the same metric (live churn
    2026-07-16: ``goal-gap-630df833`` vs ``goal-gap-3a4a6089`` for
    ``repeat_failure_rate`` at 0.4731 vs 0.4681), defeating the completed
    fold (#773) and per-id exhaustion alike. Current/target/window detail
    goes in ``evidence`` only. Fail-open: any error yields no goal-gap
    demand.

    #879: each item is tagged with the tech-tree DOMAIN whose lever_metric
    tail matches this gap's bare metric name (``tech_tree.direction_for_
    metric`` — an EXACT string correspondence, the one place this item's
    domain is unambiguous rather than a fuzzy text match). Within this
    kind, items whose ``direction`` equals the tech-tree's CURRENT
    investment direction are stable-sorted to lead WITHIN their existing
    vector class (#815's V1-before-V2 ordering is the primary key,
    unchanged; the direction boost is a secondary tiebreak only) — nothing
    is ever dropped, and a gap whose domain isn't the current direction (or
    isn't mapped to any node at all) is simply presented in its original
    relative order, never suppressed. Both lookups are wrapped fail-open:
    a tech_tree bug degrades to no tagging/no reordering, never fewer
    items."""
    try:
        from nanobot.runtime import scorecard

        snapshot = scorecard.compute_scorecard(state_dir, selfevo_repo)
        if snapshot.get("gaps_status") == "unavailable":
            logger.warning("goal gaps unavailable; no gaps known")
            return []

        try:
            from nanobot.runtime import tech_tree

            current_direction = tech_tree.current_direction(state_dir)
        except Exception:
            tech_tree = None  # type: ignore[assignment]
            current_direction = None

        items: list[dict[str, str]] = []
        gap_rows: list[dict[str, Any]] = []
        for gap in snapshot.get("gaps", []):
            metric = str(gap.get("metric") or "").strip()
            vector = str(gap.get("vector") or "").strip()
            if not metric or vector not in ("V1", "V2"):
                continue  # FUTURE (or anything else) never generates demand
            detail = f"current {gap.get('current')} vs target {gap.get('target')}"
            gap_evidence = str(gap.get("evidence") or "").strip()
            rationale = gap_evidence or detail
            lever_hint = str(gap.get("lever_hint") or "").strip()
            if lever_hint:
                # #808: proposer-facing guidance only — the summary (and
                # therefore the id) stays untouched so exhaustion/dedup
                # identity (#778) is unaffected by this addition.
                rationale = f"{rationale} | lever: {lever_hint}"
            direction = ""
            if tech_tree is not None:
                try:
                    direction = tech_tree.direction_for_metric(state_dir, metric) or ""
                except Exception:
                    direction = ""
            item = _make_item(
                    "goal-gap",
                    f"goal gap: {metric} ({vector})",
                    rationale,
                    vector=vector,
                    direction=direction,
                )
            items.append(item)
            gap_rows.append({**gap, "id": item["id"]})
        try:
            from nanobot.runtime import goal_gap_futility
            futile_ids = goal_gap_futility.futile_gap_ids(state_dir, gap_rows, ledger_rows=ledger_rows)
            if futile_ids:
                items = [item for item in items if item.get("id") not in futile_ids]
        except Exception:
            pass
        if current_direction:
            items.sort(
                key=lambda it: (
                    _vector_rank(it.get("vector", "")),
                    0 if it.get("direction") == current_direction else 1,
                )
            )
        return items if limit is None else items[:limit]
    except Exception:
        return []


# ─── kind: hypothesis ───────────────────────────────────────────────────────


def _acceptance_references_repo_file(acceptance: str, selfevo_repo: Path | None) -> bool:
    if not acceptance or not selfevo_repo:
        return False
    try:
        repo = Path(selfevo_repo)
        if not repo.is_dir():
            return False
        for token in _PATH_TOKEN_RE.findall(acceptance)[:20]:
            token = token.strip().strip(".,;:")
            if not token or "/" not in token:
                continue
            if (repo / token).exists():
                return True
        return False
    except Exception:
        return False


def _hypothesis_has_evidence(entry: dict[str, Any], selfevo_repo: Path | None) -> bool:
    """A hypothesis qualifies as demand ONLY with measurement evidence: a
    non-empty ``evidence`` or ``metric`` field, or an ``acceptance`` text that
    references an existing repo file. Free-form musing (the boilerplate
    generator's output) has none of these and never qualifies."""
    for key in ("evidence", "metric", "data_to_collect", "insight_criterion"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    acceptance = entry.get("acceptance")
    if isinstance(acceptance, str) and _acceptance_references_repo_file(acceptance, selfevo_repo):
        return True
    return False


def _skill_candidate_items(state_dir: Path, selfevo_repo: Path | None) -> list[dict[str, str]]:
    """Read deterministic recurring-action skill candidates (#1006).

    F2: reads ONLY from the pre-computed sidecar written by the daily
    action-index job (``skill_candidate_mining.write_sidecar``).  No
    mining scan occurs in the cycle path.
    """
    try:
        from nanobot.runtime import skill_candidate_mining
        items = []
        for candidate in skill_candidate_mining.read_sidecar(state_dir):
            sequence = candidate.get("sequence") or []
            text = " -> ".join(str(x) for x in sequence)
            evidence = (
                f"recurs in {candidate.get('cycles', 0)} distinct cycles over "
                f"{candidate.get('days', 0)} days; samples: "
                + ", ".join(str(x) for x in (candidate.get("samples") or [])[:3])
            )
            items.append(_make_item("skill-candidate", f"package recurring procedure: {text}", evidence))
        return items
    except Exception:
        return []


def _hypothesis_items(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    limit: int | None = 1,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        # Load authoritative lifecycle.json if present
        lifecycle = _read_json(Path(state_dir) / "hypotheses" / "lifecycle.json", {})
        lifecycle_entries: dict[str, Any] = {}
        if isinstance(lifecycle, dict):
            lifecycle_entries = lifecycle.get("hypotheses") or lifecycle.get("entries") or {}
            if not isinstance(lifecycle_entries, dict):
                lifecycle_entries = {}

        def _is_active(cand: dict[str, Any]) -> bool:
            hid = str(cand.get("hypothesis_id") or "").strip()
            title = str(cand.get("task_title") or cand.get("title") or cand.get("hypothesis") or "").strip()
            # Authoritative lifecycle status check
            key = hid or (f"slug-{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]}" if title else "")
            info = lifecycle_entries.get(hid) or lifecycle_entries.get(key) or (lifecycle_entries.get(title) if title else None)
            if isinstance(info, dict):
                st = str(info.get("status") or "").strip().lower()
                if st and st != "active":
                    return False
            elif isinstance(info, str):
                st = info.strip().lower()
                if st and st != "active":
                    return False
            # Direct entry status check
            st = str(cand.get("status") or cand.get("lifecycle_status") or "active").strip().lower()
            return st == "active"

        durable = _read_json(Path(state_dir) / "hypotheses" / "durable.json", None)
        durable_entries = durable.get("entries") if isinstance(durable, dict) else None
        backlog = _read_json(Path(state_dir) / "hypotheses" / "backlog.json", None)
        entries = (durable_entries or []) + (backlog.get("entries", []) if isinstance(backlog, dict) else [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not _is_active(entry):
                continue
            title = str(entry.get("task_title") or entry.get("title") or "").strip()
            if not title or title in seen:
                continue
            if not _hypothesis_has_evidence(entry, selfevo_repo):
                continue
            seen.add(title)
            evidence = str(entry.get("evidence") or entry.get("metric") or entry.get("data_to_collect") or entry.get("insight_criterion") or entry.get("acceptance") or "")
            items.append(_make_item("hypothesis", title, evidence))
        # #1219: ``research/hypotheses.json`` is no longer a source here. Its
        # writer (``cycle_planning._write_research_feed``) was deleted with the
        # planner (#924) and the file froze on 2026-08-22; durable + backlog
        # above are the live hypothesis sources.

        # #878: at most ONE active hypothesis experiment at a time. If a
        # hypothesis already has an unanswered in-flight serving cycle
        # (``hypothesis_backlog.has_in_flight_experiment`` — a 'proposed'
        # row with no terminal 'outcome' row yet), suppress minting a NEW
        # hypothesis demand item entirely this pass, rather than stacking a
        # second parallel experiment on top of the one already running.
        # #1038: do NOT slice items[:1] here pre-fold; collect_demand applies
        # the final per-kind cap after completed/exhausted folds.
        try:
            from nanobot.runtime import hypothesis_backlog

            if hypothesis_backlog.has_in_flight_experiment(state_dir, ledger_rows=ledger_rows):
                return []
        except Exception:
            pass
        return items if limit is None else items[:limit]
    except Exception:
        return items


# ─── kind: decay (#761) ─────────────────────────────────────────────────────


def _decay_items(
    state_dir: Path,
    selfevo_repo: Path | None,
    now: datetime,
    *,
    limit: int | None = _MAX_DECAY_ITEMS,
) -> list[dict[str, str]]:
    """Unused/untouched ``scripts/*.py`` artifacts as archival-proposal
    demand (#761). Staleness is computed by
    ``usage_evidence.stale_artifacts`` from harness-observed evidence ONLY
    (never a claim); this function only shapes the demand items. Bounded to
    the :data:`_MAX_DECAY_ITEMS` oldest; NEVER auto-deletes anything — the
    resulting proposal flows through the normal gate pipeline. Fail-open:
    any error yields no decay demand."""
    try:
        from nanobot.runtime import usage_evidence

        stale = usage_evidence.stale_artifacts(
            state_dir, selfevo_repo, older_than_days=_DECAY_DAYS, now=now
        )
        items: list[dict[str, str]] = []
        for record in stale:
            rel = str(record.get("path") or "").strip()
            since = str(record.get("stale_since") or "").strip()
            if not rel:
                continue
            items.append(
                _make_item(
                    "decay",
                    f"Propose archiving {rel} — unused since {since[:10] or 'unknown'}",
                    f"no harness-observed use or modification in {_DECAY_DAYS}+ days "
                    f"(last evidence {since or 'none'}); propose archival/removal via "
                    "the normal gate — never delete directly",
                    affected_path=rel,
                )
            )
        return items if limit is None else items[:limit]
    except Exception:
        return []


# ─── kind: defect — repair-unused / fix_skill (#845) ───────────────────────


# ─── skill retirement cooldown sidecar (#958) ────────────────────────────────


def _retirement_cooldown_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "skill_retirement_cooldown.json"


def _load_retirement_cooldown(state_dir: Path) -> dict[str, Any]:
    data = _read_json(_retirement_cooldown_path(state_dir), None)
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        return {"schema_version": _SKILL_RETIREMENT_COOLDOWN_SCHEMA, "paths": {}}
    return data


def mark_skill_retired(state_dir: Path, rel: str, now: datetime) -> None:
    """Record a skill path in the retirement cooldown sidecar (#958).

    Called by the demand collector when it emits a retirement item, so
    subsequent passes know this path was retired and can warn on re-creation.
    Fail-open: any write error is silently swallowed.
    """
    try:
        data = _load_retirement_cooldown(state_dir)
        data["paths"][rel] = now.isoformat().replace("+00:00", "Z")
        _write_json(_retirement_cooldown_path(state_dir), data)
    except Exception:
        pass


def retired_skill_paths_in_cooldown(state_dir: Path, now: datetime) -> dict[str, str]:
    """Return skill paths retired within _SKILL_RETIRE_COOLDOWN_DAYS (path -> retired_at).

    Used by the proposer context to warn about re-creation of recently-retired paths.
    Fail-open to empty dict.
    """
    try:
        data = _load_retirement_cooldown(state_dir)
        cutoff = now - timedelta(days=_SKILL_RETIRE_COOLDOWN_DAYS)
        active: dict[str, str] = {}
        for path, ts_raw in data.get("paths", {}).items():
            ts = _parse_ts(ts_raw)
            if ts is not None and ts >= cutoff:
                active[path] = ts_raw
        return active
    except Exception:
        return {}


def _repair_skill_counts(
    state_dir: Path,
    rel_paths: list[str],
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
    completed_entries: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Batch-calculate integrated repair counts for multiple skill paths (#1040).

    Computes repair IDs across all paths and scans ledger_rows once instead of
    rescanning rows for every individual skill file.
    """
    try:
        if not rel_paths:
            return {}
        path_to_repair_ids: dict[str, set[str]] = {}
        all_repair_to_path: dict[str, str] = {}
        for rel in rel_paths:
            ids = {
                item_id("defect", summary[:_MAX_SUMMARY_CHARS])
                for summary in (
                    f"repair: exercise never-read skill {rel} — wire or improve it, do not build a new one-shot",
                    f"repair: re-wire idle skill {rel} — extend or consume it, do not build a new one-shot",
                    f"repair: exercise never-read skill {rel}",
                    f"repair: re-wire idle skill {rel}",
                )
            }
            path_to_repair_ids[rel] = ids
            for d_id in ids:
                all_repair_to_path[d_id] = rel

        completed = (
            completed_entries
            if completed_entries is not None
            else _load_completed(state_dir).get("entries", {})
        )
        counts: dict[str, int] = {rel: 0 for rel in rel_paths}
        for demand_id in completed:
            rel = all_repair_to_path.get(str(demand_id))
            if rel:
                counts[rel] += 1

        cycle_proposed: dict[str, str] = {}
        successful_cycles_by_rel: dict[str, set[str]] = {rel: set() for rel in rel_paths}
        rows = ledger_rows if ledger_rows is not None else _load_ledger_rows(state_dir)
        for row in rows:
            if not isinstance(row, dict):
                continue
            cycle_id = str(row.get("cycle_id") or "").strip()
            if not cycle_id:
                continue
            phase = row.get("phase")
            if phase == "proposed":
                d_id = str(row.get("demand_id") or "")
                if d_id in all_repair_to_path:
                    cycle_proposed[cycle_id] = d_id
            elif (
                phase == "outcome"
                and str(row.get("outcome") or "").strip().lower() == "success"
                and cycle_id in cycle_proposed
            ):
                d_id = cycle_proposed[cycle_id]
                rel = all_repair_to_path.get(d_id)
                if rel:
                    successful_cycles_by_rel[rel].add(cycle_id)

        for rel in rel_paths:
            counts[rel] += len(successful_cycles_by_rel[rel])
        return counts
    except Exception:
        return {rel: 0 for rel in rel_paths}


def _completed_repair_count_for_skill(
    state_dir: Path,
    rel: str,
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
    completed_entries: dict[str, Any] | None = None,
) -> int:
    """Count integrated repair-unused cycles for the exact skill path.

    The completed sidecar is the durable summary for ordinary demand reads, but
    a stable demand id can recur after a reset.  Count successful proposed →
    outcome pairs in the bounded cycle ledger as well, de-duplicating by cycle
    id.  A sidecar entry without a cycle id remains useful for small migrations
    and tests.  Never trust a path from a sidecar as a workspace skill: callers
    enumerate real ``skills/*/SKILL.md`` files.
    """
    counts = _repair_skill_counts(
        state_dir,
        [rel],
        ledger_rows=ledger_rows,
        completed_entries=completed_entries,
    )
    return counts.get(rel, 0)


# ─── kind: defect — repair-unused / fix_skill (#845) ───────────────────────


def _repair_unused_items(
    state_dir: Path,
    selfevo_repo: Path | None,
    now: datetime,
    *,
    limit: int | None = _MAX_REPAIR_UNUSED_ITEMS,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Recently-idle existing skills as a narrow ``defect`` repair demand
    (#845, OpenSpace fix_skill). A ``scripts/*.py`` whose harness-observed
    use went idle in the [_REPAIR_UNUSED_MIN_DAYS, _DECAY_DAYS) band is a
    skill that WORKED then fell idle — steer the loop to re-wire/extend it
    (an EDIT to an existing file, which #834 permanent-novelty correctly
    never blocks) instead of building a fresh one-shot. Disjoint from the
    decay/archival band (>= _DECAY_DAYS) by construction, so a script is
    never simultaneously a repair and an archival candidate. Bounded to
    :data:`_MAX_REPAIR_UNUSED_ITEMS`; ties #840 (reuse) / #838 (usage).
    Fail-open: any error yields no repair demand.

    #958: skills/*/SKILL.md paths that have zero confirmed reads AND are
    past the never-read grace period AND have already been the subject of
    _SKILL_RETIRE_AFTER_REPAIR_CYCLES integrated repair-unused cycles yield
    a *retirement* item instead of another repair item. Both kinds share the
    _MAX_REPAIR_UNUSED_ITEMS cap. Retired paths are recorded in the cooldown
    sidecar; re-creation within _SKILL_RETIRE_COOLDOWN_DAYS surfaces a
    warning in the proposer context (see retired_skill_paths_in_cooldown).
    """
    try:
        from nanobot.runtime import usage_evidence

        fresh = usage_evidence.stale_artifacts(
            state_dir, selfevo_repo, older_than_days=_REPAIR_UNUSED_MIN_DAYS, now=now
        )
        decay_paths = {
            str(r.get("path") or "").strip()
            for r in usage_evidence.stale_artifacts(
                state_dir, selfevo_repo, older_than_days=_DECAY_DAYS, now=now
            )
        }
        items: list[dict[str, str]] = []
        for record in fresh:
            rel = str(record.get("path") or "").strip()
            since = str(record.get("stale_since") or "").strip()
            if not rel or rel in decay_paths:
                continue  # empty, or belongs to the decay/archival band
            items.append(
                _make_item(
                    "defect",
                    f"repair: re-wire idle skill {rel} — extend or consume it, do not build a new one-shot",
                    f"no harness-observed use or modification in {_REPAIR_UNUSED_MIN_DAYS}-{_DECAY_DAYS}d "
                    f"(last evidence {since or 'unknown'}); reuse/wire it into a live path via the "
                    "normal gate rather than proposing a new script (#845, ties #840)",
                    affected_path=rel,
                )
            )
        # #940: workspace skills use the same bounded repair-unused demand.
        if selfevo_repo is None:
            return items if limit is None else items[:limit]
        from nanobot.runtime import skill_fitness
        last_reads = skill_fitness.last_confirmed_skill_reads(state_dir)
        skills_root = Path(selfevo_repo) / "skills"
        if skills_root.is_dir():
            skill_files = [
                sf for sf in sorted(skills_root.glob("*/SKILL.md"))
                if sf.is_file() and sf.relative_to(selfevo_repo).as_posix().startswith("skills/")
            ]
            all_skill_rels = [sf.relative_to(selfevo_repo).as_posix() for sf in skill_files]
            cached_completed = _load_completed(state_dir).get("entries", {})
            batch_repair_counts = _repair_skill_counts(
                state_dir,
                all_skill_rels,
                ledger_rows=ledger_rows,
                completed_entries=cached_completed,
            )
            for skill_file in skill_files:
                rel = skill_file.relative_to(selfevo_repo).as_posix()
                skill_name = skill_file.parent.name
                last_read = last_reads.get(skill_name)
                created_raw = usage_evidence._git_creation_iso(selfevo_repo, rel)
                created = usage_evidence._parse_ts(created_raw) if created_raw else None
                last = usage_evidence._parse_ts(last_read) if last_read else None
                if last is None:
                    if created is None or (now - created).days < _SKILL_NEVER_READ_GRACE_DAYS:
                        continue
                    # #958: never-read past grace — check retirement condition
                    repair_count = batch_repair_counts.get(rel, 0)
                    if repair_count >= _SKILL_RETIRE_AFTER_REPAIR_CYCLES:
                        summary = (
                            f"retire skill {rel}: fold anything reusable into docs/ or "
                            "another skill, then delete the directory"
                        )
                        items.append(
                            _make_item(
                                "defect",
                                summary,
                                f"zero confirmed reads after grace period and "
                                f"{repair_count} integrated repair-unused cycles; "
                                "delete via normal gated cycle (git history preserves content)",
                                affected_path=rel,
                            )
                        )
                        mark_skill_retired(state_dir, rel, now)
                    else:
                        summary = f"repair: exercise never-read skill {rel} — wire or improve it, do not build a new one-shot"
                        items.append(_make_item("defect", summary, f"harness-confirmed skill path={rel}; repair-unused demand" , affected_path=rel))
                elif _REPAIR_UNUSED_MIN_DAYS <= (now - last).days < _DECAY_DAYS:
                    summary = f"repair: re-wire idle skill {rel} — extend or consume it, do not build a new one-shot"
                    items.append(_make_item("defect", summary, f"harness-confirmed skill path={rel}; repair-unused demand" , affected_path=rel))
                else:
                    continue
        return items if limit is None else items[:limit]
    except Exception:
        return []


# ─── completed-demand ledger-chain done-truth (#773) ────────────────────────



def _completed_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "completed.json"


def _load_completed(state_dir: Path) -> dict[str, Any]:
    data = _read_json(_completed_path(state_dir), None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"schema_version": _COMPLETED_SCHEMA, "entries": {}}
    return data


def completed_demand_ids(state_dir: Path) -> set[str]:
    """Read-only view of the completed-demand sidecar's ids (#773) — the
    ledger-chain done-truth consumed by
    ``goal_text_utils.filter_completed_priorities_from_goal_text`` (lazy
    import there; this module already imports goal_text_utils lazily, so
    goal_text_utils must never import demand at module level). Fail-open:
    any error reads as "nothing completed"."""
    try:
        return set(_load_completed(Path(state_dir))["entries"].keys())
    except Exception:
        return set()


def _fold_completed(
    state_dir: Path,
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Fold (proposed(demand_id) → same-cycle terminal ``outcome: success``)
    ledger pairs into the completed-demand sidecar and return all completed
    ids (#773, live P14 evidence 2026-07-15/16).

    In demand mode the model *refines* proposal titles, so text-based
    done-evidence (git-log labels/basenames, #748/#769) structurally cannot
    retire a completed goal_text priority — the authoritative chain is the
    ledger's own ``proposed`` row carrying ``demand_id`` followed by a
    terminal ``outcome: success`` row for the same ``cycle_id``. One pass
    over the shared :data:`_LEDGER_ROWS_HORIZON_DAYS` ledger window (live
    file plus rotated archives via ``state_access.ledger_window``, #1175;
    #790 first made the read rotation-aware because a pair straddling the
    midnight rotation — proposed 23:49, success 00:06, live P16
    2026-07-17/18 — otherwise never folds and the priority is re-proposed
    until exhaustion). New pairs are merged into the sidecar
    append-only (existing entries are never overwritten), which makes
    done-truth rotation-proof by construction: once folded, an entry
    survives the midnight ledger rotation that blinds every single-file
    ledger reader (the #771/#772 blind spot). Fail-open: an unreadable
    ledger or sidecar degrades to whatever the sidecar already holds.

    #813: each folded entry also carries the ``proposed`` row's own
    ``serves`` value (``""`` if the row predates this field or carried
    none) — the benchmark-evidence gate in
    ``usage_evidence.confirm_serves`` reads it via
    ``benchmark_evidence.is_optimization_claim`` to tell an optimization
    claim from an ordinary entry without re-reading the ledger."""
    try:
        data = _load_completed(state_dir)
        entries: dict[str, Any] = data["entries"]
        demand_by_cycle: dict[str, str] = {}
        # #813: the 'proposed' row's own ``serves`` value, folded alongside
        # ``demand_id`` so the confirmation path (usage_evidence.confirm_serves)
        # can later tell whether a completed entry is an optimization claim
        # without re-reading the ledger.
        serves_by_cycle: dict[str, str] = {}
        success_by_cycle: dict[str, dict[str, Any]] = {}

        rows = ledger_rows if ledger_rows is not None else _load_ledger_rows(state_dir)
        for row in rows:
            if not isinstance(row, dict):
                continue
            cycle_id = str(row.get("cycle_id") or "").strip()
            if not cycle_id:
                continue
            phase = row.get("phase")
            if phase == "proposed":
                demand_id = str(row.get("demand_id") or "").strip()
                if demand_id:
                    demand_by_cycle[cycle_id] = demand_id
                serves = str(row.get("serves") or "").strip()
                if serves:
                    serves_by_cycle[cycle_id] = serves
            elif phase == "outcome":
                if str(row.get("outcome") or "").strip().lower() == "success":
                    success_by_cycle[cycle_id] = row
        changed = False
        for cycle_id, demand_id in demand_by_cycle.items():
            if demand_id in entries:
                continue  # append-only: never overwrite an existing entry
            success = success_by_cycle.get(cycle_id)
            if success is None:
                continue  # no terminal success for this cycle — not done
            files = success.get("files_changed")
            tier = success.get("change_tier")
            if tier is None:
                tier = classify_change_tier(files if isinstance(files, list) else [])
            entries[demand_id] = {
                "cycle_id": cycle_id,
                "ts": str(success.get("ts") or ""),
                "files_changed": files if isinstance(files, list) else [],
                "change_tier": tier,
                # #813: empty string when the proposed row carried no serves
                # (or predates this field) — is_optimization_claim("") is
                # False, so older/serves-less entries are unaffected.
                "serves": serves_by_cycle.get(cycle_id, ""),
            }
            changed = True
        if changed:
            data["entries"] = entries
            _write_json(_completed_path(state_dir), data)
        return set(entries.keys())
    except Exception:
        if ledger_rows is not None:
            try:
                return set(_load_completed(Path(state_dir))["entries"].keys())
            except Exception:
                return set()
        return completed_demand_ids(state_dir)


# ─── exhaustion tracking ────────────────────────────────────────────────────


def _exhausted_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "exhausted.json"


def _load_exhausted(state_dir: Path) -> dict[str, Any]:
    data = _read_json(_exhausted_path(state_dir), None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"schema_version": _EXHAUSTED_SCHEMA, "entries": {}}
    return data


def _runtime_release_id() -> str:
    """Identifier of the running runtime release (#771).

    On the eeepc host the runtime executes from
    ``/opt/eeepc-agent/runtimes/self-evolving-agent/releases/<id>/`` (the
    ``current`` symlink is on PYTHONPATH; ``resolve()`` dereferences it), so
    the path segment after ``releases`` IS the release id. Outside a release
    layout (dev checkout, tests) fall back to the product version. Empty
    string means "unknown" and never triggers a release-change reset —
    fail-open in the direction of not resetting spuriously."""
    try:
        parts = Path(__file__).resolve().parts
        for i, part in enumerate(parts[:-1]):
            if part == "releases":
                return parts[i + 1]
    except Exception:
        pass
    try:
        from nanobot import __version__

        return str(__version__)
    except Exception:
        return ""


def _latest_success_ts(
    state_dir: Path,
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> datetime | None:
    """Timestamp of the newest terminal ``outcome: success`` ledger row, or
    ``None``. Any successful integration resets exhaustion (#771) — HEAD
    moves on integration anyway; this makes the reset explicit and immediate,
    closing the circularity where an exhausted-only demand set meant nothing
    ever integrated and HEAD never moved. Same single-pass bounded-read
    discipline as the other ledger readers; fail-open to ``None``."""
    latest: datetime | None = None
    try:
        rows = ledger_rows if ledger_rows is not None else _load_ledger_rows(state_dir)
        for row in rows:
            if not isinstance(row, dict) or row.get("phase") != "outcome":
                continue
            if str(row.get("outcome") or "").strip().lower() != "success":
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
        return latest
    except Exception:
        return latest


def _self_dedup_reject_ts_by_demand_id(
    state_dir: Path,
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> dict[str, list[datetime]]:
    """Timestamps of demand-linked no-op outcomes per demand id.

    Self-dedup proposer rejects and terminal no-op outcomes both consume the
    same bounded exhaustion budget. Rows without a demand id are ignored.
    """
    out: dict[str, list[datetime]] = {}
    try:
        rows = ledger_rows if ledger_rows is not None else _load_ledger_rows(state_dir)
        demand_by_cycle = {
            str(row.get("cycle_id") or "").strip(): str(row.get("demand_id") or "").strip()
            for row in rows
            if isinstance(row, dict) and row.get("phase") == "proposed"
            and str(row.get("demand_id") or "").strip()
        }
        result_noops = _completed_no_commit_ts_by_demand_id(state_dir, demand_by_cycle)
        for row in rows:
            if not isinstance(row, dict):
                continue
            phase = row.get("phase")
            reason = str(row.get("reason") or "").strip().lower()
            if phase == "proposer_reject" and reason == "self_dedup":
                demand_id = str(row.get("demand_id") or "").strip()
            elif phase == "outcome" and str(row.get("outcome") or "").strip().lower() in _NOOP_OUTCOMES:
                demand_id = str(row.get("demand_id") or "").strip()
                if not demand_id:
                    demand_id = demand_by_cycle.get(str(row.get("cycle_id") or "").strip(), "")
                    if not demand_id:
                        match = re.search(r"(?:assigned|demand)[=: ]+([A-Za-z0-9_-]+)", reason)
                        demand_id = match.group(1) if match else ""
            else:
                continue
            if not demand_id:
                continue
            ts = _parse_ts(row.get("ts")) or datetime.now(timezone.utc)
            out.setdefault(demand_id, []).append(ts)
        for demand_id, timestamps in result_noops.items():
            out.setdefault(demand_id, []).extend(timestamps)
        return out
    except Exception:
        return out


def _completed_no_commit_ts_by_demand_id(
    state_dir: Path,
    demand_by_cycle: dict[str, str],
) -> dict[str, list[datetime]]:
    """Read bounded bridge results and credit demand-linked no-commit runs.

    Malformed result files are skipped independently. The bridge's terminal
    ledger row is checked by the caller's cycle mapping; this helper only
    supplies the result-side ``completed_no_commit`` evidence and timestamp.
    """
    out: dict[str, list[datetime]] = {}
    try:
        paths: list[Path] = []
        for dirname in _RESULT_DIRS:
            results_dir = Path(state_dir) / "subagents" / dirname
            if results_dir.is_dir():
                paths.extend(p for p in results_dir.glob("*.json") if p.is_file())
        if not paths:
            return out
        try:
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        for path in paths[:_MAX_RESULT_NOOP_FILES]:
            try:
                result = _read_json(path, None)
                if not isinstance(result, dict):
                    continue
                classification = str(result.get("learning_classification") or "").strip().lower()
                status = str(result.get("result_status") or result.get("status") or "").strip().lower()
                if classification != "completed_no_commit" and status not in {"completed_no_commit", "no_commit"}:
                    continue
                cycle_id = str(result.get("cycle_id") or "").strip()
                demand_id = demand_by_cycle.get(cycle_id, "")
                if not demand_id:
                    continue
                ts = _parse_ts(result.get("created_at"))
                if ts is None:
                    ts = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                out.setdefault(demand_id, []).append(ts)
            except Exception:
                continue
        return out
    except Exception:
        return out


def _filter_exhausted(
    state_dir: Path,
    items: list[dict[str, str]],
    head: str | None,
    *,
    now: datetime | None = None,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Drop exhausted items; exhaustion resets (#771) on any of: a newer
    successful integration, a runtime release change, a repo HEAD move, or
    the 24h expiry.

    An expired entry becomes a ``reset`` record with ``reset_at`` so only
    self-dedup rejects NEWER than the reset count toward re-exhaustion —
    otherwise the old ledger rows would re-exhaust the item the moment its
    exhaustion expired. A MISSING entry gets the same protection (#771
    honest manual clear): only rejects newer than the newest of (last
    success outcome, 24h ago) count, so deleting ``entries`` behaves like a
    reset instead of being silently undone by stale ledger rows. Fail-open:
    any error returns ``items`` unchanged.
    """
    try:
        now = now or datetime.now(timezone.utc)
        rows = ledger_rows if ledger_rows is not None else _load_ledger_rows(state_dir)
        ledger_status = window_status(rows)
        if ledger_status == "unavailable":
            # #1175 rule (3): a blind ledger is not evidence; keep the persisted
            # verdicts and present the items as they are.
            _LOG.warning("exhaustion filter: ledger window unavailable; exhausted.json left untouched")
            return items
        data = _load_exhausted(state_dir)
        entries: dict[str, Any] = data["entries"]
        rejects = _self_dedup_reject_ts_by_demand_id(state_dir, ledger_rows=rows)
        success_ts = _latest_success_ts(state_dir, ledger_rows=rows)
        release = _runtime_release_id()
        now_iso = now.isoformat().replace("+00:00", "Z")
        changed = False
        out: list[dict[str, str]] = []
        for item in items:
            iid = item["id"]
            entry = entries.get(iid)
            reset_at: datetime | None = None
            if isinstance(entry, dict) and entry.get("status") == "exhausted":
                exhausted_at = _parse_ts(entry.get("exhausted_at")) or now
                head_moved = bool(
                    head and entry.get("git_head") and head != entry.get("git_head")
                )
                entry_release = str(entry.get("release") or "")
                release_changed = bool(release and entry_release and release != entry_release)
                # #1175 rule: absence of a success in a partial window is not
                # evidence, and presence in one is only a lower bound on
                # recency — a reset needs a complete window.
                success_reset = ledger_status == "complete" and success_ts is not None and success_ts > exhausted_at
                expired = (
                    success_reset
                    or release_changed
                    or head_moved
                    or (now - exhausted_at) >= timedelta(hours=_EXHAUSTION_EXPIRY_HOURS)
                )
                if not expired:
                    continue  # still exhausted — item stays hidden
                reset_iso = (
                    success_ts.isoformat().replace("+00:00", "Z")
                    if success_reset and success_ts is not None
                    else now_iso
                )
                reset_entry: dict[str, Any] = {"status": "reset", "reset_at": reset_iso}
                if isinstance(entry, dict) and isinstance(entry.get("escalated"), dict):
                    reset_entry["escalated"] = entry["escalated"]
                entry = reset_entry
                entries[iid] = entry
                changed = True
            if isinstance(entry, dict) and entry.get("status") == "reset":
                reset_at = _parse_ts(entry.get("reset_at"))
            elif not isinstance(entry, dict):
                # No sidecar entry: fresh item OR a manual `entries` clear
                # (#771). Floor the countable rejects at 24h ago so stale
                # bug-era ledger rows cannot resurrect a cleared exhaustion.
                reset_at = now - timedelta(hours=_EXHAUSTION_EXPIRY_HOURS)
            # Any successful integration raises the floor further (#771).
            if success_ts is not None and (reset_at is None or success_ts > reset_at):
                reset_at = success_ts

            item_rejects = rejects.get(iid, [])
            if reset_at is not None:
                item_rejects = [ts for ts in item_rejects if ts > reset_at]
            if len(item_rejects) >= _EXHAUSTION_REJECTS:
                # Optional escalation gets the next serving cycle before the
                # normal exhaustion filter hides the item. It is disabled
                # entirely when SELFEVO_ESCALATION_MODEL is unset.
                if escalation_model() and not _escalation_marker(state_dir, iid):
                    out.append(item)
                    continue
                exhausted_entry = {
                    "status": "exhausted",
                    "exhausted_at": now_iso,
                    "git_head": head or "",
                    "release": release,
                    "rejects": len(item_rejects),
                }
                if isinstance(entry, dict) and isinstance(entry.get("escalated"), dict):
                    exhausted_entry["escalated"] = entry["escalated"]
                entries[iid] = exhausted_entry
                changed = True
                continue
            out.append(item)
        if changed:
            data["entries"] = entries
            _write_json(_exhausted_path(state_dir), data)
        return out
    except Exception:
        return items


def escalation_model() -> str:
    """Return the optional, operator-configured escalation model."""
    try:
        from nanobot.runtime.model_registry import resolve_model
        return resolve_model("escalation")
    except Exception:
        return os.environ.get(_ESCALATION_MODEL_ENV, "").strip()


def _escalation_marker(state_dir: Path, demand_id: str) -> dict[str, Any] | None:
    for path in (_exhausted_path(state_dir), _completed_path(state_dir)):
        payload = _read_json(path, {})
        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        entry = entries.get(demand_id) if isinstance(entries, dict) else None
        marker = entry.get("escalated") if isinstance(entry, dict) else None
        if isinstance(marker, dict) and marker.get("cycle_id") and marker.get("model"):
            return marker
    return None


def should_escalate(state_dir: Path, demand_id: str) -> bool:
    """Return whether this demand has earned its single optional escalation.

    The same ledger/result evidence used by exhaustion is the trigger. This
    keeps the opt-in rung off for fresh items and makes the decision survive
    proposer/bridge process boundaries.
    """
    try:
        model = escalation_model()
        if not model or not demand_id or _escalation_marker(Path(state_dir), demand_id):
            return False
        rejects = _self_dedup_reject_ts_by_demand_id(Path(state_dir))
        return len(rejects.get(demand_id, [])) >= _EXHAUSTION_REJECTS
    except Exception:
        return False


def record_escalation(
    state_dir: Path,
    demand_id: str,
    cycle_id: str,
    model: str,
    ts: str | None = None,
) -> bool:
    """Persist one escalation marker and report whether it was durable."""
    if not demand_id or not cycle_id or not model or _escalation_marker(Path(state_dir), demand_id):
        return False
    try:
        data = _load_exhausted(state_dir)
        entry = data["entries"].setdefault(demand_id, {})
        entry.setdefault(
            "escalated",
            {"cycle_id": cycle_id, "model": model, "ts": ts or datetime.now(timezone.utc).isoformat()},
        )
        data["entries"][demand_id] = entry
        path = _exhausted_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return _escalation_marker(Path(state_dir), demand_id) is not None
    except Exception:
        return False


def _fold_already_delivered_priorities(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    ledger_rows: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Fold evidence-backed already-delivered priority no-ops into done state.

    A ``completed_no_commit`` result must identify the serving cycle and target
    path; the path is accepted only when it exists in the current checkout and
    the checkout is on ``main``. The completed entry records the cycle and
    verification evidence, while leaving usage/fitness signals untouched.
    """
    if not selfevo_repo:
        return set()
    try:
        repo = Path(selfevo_repo)
        branch = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        )
        if branch.returncode != 0 or branch.stdout.strip() != "main":
            return set()
        rows = ledger_rows if ledger_rows is not None else _load_ledger_rows(state_dir)
        proposed = {
            str(row.get("cycle_id") or "").strip(): str(row.get("demand_id") or "").strip()
            for row in rows
            if isinstance(row, dict) and row.get("phase") == "proposed"
            and str(row.get("demand_id") or "").strip()
        }
        completed = _load_completed(state_dir)
        entries = completed["entries"]
        changed = False
        folded: set[str] = set()
        result_dirs = [Path(state_dir) / "subagents" / dirname for dirname in _RESULT_DIRS]
        candidates = [
            path
            for result_dir in result_dirs
            if result_dir.is_dir()
            for path in result_dir.glob("*.json")
            if path.is_file()
        ]
        terminal_outcomes = {
            str(row.get("cycle_id") or "").strip(): row
            for row in rows
            if isinstance(row, dict)
            and row.get("phase") == "outcome"
            and str(row.get("cycle_id") or "").strip()
            and str(row.get("outcome") or "").strip().lower() in {"partial", "completed_no_commit"}
        }
        for result_path in candidates:
            try:
                result = _read_json(result_path, None)
                if not isinstance(result, dict):
                    continue
                classification = str(result.get("learning_classification") or "").strip().lower()
                result_status = str(result.get("result_status") or result.get("status") or "").strip().lower()
                if classification != "completed_no_commit" and result_status not in {"completed_no_commit", "no_commit"}:
                    continue
                cycle_id = str(result.get("cycle_id") or "").strip()
                demand_id = proposed.get(cycle_id, "")
                if not demand_id or not demand_id.startswith("priority-") or demand_id in entries:
                    continue
                target = str(result.get("target_path") or "").strip().replace("\\", "/")
                if not target or Path(target).is_absolute() or ".." in Path(target).parts:
                    continue
                if cycle_id not in terminal_outcomes:
                    continue
                target_path = repo / target
                tracked = subprocess.run(
                    ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD", "--", target],
                    capture_output=True, text=True, timeout=10,
                )
                if tracked.returncode != 0 or tracked.stdout.strip() != target or not target_path.is_file():
                    continue
                outcome_ts = str(terminal_outcomes[cycle_id].get("ts") or result.get("created_at") or "")
                entries[demand_id] = {
                    "cycle_id": cycle_id,
                    "ts": outcome_ts,
                    "files_changed": [],
                    "change_tier": "code-bearing",
                    "serves": "",
                    "evidence": {
                        "verification_cycle_id": cycle_id,
                        "target_path": target,
                        "target_exists_on_main": True,
                    },
                }
                folded.add(demand_id)
                changed = True
            except Exception:
                continue
        if changed:
            completed["entries"] = entries
            _write_json(_completed_path(state_dir), completed)
        return folded
    except Exception:
        return set()


def _emit_vector_split_event(state_dir: Path, items: list[dict[str, str]]) -> None:
    """Operator-visible ledger event exposing the V1-vs-V2 demand split
    (#815) — a structured ``demand_vector_split`` row via the same
    ``append_event`` mechanism every other phase uses, counting the FINAL
    presented items by ``vector`` (``V1``, ``V2``, or ``unknown`` for
    anything untagged/unclassifiable). Best-effort: any error is swallowed
    and never affects the returned demand list."""
    try:
        from nanobot.runtime.cycle_ledger import append_event

        counts = {"V1": 0, "V2": 0, "unknown": 0}
        for item in items:
            v = item.get("vector") or ""
            counts[v if v in ("V1", "V2") else "unknown"] += 1
        append_event(state_dir, {"phase": "demand_vector_split", **counts})
    except Exception:
        pass


def _reflection_items(
    state_dir: Path,
    now: datetime | None = None,
    *,
    limit: int | None = _MAX_REFLECTION_ITEMS,
) -> list[dict[str, str]]:
    """Turn reflector recommendations into normal, evidence-linked demand (#1038).

    - Per-line malformed json guard (skips bad line without crashing)
    - Freshness window: requires valid timestamp, ignores reflections older than ``_REFLECTION_MAX_AGE_DAYS``
    - Ignores reflections whose status is not empty/active (e.g. 'consumed')
    - Collects all valid candidates; final presentation cap applied post-fold in collect_demand
    """
    items: list[dict[str, str]] = []
    try:
        from nanobot.runtime import reflector

        # #1178: the journal rotates at 512 KiB into reflector/archive/; read the
        # newest archives plus the live file through the reflector's own reader.
        if not reflector.reflection_files(state_dir):
            return items
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=_REFLECTION_MAX_AGE_DAYS)
        for row in reflector.iter_reflection_rows(state_dir):
            if not isinstance(row, dict) or not row.get("cycle_id"):
                continue
            row_status = str(row.get("status") or "").strip().lower()
            if row_status and row_status != "active":
                continue
            ts_raw = row.get("created_at") or row.get("timestamp") or row.get("ts")
            if not ts_raw:
                continue
            ts_val = _parse_ts(ts_raw)
            if ts_val is None or ts_val < cutoff:
                continue
            for recommendation in row.get("recommendations") or []:
                if not isinstance(recommendation, dict):
                    continue
                rec_status = str(recommendation.get("status") or "").strip().lower()
                if rec_status and rec_status != "active":
                    continue
                detail = str(recommendation.get("detail") or "").strip()
                if detail:
                    evidence = str(recommendation.get("evidence") or "").strip()
                    target_artifact = str(recommendation.get("target_artifact") or recommendation.get("target_path") or "").strip()
                    if not target_artifact:
                        m = _PATH_TOKEN_RE.search(detail)
                        if m:
                            target_artifact = m.group(0)
                    item = _make_item("reflection", detail, f"cycle {row['cycle_id']}: {evidence}", affected_path=target_artifact)
                    try:
                        from nanobot.runtime import reflector
                        completed = _read_json(Path(state_dir) / "demand" / "completed.json", {})
                        if item["id"] in (completed.get("entries", {}) if isinstance(completed, dict) else {}):
                            reflector.mark_reflection_consumed(state_dir, detail, cycle_id=str(row.get("cycle_id") or ""))
                            continue
                    except Exception:
                        pass
                    items.append(item)
        return items if limit is None else items[:limit]
    except Exception:
        return items


# ─── public entrypoint ──────────────────────────────────────────────────────


def _uncapped(builder: Any, *args: Any, **kwargs: Any) -> list[dict[str, str]]:
    """Call a lane builder without its presentation cap.

    The fallback preserves compatibility with older test doubles and optional
    callers that still expose the pre-1038 signature; production builders all
    accept ``limit`` explicitly.
    """
    try:
        return builder(*args, limit=None, **kwargs)
    except TypeError as exc:
        if "limit" not in str(exc):
            raise
        return builder(*args, **kwargs)


def _apply_futile_surfaces(state_dir: Path, items: list[dict[str, str]]) -> list[dict[str, str]]:
    """#1184: a futile lever-addressable gap suppresses every non-``defect`` item
    aimed at its surface, not just the goal-gap item itself — 11 of the 12
    live ``stale_feeds`` attacks came from the priority/reflection/hypothesis
    lanes, which the per-id filter never saw. ``defect`` items survive: a
    broken feed or checker script must stay repairable. Class A (#1173): a
    lower-bound count may suppress. Fail-open: any error returns ``items``."""
    try:
        from nanobot.runtime import goal_gap_futility

        surfaces = goal_gap_futility.futile_surfaces(state_dir)
        if not surfaces:
            return items
        kept: list[dict[str, str]] = []
        dropped: list[tuple[str, str]] = []
        for item in items:
            if item.get("kind") == "defect":
                kept.append(item)
                continue
            paths = [item.get("affected_path", "")] + [
                m.strip(".,;:()[]{}") for m in _PATH_TOKEN_RE.findall(str(item.get("summary") or ""))[:20]
            ]
            hit = next((s for s in surfaces if goal_gap_futility.surface_hits(s["surface"], paths)), None)
            if hit is None:
                kept.append(item)
            else:
                dropped.append((item["id"], str(hit.get("gap_id"))))
        if dropped:
            _LOG.warning("futile surface: dropped %d demand item(s): %s", len(dropped), dropped[:5])
        return kept
    except Exception:
        return items


def collect_demand(
    state_dir: Path, selfevo_repo: Path | None, *, emit_split: bool = False
) -> list[dict[str, str]]:
    """Collect all current demand items, trust order (priority > defect >
    goal-gap > skill-candidate > hypothesis > decay > reflection),
    exhausted items filtered out, per-kind bounds applied after completed/exhausted
    folds (#1038). Deterministic, no LLM call. Fail-open: any error degrades to fewer
    (possibly zero) items, never raises.

    ``emit_split`` (#815, default OFF): opt-in best-effort
    ``demand_vector_split`` ledger event. ``collect_demand`` runs at least
    twice per proposer cycle — the gate probe (``llm_proposer.should_propose``)
    and the context build (``llm_proposer.build_context`` call site) — so an
    unconditional emit would double-count the operator-visible split. Only
    the context-build call site passes ``emit_split=True``; the gate probe
    leaves it default ``False``, giving exactly one row per cycle."""
    try:
        state_dir = Path(state_dir)
        now = datetime.now(timezone.utc)
        head = _git_head(selfevo_repo)

        # #761: keep the usage-evidence layer current (watermark-cheap) and
        # run the declared→confirmed serves tie-back — BEFORE decay items
        # are built from that evidence. Wrapped fail-open on its own: a
        # usage bug must never block demand collection.
        try:
            from nanobot.runtime import usage_evidence

            usage_evidence.refresh_usage(state_dir, selfevo_repo)
            usage_evidence.confirm_serves(state_dir, selfevo_repo)
        except Exception:
            pass

        # #1040: parse cycles.jsonl once per collect_demand and thread through helpers
        ledger_rows = _load_ledger_rows(state_dir)

        # #1114: fold a priority whose no-commit cycle verified that its
        # target already exists on the current main checkout. This is a
        # deterministic state check, not a fabricated success event.
        _fold_already_delivered_priorities(state_dir, selfevo_repo, ledger_rows=ledger_rows)

        items: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for batch in (
            _priority_items(state_dir, selfevo_repo),
            _uncapped(_ledger_defects, state_dir, now, ledger_rows=ledger_rows),
            _uncapped(_result_file_defects, state_dir, now, ledger_rows=ledger_rows),
            _uncapped(_compile_defects, state_dir, selfevo_repo, head),
            _uncapped(_heldout_defect_items, state_dir),
            _uncapped(_validator_defect_items, state_dir),
            _uncapped(_skill_eval_defect_items, state_dir),
            _uncapped(_knowledge_lift_defect_items, state_dir),
            _uncapped(_curator_unsupported_items, state_dir),
            _uncapped(_tamper_defect_items, state_dir, selfevo_repo),
            _uncapped(_repair_unused_items, state_dir, selfevo_repo, now, ledger_rows=ledger_rows),
            _uncapped(_goal_gap_items, state_dir, selfevo_repo, ledger_rows=ledger_rows),
            _uncapped(_artifact_gap_items, state_dir, selfevo_repo),
            _skill_candidate_items(state_dir, selfevo_repo),
            _uncapped(_hypothesis_items, state_dir, selfevo_repo, ledger_rows=ledger_rows),
            _uncapped(_decay_items, state_dir, selfevo_repo, now),
            _uncapped(_reflection_items, state_dir, now),
        ):
            for item in batch:
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                items.append(item)

        # #773: ledger-chain done-truth — fold (proposed(demand_id) →
        # same-cycle success) pairs into the completed sidecar, then drop
        # completed ids from ALL demand kinds BEFORE the exhausted filter:
        # a completed item needs no exhaustion bookkeeping at all (this
        # also supersedes #772's success-reset blind spot for these items).
        # #778: for the ``goal-gap`` kind ONLY, completed-suppression is
        # time-boxed to _GOAL_GAP_COMPLETED_TTL_DAYS (entry ``ts``): a
        # metric can legitimately regress, so after the TTL the gap may be
        # presented again. Every other kind stays permanently suppressed —
        # a done priority/defect stays done. An unparseable/missing entry
        # ts counts as expired (fail-open toward re-presenting the gap).
        # #925: validator-harness defects get the same treatment for the same
        # reason — their summary is constant per script, so permanent
        # suppression would silence a validator that breaks again later.
        completed = _fold_completed(state_dir, ledger_rows=ledger_rows)
        if completed:
            try:
                from nanobot.runtime import reflector

                for item in items:
                    if item.get("kind") == "reflection" and item["id"] in completed:
                        reflector.mark_reflection_consumed(
                            state_dir,
                            recommendation_detail=item.get("summary", ""),
                            demand_id=item["id"],
                        )
            except Exception:
                pass
            entries = _load_completed(state_dir)["entries"]
            ttl = timedelta(days=_GOAL_GAP_COMPLETED_TTL_DAYS)
            kept: list[dict[str, str]] = []
            for item in items:
                if item["id"] not in completed:
                    kept.append(item)
                    continue
                is_validator = item["summary"].startswith(_VALIDATOR_SUMMARY_PREFIX)
                if item["kind"] == "goal-gap" or is_validator:
                    entry = entries.get(item["id"])
                    ts = _parse_ts(entry.get("ts")) if isinstance(entry, dict) else None
                    item_ttl = (
                        timedelta(days=_VALIDATOR_COMPLETED_TTL_DAYS) if is_validator else ttl
                    )
                    if ts is None or (now - ts) >= item_ttl:
                        kept.append(item)  # TTL elapsed — the condition may have recurred
            items = kept

        result = _filter_exhausted(state_dir, items, head, now=now, ledger_rows=ledger_rows)

        # #1038: Apply per-kind caps AFTER completed/exhausted folds so that
        # completed/exhausted items in the front of a lane do not push out live items.
        kind_caps: dict[str, int] = {
            "priority": _MAX_PRIORITY_ITEMS,
            "defect": _MAX_DEFECT_ITEMS,
            "goal-gap": _MAX_GOAL_GAP_ITEMS,
            "artifact-gap": _MAX_ARTIFACT_GAP_ITEMS,
            "skill-candidate": _MAX_SKILL_CANDIDATE_ITEMS,
            "hypothesis": _MAX_HYPOTHESIS_ITEMS,
            "decay": _MAX_DECAY_ITEMS,
            "repair-unused": _MAX_REPAIR_UNUSED_ITEMS,
            "reflection": _MAX_REFLECTION_ITEMS,
        }
        kind_counts: dict[str, int] = {}
        bounded_result: list[dict[str, str]] = []
        for item in result:
            k = item.get("kind", "")
            cap = kind_caps.get(k)
            if cap is not None:
                if kind_counts.get(k, 0) >= cap:
                    continue
                kind_counts[k] = kind_counts.get(k, 0) + 1
            bounded_result.append(item)
        result = bounded_result

        # #1090/#1108: suppress predicted doc-only items once the rolling
        # budget is reached.  Prediction calls the same classifier used for
        # integration-time counting, so the two decisions cannot drift.
        doc_count_24h = count_doc_only_integrations_24h(state_dir, now=now)
        doc_budget = doc_only_budget_24h()
        # #1175 Class-A: a blind ledger (permission, I/O error, nothing
        # readable) is not evidence that the budget is unspent. Fail toward
        # not spending LLM budget on doc-only work; the lane reopens on the
        # next cycle that can read its history. The shared 3-day window
        # answers "can the ledger be read" for the 24 h window too.
        ledger_blind = window_status(ledger_rows) == "unavailable"
        doc_budget_exceeded = doc_count_24h >= doc_budget or ledger_blind
        if ledger_blind:
            _LOG.warning(
                "doc-only budget: ledger window unavailable (%s); treating the budget as reached this cycle",
                ",".join(getattr(ledger_rows, "notes", ())) or "-",
            )
            notice = (
                f"Doc-only daily budget ({doc_budget}) treated as reached: the ledger could not be read. "
                "Focus on code-bearing improvements (scripts/, runtime/, tests/)."
            )
        else:
            notice = (
                f"Doc-only daily budget ({doc_budget}) reached ({doc_count_24h} in 24h). "
                "Focus on code-bearing improvements (scripts/, runtime/, tests/)."
            )

        post_doc_guard: list[dict[str, str]] = []
        doc_only_deferred = 0
        for item in result:
            k = item.get("kind", "")
            affected = item.get("affected_path", "")
            if doc_budget_exceeded and predict_item_change_tier(item) == "doc-only":
                # Deferral only: leave completion/exhaustion state untouched.
                doc_only_deferred += 1
                continue
            # Preserve #1090's reflection steering and extend the same bounded
            # notice to every surviving lane under the reached budget.
            if doc_budget_exceeded:
                item["doc_budget_notice"] = notice
                summary = item.get("summary", "")
                if notice not in summary:
                    item["summary"] = f"{summary} [STEERING NOTICE: {notice}]" if summary else notice
            if k == "reflection" and affected == "AGENTS.md":
                operator_owned_note = (
                    "[OPERATOR-OWNED TARGET: encode this as a skill under skills/ "
                    "or a lesson card, not as an instruction edit]"
                )
                summary = item.get("summary", "")
                if operator_owned_note not in summary:
                    item["summary"] = f"{summary} {operator_owned_note}".strip()
            if k == "reflection" and is_non_confirmable_target(affected):
                item["non_confirmable_target"] = "true"
                item["steering_only"] = "true"
                steering_note = (
                    "[STEERING ONLY: target cannot earn harness usage confirmation]"
                )
                summary = item.get("summary", "")
                if steering_note not in summary:
                    item["summary"] = f"{summary} {steering_note}".strip()
            post_doc_guard.append(item)
        # #1108: the denominator is what entered *this* guard, so it is read
        # before ``_apply_futile_surfaces`` runs — that filter drops items of its
        # own (#1184), and counting after it would silently charge those to the
        # doc-only budget's ledger row.
        items_considered = len(post_doc_guard) + doc_only_deferred
        result = _apply_futile_surfaces(state_dir, post_doc_guard)
        # #1238: one row per state change, per deferral and per invocation —
        # not per pass. ``passes`` says how many passes the row stands for.
        passes = _doc_budget_row_due(
            state_dir, (doc_budget_exceeded, ledger_blind, doc_only_deferred > 0)
        )
        if passes:
            from nanobot.runtime.cycle_ledger import append_event

            append_event(state_dir, {
                "phase": "doc_only_budget",
                "doc_only_deferred": doc_only_deferred,
                "doc_only_integrations_24h": doc_count_24h,
                "doc_only_budget_24h": doc_budget,
                "ledger_blind": ledger_blind,
                "doc_budget_exceeded": doc_budget_exceeded,
                "items_considered": items_considered,
                "passes": passes,
            })

        # #815: best-effort, operator-visible V1-vs-V2 split of what's
        # actually presented — never affects the returned list. Opt-in
        # only (see the ``emit_split`` docstring note) so the gate-probe
        # call doesn't double-count alongside the context-build call.
        if emit_split:
            _emit_vector_split_event(state_dir, result)
        return result
    except Exception:
        return []
