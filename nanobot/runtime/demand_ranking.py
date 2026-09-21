"""Rank the demand queue: rung gained, day urgency, measured cost (#1796, ADR-027).

**An input, never a gate** — the same clause as ADR-024/025/027 decision 6,
repeated here because this module is the one most likely to be read as a
licence to suppress. Every function scores and orders candidates; none of
them drop, refuse, defer, or filter one. The doc-only budget is the
standing argument against reading this any other way: it reported
"exceeded" all day while deferring nothing, and it pushed the monoculture
from documents into eight near-identical script additions a day. See
:func:`rank_candidates` and its own guarantee.

Three terms, each visible separately on the recorded score (AC: "a later
audit can say which term drove a choice"):

- **value** — rung gained (ADR-025's rung, read from the graph's own
  ``is_component``/``rung``, never restated here) plus a claimed reduced
  failure mode. Ordinal, per ADR-027 decision 1's table:
  connects-a-leaf > {extends-a-component, moves-the-deliverable,
  reduces-a-failure-mode} > new-artifact-with-a-named-consumer >
  new-artifact-without-one / a-document-nothing-routes-to.
- **urgency** — the day clock (:mod:`nanobot.runtime.day_clock`) and the
  deliverable stage. Harness-computed from ``now``/``state_dir`` alone;
  the model supplies neither input (AC: "cannot be set by the model").
- **size** — measured historical cost for the candidate's shape, read
  from recent ledger outcomes (:func:`shape_cost`). A shape with no
  history is marked ``"estimated"`` and falls back to the model's own
  self-reported size, never silently treated as measured (ADR-027
  decision 3; #1795 is the separate, later audit of whether these
  estimates are systematically wrong — not this module's job).

Provenance (ADR-027 decision 5, restated because getting it backwards is
costly in both directions): the size term is harness-measured and may be
shown to the loop as fact. The value term's rung half is graph-derived and
therefore, per ADR-023/ADR-025, may not be presented as fact — it reaches
ranking through this module and the demand pipeline, never the executor
prompt as a claim of truth. ``reduces_failure_mode`` and rung-at-proposal-
time are both **claims** made by the candidate, not measurements; see
:func:`record_score` for how a claim and a later measurement stay
distinguishable in the data rather than merging into one number.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Any

from nanobot.runtime import day_clock
from nanobot.runtime.artifact_graph import ArtifactGraph

#: ADR-027 decision 1's ordinal value tiers, highest first. A candidate's
#: value category is always exactly one of these -- never a blended or
#: interpolated number, so "which tier drove this" is legible without
#: re-deriving it from the raw fields.
VALUE_TIERS: tuple[str, ...] = (
    "connects_leaf",       # 5 -- highest: one cycle, one rung gained
    "extends_component",   # 4 -- the improvement propagates
    "moves_deliverable",   # 4 -- rises with the day clock, see urgency
    "reduces_failure_mode",  # 4 -- a claim; reconciled later, see record_score
    "new_with_consumer",   # 2 -- moderate; the claim is checked at integration
    "new_without_consumer",  # 1 -- lowest, by construction a new leaf
    "doc_no_route",        # 1 -- lowest
    "unknown",             # 1 -- cannot be classified; never higher than lowest
)
_VALUE_SCORE = {
    "connects_leaf": 5,
    "extends_component": 4,
    "moves_deliverable": 4,
    "reduces_failure_mode": 4,
    "new_with_consumer": 2,
    "new_without_consumer": 1,
    "doc_no_route": 1,
    "unknown": 1,
}

#: Extensions this module treats as "a document" for the
#: new-artifact-without-a-consumer vs. doc-nothing-routes-to distinction.
_DOC_EXTENSIONS = (".md",)

#: How many recent ledger outcome rows :func:`shape_cost` scans for a
#: shape's measured history. Bounded, matching this codebase's own
#: recency-window convention elsewhere (e.g. llm_proposer's
#: ``_RECENT_FAILED_WINDOW_CYCLES``) rather than an unbounded full-history
#: scan.
_COST_HISTORY_WINDOW_DAYS = 14


def classify_value(
    candidate: dict[str, Any], graph: ArtifactGraph | None,
) -> tuple[str, int]:
    """``(tier, score)`` for one candidate, per ADR-027 decision 1's table.

    ``graph`` is the artifact graph (ADR-025's rung, read via
    :meth:`ArtifactGraph.rung` — never re-derived here). ``None`` (the
    graph is unavailable) degrades every graph-dependent tier to
    ``"unknown"`` rather than guessing a rung the harness cannot currently
    support — fail-open toward the LOWEST tier, never toward a fabricated
    high one, since this score can never gate anything but an inflated
    score would still mislead the ordering.
    """
    if not isinstance(candidate, dict):
        return "unknown", _VALUE_SCORE["unknown"]

    if candidate.get("reduces_failure_mode"):
        return "reduces_failure_mode", _VALUE_SCORE["reduces_failure_mode"]
    if candidate.get("moves_deliverable"):
        return "moves_deliverable", _VALUE_SCORE["moves_deliverable"]

    target_path = str(candidate.get("target_path") or "").strip()
    if not target_path:
        return "unknown", _VALUE_SCORE["unknown"]

    node_id = None
    if graph is not None:
        for nid, node in graph.nodes.items():
            if node.path == target_path:
                node_id = nid
                break

    if node_id is not None and graph is not None:
        return (
            ("extends_component", _VALUE_SCORE["extends_component"])
            if graph.is_component(node_id)
            else ("connects_leaf", _VALUE_SCORE["connects_leaf"])
        )

    # target_path names an artifact the graph does not know about: a NEW
    # artifact. graph is None (unavailable) also falls here -- "unknown
    # whether it already exists" is treated the same as "does not exist
    # yet", the conservative (never a fabricated high tier) reading.
    if target_path.endswith(_DOC_EXTENSIONS):
        return "doc_no_route", _VALUE_SCORE["doc_no_route"]
    consumer = str(candidate.get("consumer") or candidate.get("named_consumer") or "").strip()
    if consumer:
        return "new_with_consumer", _VALUE_SCORE["new_with_consumer"]
    return "new_without_consumer", _VALUE_SCORE["new_without_consumer"]


@dataclass(frozen=True)
class Urgency:
    hours_to_deep_sleep: float
    deliverable_stage: str
    #: True only for value tiers the day clock actually accelerates
    #: (moves_deliverable) -- every other tier's urgency is flat, per
    #: ADR-027 decision 2: "urgency ... is flat for everything else."
    score: float


def compute_urgency(
    value_tier: str, *, now: datetime | None = None, state_dir: Path | None = None,
) -> Urgency:
    """Urgency from the day clock alone (ADR-027 decision 2).

    Takes no field from the candidate at all -- not the model's title, not
    a self-reported priority, nothing. This is the AC's own requirement
    ("a test proves it is harness-derived and cannot be set by the
    model"): the ONLY inputs are ``now`` and ``state_dir``, both supplied
    by the harness caller, never by the LLM reply.
    """
    position = day_clock.day_position(now)
    stage = day_clock.deliverable_stage()
    hours_left = position["hours_to_deep_sleep"]
    if value_tier != "moves_deliverable":
        return Urgency(hours_to_deep_sleep=hours_left, deliverable_stage=stage, score=0.0)
    # Rises as the day runs down -- 0 at hour 0, approaching 1 near deep
    # sleep. Deliberately simple (linear in hours remaining) rather than a
    # tuned curve: ADR-027 asks for "urgency rises... flat for everything
    # else", not a specific shape, and a simple function is auditable.
    elapsed_fraction = 1.0 - (hours_left / day_clock.DAY_HOURS if day_clock.DAY_HOURS else 0.0)
    return Urgency(
        hours_to_deep_sleep=hours_left, deliverable_stage=stage,
        score=round(max(0.0, min(1.0, elapsed_fraction)), 3),
    )


@dataclass(frozen=True)
class SizeEstimate:
    shape: str
    cost: float
    #: True when `cost` came from a self-report (no measured history for
    #: this shape yet) -- ADR-027 decision 3: "marked as an estimate ...
    #: replaced by measurement the first time that shape completes."
    estimated: bool


def _shape_of(candidate: dict[str, Any]) -> str:
    """A coarse task shape for cost lookup -- reuses the SAME
    ``change_shape`` taxonomy already recorded on ledger outcome rows
    (``feature``/``maintenance``/``documentation``/``testing``/
    ``performance``/``knowledge``/``unclassified``, see
    ``scripts.change_shape.classify_subject`` and
    ``cycle_ledger``'s own accepted set) rather than inventing a second,
    finer-grained taxonomy this module would then own alone."""
    shape = str(candidate.get("change_shape") or "").strip().lower() if isinstance(candidate, dict) else ""
    accepted = {"feature", "maintenance", "documentation", "testing", "performance", "knowledge"}
    return shape if shape in accepted else "unclassified"


def shape_cost(
    state_dir: Path, shape: str, *, now: datetime | None = None,
    window_days: int = _COST_HISTORY_WINDOW_DAYS,
) -> float | None:
    """Median wall-clock seconds (``started`` -> terminal ``outcome``) for
    recently-integrated cycles of this shape, or ``None`` with no history
    in the window (ADR-027 decision 3: "the denominator is the measured
    historical cost of that task shape"; #1795 is the separate audit of
    whether this measurement is systematically biased, not this
    function's job -- this only reads what already happened).

    Reads ``state_access.ledger_window`` directly (the shared, rotation-
    aware reader every other harness measurement in this codebase uses),
    joining a 'started' row to its terminal 'outcome' row by ``cycle_id``.
    Fail-open to ``None`` (no history, not a fabricated zero) on any
    read error.
    """
    try:
        from nanobot.runtime.state_access import ledger_window

        ref = now or datetime.now(tz=timezone.utc)
        since = ref - timedelta(days=window_days)
        window = ledger_window(
            Path(state_dir), since_ts=since.isoformat().replace("+00:00", "Z"), now=ref,
        )
        started_at: dict[str, datetime] = {}
        durations: list[float] = []
        for row in window.rows:
            cid = str(row.get("cycle_id") or "").strip()
            if not cid:
                continue
            phase = row.get("phase")
            if phase == "started":
                ts = _parse_ts(row.get("ts"))
                if ts is not None:
                    started_at[cid] = ts
            elif phase == "outcome":
                if str(row.get("change_shape") or "").strip().lower() != shape:
                    continue
                start = started_at.get(cid)
                end = _parse_ts(row.get("ts"))
                if start is not None and end is not None and end >= start:
                    durations.append((end - start).total_seconds())
        if not durations:
            return None
        durations.sort()
        mid = len(durations) // 2
        if len(durations) % 2:
            return durations[mid]
        return (durations[mid - 1] + durations[mid]) / 2.0
    except Exception:
        return None


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def compute_size(candidate: dict[str, Any], state_dir: Path, *, now: datetime | None = None) -> SizeEstimate:
    """The size term: measured history when it exists, else the model's
    own self-reported estimate, explicitly marked as such."""
    shape = _shape_of(candidate)
    measured = shape_cost(state_dir, shape, now=now)
    if measured is not None:
        return SizeEstimate(shape=shape, cost=measured, estimated=False)
    self_reported = candidate.get("estimated_size") if isinstance(candidate, dict) else None
    try:
        cost = float(self_reported) if self_reported is not None else 1.0
    except (TypeError, ValueError):
        cost = 1.0
    return SizeEstimate(shape=shape, cost=max(cost, 0.001), estimated=True)


#: ADR-031 target median increment size in lines (~190 lines on host history).
_BOX_TARGET_LINES = 190.0


def compute_increment_fit(candidate: dict[str, Any]) -> float:
    """Non-monotonic multiplier for increment size in numerator (#1851, ADR-031)."""
    if not isinstance(candidate, dict):
        return 1.0
    raw = candidate.get("estimated_lines")
    if raw is None:
        raw = candidate.get("lines")
    if raw is None:
        return 1.0
    try:
        lines = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if lines <= 0:
        return 0.1
    ratio = lines / _BOX_TARGET_LINES
    fit = math.exp(-0.5 * (math.log(ratio) / 1.2) ** 2)
    return round(max(0.1, min(1.0, fit)), 4)


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: dict[str, Any]
    value_tier: str
    value_score: int
    urgency: Urgency
    size: SizeEstimate
    increment_fit: float
    total: float
    rung_gained_claim: bool


def score_candidate(
    candidate: dict[str, Any],
    *,
    graph: ArtifactGraph | None,
    state_dir: Path,
    now: datetime | None = None,
) -> ScoredCandidate:
    """Score ONE candidate. Never raises, never refuses -- a malformed
    candidate scores ``"unknown"``/lowest rather than being dropped; see
    :func:`rank_candidates` for the guarantee that this extends to a
    whole list."""
    value_tier, value_score = classify_value(candidate, graph)
    urgency = compute_urgency(value_tier, now=now, state_dir=state_dir)
    size = compute_size(candidate, state_dir, now=now)
    fit = compute_increment_fit(candidate)
    total = ((value_score * fit) / size.cost) + urgency.score
    return ScoredCandidate(
        candidate=candidate,
        value_tier=value_tier,
        value_score=value_score,
        urgency=urgency,
        size=size,
        increment_fit=fit,
        total=round(total, 6),
        rung_gained_claim=value_tier in ("connects_leaf", "extends_component"),
    )


def rank_candidates(
    candidates: list[dict[str, Any]],
    *,
    graph: ArtifactGraph | None,
    state_dir: Path,
    now: datetime | None = None,
) -> list[ScoredCandidate]:
    """Score and order every candidate. **Never drops one.**

    The length of the returned list always equals the length of
    ``candidates`` — including a malformed entry (scores lowest, via
    :func:`score_candidate`'s own fail-open path) and including a
    duplicate. This is the AC's own required guarantee ("a test proves the
    ranking path cannot drop a candidate") and the reason this function
    has no early return, no ``continue``, and no exception handler that
    swallows an entry: every branch that could otherwise skip a candidate
    is a bug, not a feature, in this specific function.
    """
    scored = [
        score_candidate(c, graph=graph, state_dir=state_dir, now=now)
        for c in candidates
    ]
    scored.sort(key=lambda s: s.total, reverse=True)
    return scored


def record_score(scored: ScoredCandidate) -> dict[str, Any]:
    """The per-candidate score shape recorded on the ledger (AC: "a score
    is recorded per candidate with its three terms visible separately").

    ``rung_gained_claim`` is explicitly a claim, never merged into
    ``value_score`` as though it were a settled fact — a later reader
    reconciles it against the graph's own ``is_component`` (ADR-027
    decision 5's "claim at proposal time, a measurement weeks later");
    this function's only job is to keep the claim and its eventual
    measurement two separate, comparably-keyed fields rather than one
    number that has already forgotten which it was.
    """
    return {
        "value_tier": scored.value_tier,
        "value_score": scored.value_score,
        "rung_gained_claim": scored.rung_gained_claim,
        "urgency_score": scored.urgency.score,
        "hours_to_deep_sleep": scored.urgency.hours_to_deep_sleep,
        "deliverable_stage": scored.urgency.deliverable_stage,
        "size_shape": scored.size.shape,
        "size_cost": scored.size.cost,
        "size_estimated": scored.size.estimated,
        "increment_fit": scored.increment_fit,
        "total": scored.total,
    }
