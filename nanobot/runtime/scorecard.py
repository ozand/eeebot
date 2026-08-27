"""Deterministic instance scorecard — the loop's fitness function (#765).

The integration gate answers only "are tests green?"; nothing measured
whether a change created *value*. AIDE² (weco.ai, "First Evidence of
Recursive Self-Improvement") shows the missing mechanism: score-gated
acceptance against a measured objective is what turns a loop into an
optimizer. This module is the measurement substrate: a versioned,
deterministic (NO LLM call), fail-open snapshot of the instance's health,
computed from state the harness already writes:

- **loop** (goal Vector 1 — primary): counts over the last
  :data:`_WINDOW_DAYS` days of the cycle ledger — integrations
  (``outcome: success``), skips by class, proposer rejects, idle share
  (idle heartbeats over cycle-ish rows), and the repeat-failure rate
  (``recent_duplicate_failure`` skips + ``self_dedup`` rejects over
  proposals — the "learning from its own errors" measure). Rotation-aware:
  reads the current ``cycles.jsonl`` PLUS up to :data:`_MAX_GZ_FILES`
  newest ``cycles-*.jsonl.gz`` archives, because the midnight rotation
  blinds every single-file ledger reader (the #771/#772/#773 lesson).
  ``integrations`` is further split into ``confirmed_integrations`` vs
  ``unconfirmed_integrations`` (#814) by joining each success-outcome
  cycle_id against ``demand/completed.json``'s harness-confirmed set
  (same guard as ``value.confirmed_ratio``) — confirmation is POST-HOC,
  so this is an aggregate-window join, never a per-cycle gate.
  ``confirmed_integration_ratio`` is scoped to ``confirmable_integrations``
  (the non-decay successes touching a ``scripts/`` path — the only kind
  ``confirm_serves`` can ever confirm), NOT all integrations, so a
  runtime/docs/config change can never sit permanently unconfirmed and a
  decay-heavy window can never dilute it. See :data:`_TARGETS`.
- **cost** (Vector 1): from the #675 per-call telemetry
  (``<state_dir>/llm_calls/YYYY-MM-DD.jsonl``, last :data:`_WINDOW_DAYS`
  daily files — NOT the ``prompts/`` recordings): total calls, total
  tokens, and calls/tokens per integration (``None``-safe when 0
  integrations — never a fabricated 0).
- **quality** (Vector 1): instance-repo signals — script count,
  compile-clean count (reusing ``demand``'s HEAD-watermarked py_compile
  scan when importable, else a bounded own scan), test-file count.
- **value** (goal Vector 2 — secondary): from the #761 sidecars —
  completed-demand declared vs harness-confirmed counts, decay-candidate
  count, usage-tracked artifact count.
- **heldout** (Vector 1, #780): counts over the held-out verification
  pack's persisted results (``nanobot.runtime.heldout``, refreshed on this
  recompute path, HEAD+time-gated internally) — checked/passed/failed/
  skipped plus ``heldout_gap`` = failed / (passed+failed), ``None`` at
  denominator 0; skips never count against the instance.
- **integrity** (#789 — no target on purpose): count of ``phase:
  "integrity"`` ledger rows in the window (fitness-sidecar tamper repairs +
  spawn-boundary write detections), broken down by reason. ``value``'s
  confirmed count trusts ONLY harness-authored signals
  (``usage_evidence.HARNESS_SIGNALS``) — a foreign ``signal`` on a
  ``confirmed`` entry never counts (live reward-hack 2026-07-17).

The goal's FUTURE section (deferred creative work) deliberately maps to NO
metric and generates NO gap (regression-pinned in tests).

Snapshots persist to ``<state_dir>/scorecard/latest.json`` (overwritten)
plus one appended line in ``<state_dir>/scorecard/history.jsonl`` (bounded
read; no rotation needed yet). Recomputation is gated by a time watermark
(:data:`_RECOMPUTE_MINUTES` — ``system_map``'s no-op-gate pattern), so idle
cycles pay one small file read.

**Gap analysis → demand.** :data:`_TARGETS` is the declarative
metric→(direction, threshold, vector, rank) table derived from the ORDERED
goal vectors (#767): V1 targets dominate V2 targets. ``idle_share``
deliberately has NO target — an idle loop with no demand is healthy, not a
gap. Metrics violating their target become ``goal-gap`` demand items via
:func:`goal_gaps`, consumed by ``demand.collect_demand`` and ranked between
``defect`` and ``hypothesis``.

**Immutability (#603 invariant).** This computation, its targets, and its
sidecars live in the product runtime (``nanobot/``) and the harness-owned
``state_dir`` — entirely OUTSIDE the instance's mutable workspace
(``eeebot-self-evolving/``). The instance must never be able to redefine
its own fitness function (AIDE²'s public/private evaluation split makes
the same point); it can only *move* the metrics by doing real work.

Everything here is deterministic and fail-open: a missing/corrupt file, an
unreadable directory, or any unexpected exception degrades to zeros /
``None`` values / no gaps — never raises into the caller.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCORECARD_SCHEMA = "scorecard-v1"

# ─── control-plane visibility (#865) ─────────────────────────────────────────
#
# EXPLICIT allowlist of operator-facing runtime flags. NEVER dump os.environ
# wholesale here and NEVER add a secret-bearing key (API keys/tokens/secrets,
# remote sync URLs) — this snapshot is written to state (scorecard/latest.json
# + history.jsonl) and is meant to be read/shared freely. Every key listed is
# reported (value or ``None`` if unset) so a reader always sees the full
# policy surface, not just whatever happens to be set.
_CONTROL_PLANE_KEYS: tuple[str, ...] = (
    "SELFEVO_LLM_PROPOSER_ENABLED",
    "SELFEVO_GOAL_REVIEW_ENABLED",
    "SELFEVO_RUNTIME_SLICE",
    "SELFEVO_DECAY_PROTECT",
    "SELFEVO_BENCHMARK_TRUST",
    "SELFEVO_USAGE_REFERENCE_ENABLED",
    "SELFEVO_DEMAND_DRIVEN_ENABLED",
    "SELFEVO_EXISTENCE_INDEX_ENABLED",
    "SELFEVO_SURFACES_DIR",
    "SUBAGENT_BRIDGE_MODEL",
    "SELFEVO_PROPOSER_MODEL",
    "SUBAGENT_BRIDGE_REASONING_EFFORT",
    "SUBAGENT_BRIDGE_FORCE_BUDGET",
    "SUBAGENT_BRIDGE_ENABLED",
    "SUBAGENT_BRIDGE_FORCE_PROFILE",
    "SUBAGENT_BRIDGE_FAILURE_SUPPRESS_HOURS",
    "SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN",
    "SUBAGENT_BRIDGE_MAX_REVISIONS",
    # #906: operator preset visibility — which named profile (if any) is
    # active, and the two knobs it bundles (cycle cadence, iteration cap).
    "SELFEVO_PRESET",
    "SELFEVO_CYCLE_PAUSE",
    "SELFEVO_MAX_TOOL_ITERATIONS",
)


# ─── #875: root-verified runtime-slice promotions visibility ────────────────
#
# The root promotion verifier (host/eeepc/libexec/eeepc_promotion_verifier.py)
# writes its manifest + per-candidate state into PROMOTED_TREE (default
# /var/lib/eeepc-promoted), entirely outside state_dir. This gives operators
# a cheap read-only glance at that pipeline from the SAME scorecard they
# already check, without granting the instance any write access to it (the
# manifest/verifier_state files are root-owned; this is a read-only peek).
_PROMOTED_TREE_ENV = "PROMOTED_TREE"
_DEFAULT_PROMOTED_TREE = "/var/lib/eeepc-promoted"


def _runtime_promotions_snapshot() -> dict[str, Any]:
    """Cheap, fail-open, read-only counts of the #875 root-verification
    pipeline: ``active`` promotions (from PROMOTED_TREE/manifest.json) plus
    ``soaking``/``rejected`` counts (from PROMOTED_TREE/verifier_state.json,
    joined against STATE_DIR/promotions/promotion-runtime-*.json candidate
    ids). Never raises; missing files/dirs degrade to zeros — this section
    is visibility-only, same as the rest of control_plane.
    """
    counts: dict[str, Any] = {"active": 0, "soaking": 0, "rejected": 0}
    promoted_tree = Path(os.environ.get(_PROMOTED_TREE_ENV) or _DEFAULT_PROMOTED_TREE)
    try:
        manifest = _read_json(promoted_tree / "manifest.json", None)
        if isinstance(manifest, dict):
            counts["active"] = sum(
                1
                for key, entry in manifest.items()
                if not str(key).startswith("_") and isinstance(entry, dict) and entry.get("status") == "active"
            )
    except Exception:
        pass
    try:
        verifier_state = _read_json(promoted_tree / "verifier_state.json", None)
        candidates = verifier_state.get("candidates") if isinstance(verifier_state, dict) else None
        if isinstance(candidates, dict):
            counts["soaking"] = sum(
                1 for entry in candidates.values() if isinstance(entry, dict) and entry.get("status") == "soaking"
            )
            counts["rejected"] = sum(
                1 for entry in candidates.values() if isinstance(entry, dict) and entry.get("status") == "rejected"
            )
    except Exception:
        pass
    return counts


def _runtime_trust_ladder_snapshot() -> dict[str, Any]:
    """#876: read-only control-plane visibility into the derived trust ladder.

    scorecard runs as the eeepc-agent uid and can READ (never write) the
    root-owned PROMOTED_TREE manifest via
    ``promoted_overlay.active_promoted_modules`` — the exact same
    boundary-checked read the ladder logic itself trusts. Fail-open to a
    minimal/omitted structure on any import/read error — this section is
    visibility-only and must never crash the scorecard.
    """
    try:
        from nanobot.runtime.promoted_overlay import active_promoted_modules
        from nanobot.runtime.runtime_deny import (
            RUNTIME_TRUST_LADDER,
            earned_ladder_level,
            earned_ladder_slice,
        )

        active = active_promoted_modules()
        return {
            "level": earned_ladder_level(active),
            "unlocked": sorted(earned_ladder_slice(active)),
            "ladder": list(RUNTIME_TRUST_LADDER),
        }
    except Exception:
        return {}


def _evolution_tree_snapshot(state_dir: 'Path | None') -> dict[str, Any]:
    """#877: read-only counts from the evolution tree sidecar
    (``state/evolution/tree.json``) — ``nodes`` (count), ``current_sha``
    (short form, 12 chars), ``switches`` (count). Lazy import (this module
    stays a leaf dependency, no import-cycle risk) + fail-open to ``{}``
    on any error or when ``state_dir`` is unavailable — visibility only,
    never fed into fitness/targets/gaps.
    """
    if state_dir is None:
        return {}
    try:
        from nanobot.runtime.evolution_tree import read_tree

        tree = read_tree(state_dir)
        current = tree.get("current_sha")
        return {
            "nodes": len(tree.get("nodes") or {}),
            "current_sha": (current[:12] if current else None),
            "switches": len(tree.get("switches") or []),
        }
    except Exception:
        return {}


def _hypothesis_loop_snapshot(state_dir: 'Path | None') -> dict[str, Any]:
    """#878: ``{active, answered, supported, refuted, inconclusive}`` counts
    from ``hypotheses/lifecycle.json`` (via
    ``hypothesis_backlog.lifecycle_counts``) — visibility only, never fed
    into fitness/targets/gaps. Same leaf-dependency shape as
    ``_evolution_tree_snapshot`` above. Fail-open to ``{}`` on any error or
    when ``state_dir`` is unavailable."""
    if state_dir is None:
        return {}
    try:
        from nanobot.runtime import hypothesis_backlog

        return hypothesis_backlog.lifecycle_counts(state_dir)
    except Exception:
        return {}


def _tech_tree_snapshot(state_dir: 'Path | None') -> dict[str, Any]:
    """#879: read-only ``tech_tree.portfolio_snapshot`` at the time
    ``_control_plane_snapshot`` is assembled — BEFORE this cycle's
    ``record_gains``/``select_current_direction`` run (see
    ``compute_scorecard``, which overwrites ``control_plane.tech_tree``
    with the freshly-updated snapshot right before persisting). Kept here
    too so the key is never simply absent if the later fail-open update
    block errors out. Same leaf-dependency shape as
    ``_evolution_tree_snapshot``/``_hypothesis_loop_snapshot`` above.
    Fail-open to ``{}`` on any error or when ``state_dir`` is unavailable."""
    if state_dir is None:
        return {}
    try:
        from nanobot.runtime import tech_tree

        return tech_tree.portfolio_snapshot(state_dir)
    except Exception:
        return {}


def _models_snapshot() -> dict[str, str]:
    """#899: visibility-only map of each role -> its currently resolved
    model, via the centralized :func:`model_registry.resolve_model`. Model
    names only, no secrets. Fail-open to ``{}`` on any error (e.g. import
    failure) so a resolver bug can never break the scorecard."""
    try:
        from nanobot.runtime import model_registry

        return {role: model_registry.resolve_model(role) for role in model_registry.ROLES}
    except Exception:
        return {}


def _control_plane_snapshot(state_dir: 'Path | None' = None) -> dict[str, Any]:
    """Active operator env values at compute time — visibility only.

    Deliberately NOT fed into fitness/targets/gaps. Reads only the explicit
    :data:`_CONTROL_PLANE_KEYS` allowlist (never ``os.environ`` wholesale),
    so no secret-bearing env var can ever leak into this section. Fail-open
    per-key: a single lookup error never blocks the rest of the section.
    """
    snapshot: dict[str, Any] = {}
    snapshot["runtime_promotions"] = _runtime_promotions_snapshot()
    snapshot["runtime_trust_ladder"] = _runtime_trust_ladder_snapshot()
    snapshot["evolution_tree"] = _evolution_tree_snapshot(state_dir)
    snapshot["hypothesis_loop"] = _hypothesis_loop_snapshot(state_dir)
    snapshot["tech_tree"] = _tech_tree_snapshot(state_dir)
    snapshot["models"] = _models_snapshot()
    # #996: visibility-only futility state; never feeds metric computation.
    try:
        from nanobot.runtime import goal_gap_futility
        snapshot["goal_gap_futility"] = goal_gap_futility.futility_snapshot(state_dir)
    except Exception:
        snapshot["goal_gap_futility"] = {}
    for key in _CONTROL_PLANE_KEYS:
        try:
            snapshot[key] = os.environ.get(key)
        except Exception:
            snapshot[key] = None
    return snapshot

_WINDOW_DAYS = 7
_RECOMPUTE_MINUTES = 30
_MAX_GZ_FILES = 7  # bounded archive read — rotation-aware, never unbounded
_MAX_HISTORY_LINES = 400  # bounded history read (one line per recompute)
_DECAY_DAYS = 14  # kept in sync with demand._DECAY_DAYS

# Trend-gap parameters for tokens_per_integration: worsening more than
# _TREND_WORSEN_FACTOR vs the mean of the prior window is a gap.
_TREND_WORSEN_FACTOR = 1.5

# ─── targets: metric → (direction, threshold, vector, rank) ─────────────────
#
# Derived from the ORDERED goal vectors (goal_text, expanded by #767):
# V1 (PRIMARY) — cycle efficiency/quality, learning-from-errors, cost per
# integration; V2 (SECONDARY) — interface usage/utility evidence (#761).
# The FUTURE section maps to NOTHING here — it must generate no gap.
#
# Start conservative: only metrics with an obvious direction get a target.
# ``idle_share`` has NO target on purpose: an idle loop facing no demand is
# the honest no-op working (#760), not a deficiency to "fix".
#
# direction semantics:
#   "max"   — gap when value >  threshold
#   "min"   — gap when value <  threshold
#   "trend" — gap when value > _TREND_WORSEN_FACTOR * mean(prior window)
_TARGETS: dict[str, dict[str, Any]] = {
    "repeat_failure_rate": {
        "section": "loop",
        "direction": "max",
        "threshold": 0.35,
        "vector": "V1",
        "rank": 1,
    },
    "compile_clean_ratio": {
        "section": "quality",
        "direction": "min",
        "threshold": 0.95,
        "vector": "V1",
        "rank": 2,
    },
    "tokens_per_integration": {
        "section": "cost",
        "direction": "trend",
        "vector": "V1",
        "rank": 3,
    },
    "confirmed_ratio": {
        "section": "value",
        "direction": "min",
        "threshold": 0.5,
        # Only meaningful once enough completed entries exist to judge.
        "min_denominator": 3,
        "denominator_metric": "completed_declared",
        "vector": "V2",
        "rank": 4,
        # #808: this gap otherwise reads as "generic quality problem" to the
        # proposer, which then edits an irrelevant reporting script that
        # structurally cannot move the metric. Spell out the actual lever.
        "lever_hint": (
            "Rises only when a completed scripts/*.py artifact is actually "
            "used after completion (harness pycache/output signal via "
            "confirm_serves). Editing reporting/analysis scripts does NOT "
            "move it — propose work that produces a script the loop will "
            "exercise, or improves the confirm-serves usage evidence path."
        ),
    },
    # #780: fraction of held-out-checked artifacts whose behavioral check
    # fails (failed / (passed+failed); skips excluded — a checker problem is
    # never counted against the instance). None (no gap) at denominator 0.
    "heldout_gap": {
        "section": "heldout",
        "direction": "max",
        "threshold": 0.2,
        "vector": "V1",
        "rank": 5,
    },
    # #1036: count of stale data feeds (stale > 0 generates gap).
    # Needs at least one feed present on disk to judge; all-missing state
    # emits stale=0 (no feed gap) to prevent spurious gaps in fresh setups.
    "stale_feeds": {
        "section": "feeds",
        "metric": "stale",
        "direction": "max",
        "threshold": 0,
        "min_denominator": 1,
        "denominator_metric": "total",
        "vector": "V1",
        "rank": 6,
    },
    # #1034: confirmed_integration_ratio removed from _TARGETS and kept as
    # reporting-only metric until confirmed movement is proven in the numerator.
    # Re-promotion condition: re-promote to active goal-gap target once
    # confirmed_integrations > 0 consistently across rolling windows.
}


# ─── small shared helpers (same shapes as demand.py) ────────────────────────


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


def _ratio(numerator: float, denominator: float) -> float | None:
    """``None``-safe ratio — never a fabricated 0 when the denominator is 0."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


# ─── shared: harness-signal guard (#789, reused by #814) ────────────────────


def _harness_signals() -> frozenset[str]:
    """The set of ``signal`` values ``usage_evidence`` itself writes.
    Shared by ``_value_section`` (``confirmed_ratio``) and
    ``_confirmed_cycle_ids`` (the loop section's confirmed-vs-unconfirmed
    integration split, #814) so both trust ONLY harness-authored signals —
    a foreign ``signal`` on a ``confirmed`` entry must never move either
    metric (live reward-hack 2026-07-17)."""
    try:
        from nanobot.runtime import usage_evidence as _ue

        return _ue.HARNESS_SIGNALS
    except Exception:
        # #819: kept in sync with usage_evidence.HARNESS_SIGNALS — "benchmark"
        # joined that set when the benchmark-evidence gate became
        # harness-history-corroborated (verify_benchmark), so this fallback
        # (used only if the usage_evidence import itself fails) must match.
        # #838: "reference" joined too — the signal for a scripts/*.py
        # consumed via import or a committed service/timer/wrapper.
        return frozenset({"pycache", "output", "benchmark", "reference"})


def _confirmed_cycle_ids(state_dir: Path) -> set[str]:
    """``cycle_id``s of ``demand/completed.json`` entries that are
    confirmed-used under the same harness-signal guard as
    ``_value_section``'s ``confirmed_ratio`` (#814). Confirmation is
    POST-HOC — a cycle's artifact is confirmed later, when harness usage
    evidence arrives — so the loop section joins each success-outcome
    cycle against this set to tell confirmed integrations from
    unconfirmed (but not-yet-disproven) ones. Fail-open to an empty set."""
    out: set[str] = set()
    try:
        harness_signals = _harness_signals()
        completed = _read_json(Path(state_dir) / "demand" / "completed.json", None)
        entries = completed.get("entries") if isinstance(completed, dict) else None
        if isinstance(entries, dict):
            for entry in entries.values():
                if not isinstance(entry, dict):
                    continue
                if entry.get("confirmed") is True and str(entry.get("signal") or "") in harness_signals:
                    cycle_id = str(entry.get("cycle_id") or "").strip()
                    if cycle_id:
                        out.add(cycle_id)
    except Exception:
        pass
    return out


def _scorecard_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "scorecard"


def _latest_path(state_dir: Path) -> Path:
    return _scorecard_dir(state_dir) / "latest.json"


def _history_path(state_dir: Path) -> Path:
    return _scorecard_dir(state_dir) / "history.jsonl"


# ─── ledger reading (rotation-aware, bounded) ───────────────────────────────


def _ledger_rows(state_dir: Path, now: datetime) -> list[dict[str, Any]]:
    """In-window rows from ``ledger/cycles.jsonl`` PLUS up to
    :data:`_MAX_GZ_FILES` newest rotated ``cycles-*.jsonl.gz`` archives.

    Rotation blinds single-file readers (#773 lesson) — a 7-day window MUST
    read the archives too, bounded so a years-old ledger dir stays cheap.
    Fail-open per file: any unreadable file contributes no rows.
    """
    rows: list[dict[str, Any]] = []
    cutoff = now - timedelta(days=_WINDOW_DAYS)

    def _consume(lines: list[str]) -> None:
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
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts < cutoff:
                continue
            rows.append(row)

    try:
        ledger_dir = Path(state_dir) / "ledger"
        if not ledger_dir.is_dir():
            return rows
        active = ledger_dir / "cycles.jsonl"
        if active.is_file():
            try:
                _consume(active.read_text(encoding="utf-8").splitlines())
            except Exception:
                pass
        try:
            archives = sorted(ledger_dir.glob("cycles-*.jsonl.gz"), reverse=True)
        except Exception:
            archives = []
        for gz_path in archives[:_MAX_GZ_FILES]:
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                    _consume(fh.read().splitlines())
            except Exception:
                continue
        return rows
    except Exception:
        return rows


# ─── section: loop (V1) ─────────────────────────────────────────────────────


def _loop_section(
    rows: list[dict[str, Any]], confirmed_cycle_ids: set[str] | None = None
) -> dict[str, Any]:
    confirmed_cycle_ids = confirmed_cycle_ids or set()
    integrations = 0
    decay_integrations = 0
    # #814: confirmed vs unconfirmed split of `integrations` (decay archivals
    # excluded — they are churn, not new value, and are never the numerator
    # either split targets). Confirmation is POST-HOC (usage evidence arrives
    # after the cycle), so this joins each success-outcome cycle_id against
    # `confirmed_cycle_ids` (built from demand/completed.json, same
    # harness-signal guard as confirmed_ratio) rather than gating at cycle
    # time — the per-cycle scorer cannot know confirmation yet.
    confirmed_integrations = 0
    unconfirmed_integrations = 0
    # #814 follow-up (2 HIGH review findings): `confirm_serves` can only
    # EVER confirm a completed entry whose ledger-outcome `files_changed`
    # contains a `scripts/`-prefixed path (usage_evidence.confirm_serves).
    # An integration that only touches nanobot/runtime, docs, or config is
    # therefore structurally unconfirmable and must not sit in the
    # ratio's denominator forever (a permanent false gap) — and decay
    # archivals must not dilute it either (the #801/#802 principle). The
    # fitness ratio is scoped to this "confirmable" universe: non-decay
    # success cycles whose files_changed includes a scripts/ path.
    # #836: that alone still admitted a second permanently-unconfirmable
    # class — a success whose `proposed` row carried no `demand_id` is
    # never off-goal-tracked and therefore never folded into
    # completed.json, so confirm_serves can never reach it either. Being
    # goal-linked (demand_id present on the proposed row) is required in
    # addition to the scripts/ touch, symmetric with the decay exclusion
    # above. This does NOT reintroduce the #814 bug: a goal-linked success
    # that IS folded but not yet confirmed still counts here — only
    # unconfirmed status is reporting-only (see confirmed_integrations
    # below); goal-linkage is a foldability gate, not a confirmed-status
    # gate.
    confirmable_integrations = 0
    confirmed_confirmable_integrations = 0
    idle_rows = 0
    outcome_rows = 0
    proposals = 0
    proposer_rejects = 0
    self_dedup_rejects = 0
    duplicate_failure_skips = 0
    skips_by_class: dict[str, int] = {}
    # #800 churn split: cycles whose proposed row served a decay demand
    # (demand_id "decay-…", the #760 traceability field). A success outcome
    # for such a cycle is an archival — bookkeeping churn, not new value —
    # and the create→archive treadmill farmed one credit per archival. Those
    # successes count as decay_integrations, never as integrations.
    decay_cycles: set[str] = set()
    # #836: cycles whose `proposed` row carried a non-empty demand_id — i.e.
    # goal-linked work that IS foldable into completed.json and therefore
    # CAN eventually be confirmed. decay-* cycles also carry a demand_id but
    # are already excluded from the confirmable universe above as decay, so
    # membership in both sets is harmless (decay_cycles is checked first).
    goal_linked_cycles: set[str] = set()
    for row in rows:
        if row.get("phase") != "proposed":
            continue
        cycle_id = str(row.get("cycle_id") or "").strip()
        if not cycle_id:
            continue
        demand_id = str(row.get("demand_id") or "").strip()
        if demand_id.startswith("decay-"):
            decay_cycles.add(cycle_id)
        if demand_id:
            goal_linked_cycles.add(cycle_id)
    for row in rows:
        phase = row.get("phase")
        if phase == "idle":
            idle_rows += 1
        elif phase == "proposed":
            proposals += 1
        elif phase == "proposer_reject":
            proposer_rejects += 1
            if str(row.get("reason") or "").strip() == "self_dedup":
                self_dedup_rejects += 1
        elif phase == "outcome":
            outcome_rows += 1
            outcome = str(row.get("outcome") or "").strip().lower()
            if outcome == "success":
                cycle_id = str(row.get("cycle_id") or "").strip()
                if cycle_id in decay_cycles:
                    decay_integrations += 1
                else:
                    integrations += 1
                    is_confirmed = cycle_id in confirmed_cycle_ids
                    if is_confirmed:
                        confirmed_integrations += 1
                    else:
                        unconfirmed_integrations += 1
                    files_changed = row.get("files_changed")
                    is_confirmable = (
                        cycle_id in goal_linked_cycles
                        and isinstance(files_changed, list)
                        and any(isinstance(f, str) and f.startswith("scripts/") for f in files_changed)
                    )
                    if is_confirmable:
                        confirmable_integrations += 1
                        if is_confirmed:
                            confirmed_confirmable_integrations += 1
            elif outcome.startswith("skipped"):
                skips_by_class[outcome] = skips_by_class.get(outcome, 0) + 1
                # Only recent_duplicate_failure suppressions are repeat-failure
                # signals (module docstring). Healthy dedup skips
                # (already_done, already_done_tag, existence_index_duplicate)
                # are not failures and must not feed repeat_failure_rate.
                if str(row.get("reason") or "").strip() == "recent_duplicate_failure":
                    duplicate_failure_skips += 1
    cycleish = idle_rows + outcome_rows
    repeat_failures = duplicate_failure_skips + self_dedup_rejects
    attempts = proposals + proposer_rejects
    wasted_attempts = repeat_failures + proposer_rejects
    return {
        # Value-bearing integrations ONLY (#800) — the fitness numerator
        # consumed by the _TARGETS gap analysis. Archival churn is reported
        # separately; cost denominators use integrations_total (all work).
        "integrations": integrations,
        "decay_integrations": decay_integrations,
        "integrations_total": integrations + decay_integrations,
        # #814: confirmed-vs-unconfirmed split of `integrations` — surfaces
        # the shift so an operator (and the goal-gap analysis below) can see
        # that shipping unconfirmed-use churn is not the same as confirmed
        # value. Reporting-only counts: confirmed_integrations +
        # unconfirmed_integrations == integrations (decay excluded either
        # way). The _TARGETS-gated fitness ratio below is scoped narrower —
        # see confirmable_integrations.
        "confirmed_integrations": confirmed_integrations,
        "unconfirmed_integrations": unconfirmed_integrations,
        # Non-decay, goal-linked success cycles whose files_changed includes
        # a scripts/ path — the only integrations confirm_serves can ever
        # confirm. confirmed_integration_ratio's denominator, so a
        # runtime/docs/config integration (permanently unconfirmable) never
        # drags it down, a decay-heavy window never dilutes it (#814 review
        # fix), and — #836 — an off-goal success whose proposed row carried
        # no demand_id (never folded into completed.json, so confirm_serves
        # can never reach it) never drags it down either. A goal-linked
        # success that IS folded but not yet confirmed still counts here
        # (#814 semantics preserved) — the new requirement gates on
        # foldability, not on confirmed status.
        "confirmable_integrations": confirmable_integrations,
        "confirmed_integration_ratio": _ratio(confirmed_confirmable_integrations, confirmable_integrations),
        "skips_by_class": skips_by_class,
        "proposals": proposals,
        "proposer_rejects": proposer_rejects,
        "attempts": attempts,
        "wasted_attempts": wasted_attempts,
        "idle_rows": idle_rows,
        "cycleish_rows": cycleish,
        # idle is healthy (honest no-op, #760) — reported, never targeted.
        "idle_share": _ratio(idle_rows, cycleish),
        "repeat_failures": repeat_failures,
        "repeat_failure_rate": _ratio(repeat_failures, attempts),
        "wasted_attempt_rate": _ratio(wasted_attempts, attempts),
    }


# ─── section: cost (V1, #675 telemetry) ─────────────────────────────────────


def _cost_section(state_dir: Path, now: datetime, integrations: int) -> dict[str, Any]:
    """Totals from the daily ``llm_calls/YYYY-MM-DD.jsonl`` telemetry files
    (last :data:`_WINDOW_DAYS` days — a bounded, named set of files; the
    ``prompts/`` recordings are deliberately NOT read)."""
    calls = 0
    tokens = 0
    try:
        calls_dir = Path(state_dir) / "llm_calls"
        for delta in range(_WINDOW_DAYS):
            day = (now - timedelta(days=delta)).strftime("%Y-%m-%d")
            path = calls_dir / f"{day}.jsonl"
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
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
                calls += 1
                try:
                    tokens += int(row.get("prompt_tokens") or 0) + int(
                        row.get("completion_tokens") or 0
                    )
                except Exception:
                    pass
    except Exception:
        pass
    return {
        "llm_calls": calls,
        "total_tokens": tokens,
        "calls_per_integration": _ratio(calls, integrations),
        "tokens_per_integration": _ratio(tokens, integrations),
    }


# ─── section: quality (V1, instance repo) ───────────────────────────────────

_SCRIPT_DIRS = ("scripts", "surfaces")  # mirrors demand._SCRIPT_DIRS


def _own_compile_failures(repo: Path) -> list[str]:
    """Bounded fallback compile scan (same shape as ``demand._compile_defects``
    minus the watermark) used only when demand isn't cleanly importable."""
    failures: list[str] = []
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
                compile(py_path.read_text(encoding="utf-8", errors="replace"), str(py_path), "exec")
            except SyntaxError:
                failures.append(py_path.name)
            except Exception:
                continue
    return failures


def _quality_section(state_dir: Path, selfevo_repo: Path | None) -> dict[str, Any]:
    script_count = 0
    test_file_count = 0
    failing = 0
    if not selfevo_repo:
        return {
            "script_count": 0,
            "compile_failing": 0,
            "compile_clean": 0,
            "compile_clean_ratio": None,
            "test_file_count": 0,
        }
    try:
        repo = Path(selfevo_repo)
        for dirname in _SCRIPT_DIRS:
            d = repo / dirname
            if not d.is_dir():
                continue
            try:
                names = [p.name for p in d.glob("*.py")]
            except Exception:
                continue
            script_count += len(names)
            test_file_count += sum(1 for n in names if n.startswith("test_"))
        tests_dir = repo / "tests"
        if tests_dir.is_dir():
            try:
                test_file_count += sum(1 for _ in tests_dir.glob("*.py"))
            except Exception:
                pass

        # Reuse demand's HEAD-watermarked py_compile scan when importable —
        # one scan, one sidecar; fall back to a bounded own scan otherwise
        # (demand's scan is HEAD-gated, so a non-git repo needs the fallback).
        try:
            from nanobot.runtime import demand as _demand

            head = _demand._git_head(repo)
            if head is not None:
                failing = len(_demand._compile_defects(Path(state_dir), repo, head))
            else:
                failing = len(_own_compile_failures(repo))
        except Exception:
            failing = len(_own_compile_failures(repo))
    except Exception:
        pass
    clean = max(script_count - failing, 0)
    return {
        "script_count": script_count,
        "compile_failing": failing,
        "compile_clean": clean,
        "compile_clean_ratio": _ratio(clean, script_count),
        "test_file_count": test_file_count,
    }


# ─── section: value (V2, #761 sidecars) ─────────────────────────────────────


def _value_section(state_dir: Path, selfevo_repo: Path | None, now: datetime) -> dict[str, Any]:
    declared = confirmed = 0
    # #789 defense in depth: a `confirmed` entry counts ONLY when its signal
    # is one usage_evidence itself writes (HARNESS_SIGNALS) — a foreign
    # signal means non-harness code wrote the fitness input (live
    # reward-hack 2026-07-17) and must not move confirmed_ratio, even
    # before confirm_serves' repair pass has run.
    harness_signals = _harness_signals()
    completed = _read_json(Path(state_dir) / "demand" / "completed.json", None)
    entries = completed.get("entries") if isinstance(completed, dict) else None
    cutoff = now - timedelta(days=_WINDOW_DAYS)
    if isinstance(entries, dict):
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            # #1034: Window completed entries by their completion timestamp `ts`
            # (same _WINDOW_DAYS cutoff as the rest of scorecard metrics).
            # Entries lacking a parseable ts fall back to unwindowed inclusion.
            ts = _parse_ts(entry.get("ts"))
            if ts is not None and ts < cutoff:
                continue
            declared += 1
            if entry.get("confirmed") is True and str(entry.get("signal") or "") in harness_signals:
                confirmed += 1

    usage = _read_json(Path(state_dir) / "usage" / "last_used.json", None)
    usage_entries = usage.get("entries") if isinstance(usage, dict) else None
    usage_tracked = len(usage_entries) if isinstance(usage_entries, dict) else 0

    decay_candidates = 0
    try:
        from nanobot.runtime import usage_evidence

        decay_candidates = len(
            usage_evidence.stale_artifacts(
                Path(state_dir), selfevo_repo, older_than_days=_DECAY_DAYS, now=now
            )
        )
    except Exception:
        pass
    return {
        "completed_declared": declared,
        "completed_confirmed": confirmed,
        "confirmed_ratio": _ratio(confirmed, declared),
        "decay_candidates": decay_candidates,
        "usage_tracked": usage_tracked,
    }


# ─── #789: fitness-input sidecars (spawn-boundary hash set) ─────────────────
#
# The fitness-input sidecars (state_dir-relative) the instance must never
# write. The bridge hashes them immediately before the subagent spawn and
# re-hashes right before the gate verdict (#789 spawn-boundary tamper
# detection). Defined HERE — the fitness module, per the #603 placement rule
# — so instance-facing sources never spell out the protected set.
#
# #819: "scorecard/history.jsonl" joined this set — it is the trust root
# benchmark_evidence.verify_benchmark corroborates a claimed optimization
# against. Without it here, an instance could append fabricated
# ever-improving snapshots to history.jsonl at runtime and the spawn-boundary
# hash check would never notice (the file wasn't in the protected set), so
# verify_benchmark would happily "corroborate" a forged claim against
# forged history — a full bypass of the whole #819 guarantee. Timing is
# safe: history.jsonl is appended by the SAME compute_scorecard call that
# overwrites latest.json (see the `_write_json(_latest_path(...))` followed
# immediately by the history.jsonl append below in this module), so it
# changes exactly when the already-protected latest.json does — no new
# window relative to the existing spawn-boundary check. Note (same as every
# other sidecar here): #789 only DETECTS a spawn-boundary write on this
# sidecar and records it (`phase: "integrity"`); it does not roll the file
# back — detection, not prevention.
FITNESS_SIDECARS = (
    "demand/completed.json",
    "demand/exhausted.json",
    "scorecard/latest.json",
    "scorecard/history.jsonl",
    "heldout/results.json",
    "heldout/microbench.json",
    "usage/last_used.json",
    # #877: the evolution tree steers which sha the bridge branches a
    # cycle from (see evolution_tree.py's module docstring for the full
    # trust argument) — tampering with it is DETECTED here the same way
    # any other fitness-adjacent sidecar is, even though it is a steering
    # input, not a verification one.
    "evolution/tree.json",
    # #879: the tech-tree portfolio steers WHICH improvement direction the
    # loop prefers next (see tech_tree.py's module docstring for the full
    # trust argument) — same steering-not-verification tier as
    # evolution/tree.json above. A directly-edited gain_history/
    # last_lever_value CAN persist here for up to GAIN_HISTORY_MAX cycles
    # (until the next record_gains call overwrites last_lever_value) — this
    # is a SOFT RANKING bias only, never a gate/verifier bypass and never
    # starvation of another direction; tampering is DETECTED (never
    # prevented) via this sidecar's FITNESS_SIDECARS membership, the same
    # spawn-boundary #789 integrity check every other sidecar here gets.
    "tech_tree/portfolio.json",
    # #939: skill-read fitness sidecar — records which SKILL.md files the
    # subagent actually read during its cycle.  Tamper detection here is
    # important: an instance that appends false skill reads to claim unearned
    # fitness credit would bypass the birth-use guard without actually reading
    # any skill.  Same spawn-boundary detection tier as the other sidecars.
    "skill_fitness/reads.json",
)


def fitness_sidecar_hashes(state_dir: Path) -> dict[str, str]:
    """sha256 (hex) of each fitness sidecar's bytes; a missing file hashes to
    the sentinel ``"absent"`` (so create/delete both count as a change).
    Fail-open per file: an unreadable file also reads as ``"absent"``."""
    import hashlib

    out: dict[str, str] = {}
    for rel in FITNESS_SIDECARS:
        try:
            out[rel] = hashlib.sha256((Path(state_dir) / rel).read_bytes()).hexdigest()
        except Exception:
            out[rel] = "absent"
    return out


# ─── section: integrity (#789 — fitness-input tamper incidents) ─────────────


_FEED_STALE_SECS_DEFAULT = 12 * 3600

# (name, rel_path, is_dir, field, max_age_seconds)
_FEEDS: tuple[tuple[str, str, bool, str | None, int], ...] = (
    ("usage", "usage/last_used.json", False, "scanned_at_utc", 12 * 3600),
    ("heldout", "heldout/results.json", False, "checked_at_utc", 12 * 3600),
    ("llm_calls", "llm_calls", True, None, 24 * 3600),
    ("host_metrics", "host_metrics", True, None, 24 * 3600),
    (
        "validator_harness_parent",
        "validator_harness_parent/runs.jsonl",
        False,
        "checked_at_utc",
        12 * 3600,
    ),
)


def _feed_latest_timestamp(
    feed_path: Path, is_dir: bool, field: str | None
) -> tuple[datetime | None, str]:
    if not feed_path.exists():
        return None, "missing"

    if is_dir:
        newest_mtime: float | None = None
        try:
            for root, _dirs, files in os.walk(feed_path):
                for fname in files:
                    fpath = Path(root) / fname
                    try:
                        mt = fpath.stat().st_mtime
                        if newest_mtime is None or mt > newest_mtime:
                            newest_mtime = mt
                    except OSError:
                        continue
        except OSError:
            pass
        if newest_mtime is None:
            return None, "empty_dir"
        return datetime.fromtimestamp(newest_mtime, tz=timezone.utc), "dir_mtime"

    # Single-file feed: attempt to parse JSON/JSONL field if specified
    if field:
        if feed_path.suffix == ".jsonl":
            try:
                last_line = ""
                with open(feed_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line_str = line.strip()
                        if line_str:
                            last_line = line_str
                if last_line:
                    obj = json.loads(last_line)
                    if isinstance(obj, dict):
                        ts = _parse_ts(obj.get(field))
                        if ts:
                            return ts, "stamp"
            except Exception:
                pass
        else:
            try:
                with open(feed_path, "r", encoding="utf-8", errors="ignore") as f:
                    obj = json.load(f)
                if isinstance(obj, dict):
                    ts = _parse_ts(obj.get(field))
                    if ts:
                        return ts, "stamp"
            except Exception:
                pass

    # Fallback to file mtime
    try:
        mt = feed_path.stat().st_mtime
        return datetime.fromtimestamp(mt, tz=timezone.utc), "mtime"
    except OSError:
        return None, "error"


def _feeds_section(state_dir: Path | None, now_utc: datetime | None = None) -> dict[str, Any]:
    """#1036: Feed freshness watch. Emits stale count, stale names, and detail map."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    stale_names: list[str] = []
    feed_details: dict[str, Any] = {}

    if state_dir is None:
        return {
            "stale": 0,
            "stale_names": [],
            "total": 0,
            "feeds": {},
        }

    # Initialization guard (#1036): fail-open for uninitialized state
    # directories. An external feed is considered populated if it exists as
    # a non-empty file or non-empty directory.
    def _is_populated(rel_path: str, is_dir: bool) -> bool:
        p = state_dir / rel_path
        if is_dir:
            return p.is_dir() and any(p.iterdir())
        return p.is_file() and p.stat().st_size > 0

    initialized = any(
        _is_populated(rel_path, is_dir)
        for name, rel_path, is_dir, _, _ in _FEEDS
        if name not in ("heldout", "usage")
    )
    if not initialized:
        for name, _rel_path, _is_dir, _field, max_age_s in _FEEDS:
            feed_details[name] = {
                "status": "missing",
                "source": "missing",
                "stale": False,
                "latest_ts": None,
                "age_seconds": None,
                "max_age_seconds": max_age_s,
            }
        return {
            "stale": 0,
            "stale_names": [],
            "total": 0,
            "feeds": feed_details,
        }

    for name, rel_path, is_dir, field, max_age_s in _FEEDS:
        feed_path = state_dir / rel_path
        ts, source = _feed_latest_timestamp(feed_path, is_dir, field)
        if ts is None:
            stale_names.append(name)
            feed_details[name] = {
                "status": "missing",
                "source": source,
                "stale": True,
                "latest_ts": None,
                "age_seconds": None,
                "max_age_seconds": max_age_s,
            }
            continue

        age_s = (now_utc - ts).total_seconds()
        if age_s < 0:
            age_s = 0.0

        if age_s > max_age_s:
            stale_names.append(name)
            status = "stale"
        else:
            status = "fresh"

        feed_details[name] = {
            "status": status,
            "source": source,
            "stale": (status != "fresh"),
            "latest_ts": _iso(ts),
            "age_seconds": round(age_s, 1),
            "max_age_seconds": max_age_s,
        }

    return {
        "stale": len(stale_names),
        "stale_names": stale_names,
        "total": len(_FEEDS),
        "feeds": feed_details,
    }





def _integrity_section(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count of ``phase: "integrity"`` ledger rows in the window (written by
    ``usage_evidence.confirm_serves`` on a sidecar-tamper repair and by the
    bridge's spawn-boundary hash check, #789). Deliberately its OWN snapshot
    key, not part of ``value``: incidents are trust telemetry about the
    fitness inputs, not a fitness metric — and they must never get a
    ``_TARGETS`` entry (an "optimize incidents to zero" target would invite
    suppressing detection rather than fixing the writer)."""
    incidents = 0
    by_reason: dict[str, int] = {}
    for row in rows:
        if row.get("phase") != "integrity":
            continue
        incidents += 1
        reason = str(row.get("reason") or "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {"incidents": incidents, "by_reason": by_reason}


# ─── section: heldout (V1, #780 held-out verification pack) ─────────────────


def _heldout_section(state_dir: Path) -> dict[str, Any]:
    """Counts over the persisted held-out results
    (``<state_dir>/heldout/results.json``, written by
    ``nanobot.runtime.heldout.run_heldout``). ``heldout_gap`` =
    failed / (passed + failed) — skips are excluded from the denominator (a
    checker timeout/bug must never count against the instance).
    ``heldout_regressions`` (#841) is the count of the persisted
    ``regressions`` list — artifacts that were ``pass`` in the previous
    held-out run and are ``fail`` in this one, i.e. "something that used
    to pass now fails," surfaced separately from the raw pass/fail counts.
    Fail-open: missing/corrupt results read as zeros with a ``None`` gap
    (no gap fabricated from missing data)."""
    checked = passed = failed = skipped = 0
    regressions = 0
    data = None
    try:
        data = _read_json(Path(state_dir) / "heldout" / "results.json", None)
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, dict):
            for entry in results.values():
                if not isinstance(entry, dict):
                    continue
                status = str(entry.get("status") or "")
                if status not in ("pass", "fail", "skip"):
                    continue
                checked += 1
                if status == "pass":
                    passed += 1
                elif status == "fail":
                    failed += 1
                else:
                    skipped += 1
    except Exception:
        pass
    try:
        regs = data.get("regressions") if isinstance(data, dict) else None
        if isinstance(regs, list):
            regressions = len(regs)
    except Exception:
        regressions = 0
    return {
        "checked": checked,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "heldout_gap": _ratio(failed, passed + failed),
        "heldout_regressions": regressions,
    }


# ─── history ────────────────────────────────────────────────────────────────


def _read_history(state_dir: Path) -> list[dict[str, Any]]:
    """Bounded read of ``history.jsonl`` — the newest
    :data:`_MAX_HISTORY_LINES` lines only. Fail-open to ``[]``."""
    out: list[dict[str, Any]] = []
    try:
        path = _history_path(state_dir)
        if not path.is_file():
            return out
        lines = path.read_text(encoding="utf-8").splitlines()[-_MAX_HISTORY_LINES:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out
    except Exception:
        return out


def _metric_value(snapshot: dict[str, Any], section: str, metric: str) -> Any:
    sec = snapshot.get(section)
    if not isinstance(sec, dict):
        return None
    return sec.get(metric)


# ─── gap analysis ───────────────────────────────────────────────────────────


def _compute_gaps(
    snapshot: dict[str, Any], history: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Metrics violating their :data:`_TARGETS` entry, ordered V1 before V2
    (then by rank). A ``None`` current value never gaps (fail-open toward
    silence — never a gap fabricated from missing data). ``idle_share`` has
    no target and structurally cannot gap. Nothing maps to the goal's
    FUTURE section."""
    gaps: list[dict[str, Any]] = []
    for metric, spec in _TARGETS.items():
        try:
            metric_key = spec.get("metric", metric)
            current = _metric_value(snapshot, spec["section"], metric_key)
            if current is None or not isinstance(current, (int, float)):
                continue
            direction = spec["direction"]
            if direction == "trend":
                gap = _trend_gap(metric, spec, float(current), history, now)
                if gap is not None:
                    gaps.append(gap)
                continue
            min_den = spec.get("min_denominator")
            if min_den is not None:
                den = _metric_value(snapshot, spec["section"], spec["denominator_metric"])
                if not isinstance(den, (int, float)) or den < min_den:
                    continue  # not enough data to judge — no gap
            threshold = float(spec["threshold"])
            if current == 0 and direction == "max" and threshold == 0:
                continue
            breached = (direction == "max" and current > threshold) or (
                direction == "min" and current < threshold
            )
            if not breached:
                continue
            rel = "above max" if direction == "max" else "below min"
            gap_dict: dict[str, Any] = {
                "metric": metric,
                "vector": spec["vector"],
                "current": round(float(current), 4),
                "target": threshold,
                "evidence": (
                    f"{metric}={round(float(current), 4)} is {rel} target "
                    f"{threshold} over the last {_WINDOW_DAYS}d window "
                    f"(goal vector {spec['vector']})"
                ),
            }
            lever_hint = spec.get("lever_hint")
            if not lever_hint and metric == "stale_feeds":
                sec = snapshot.get(spec["section"], {})
                stale_names = sec.get("stale_names", []) if isinstance(sec, dict) else []
                if stale_names:
                    dead = ", ".join(stale_names)
                    lever_hint = f"Fix or restore stale data feeds: {dead}"
            if lever_hint:
                gap_dict["lever_hint"] = lever_hint
            gaps.append(gap_dict)
        except Exception:
            continue
    gaps.sort(key=lambda g: (0 if g["vector"] == "V1" else 1, _TARGETS.get(g["metric"], {}).get("rank", 99)))
    return gaps


def _trend_gap(
    metric: str,
    spec: dict[str, Any],
    current: float,
    history: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Trend target: gap when ``current`` worsened more than
    :data:`_TREND_WORSEN_FACTOR` vs the mean of the metric over the PRIOR
    7-day window of history entries (entries computed between 14 and 7 days
    ago). Needs at least 2 usable history entries in that prior window —
    skip otherwise (too little history to call a trend)."""
    try:
        if len(history) < 2:
            return None
        window_start = now - timedelta(days=2 * _WINDOW_DAYS)
        window_end = now - timedelta(days=_WINDOW_DAYS)
        prior_values: list[float] = []
        for entry in history:
            ts = _parse_ts(entry.get("computed_at_utc"))
            if ts is None or ts < window_start or ts >= window_end:
                continue
            value = _metric_value(entry, spec["section"], metric)
            if isinstance(value, (int, float)):
                prior_values.append(float(value))
        if len(prior_values) < 2:
            return None
        mean = sum(prior_values) / len(prior_values)
        if mean <= 0:
            return None
        limit = mean * _TREND_WORSEN_FACTOR
        if current <= limit:
            return None
        return {
            "metric": metric,
            "vector": spec["vector"],
            "current": round(current, 4),
            "target": round(limit, 4),
            "evidence": (
                f"{metric}={round(current, 4)} worsened >"
                f"{int((_TREND_WORSEN_FACTOR - 1) * 100)}% vs prior-window mean "
                f"{round(mean, 4)} ({len(prior_values)} entries, "
                f"{2 * _WINDOW_DAYS}..{_WINDOW_DAYS}d ago; goal vector {spec['vector']})"
            ),
        }
    except Exception:
        return None


# ─── public entrypoints ─────────────────────────────────────────────────────


def compute_scorecard(
    state_dir: Path,
    selfevo_repo: Path | None,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Compute (or watermark-return) the instance scorecard snapshot.

    Time watermark (``system_map`` no-op-gate pattern): if the persisted
    ``latest.json`` was computed less than :data:`_RECOMPUTE_MINUTES`
    minutes ago, it is returned as-is — one small file read, zero scanning.
    Otherwise a full recompute runs, ``latest.json`` is overwritten and one
    line is appended to ``history.jsonl``. Deterministic, NO LLM call,
    fail-open: any error degrades to a zeros/``None`` snapshot, never
    raises. The computation and its sidecars live in the product runtime
    and ``state_dir`` — outside the instance's mutable workspace, so the
    instance can never redefine its own fitness (#603 invariant).
    """
    try:
        state_dir = Path(state_dir)
        now = now or datetime.now(timezone.utc)

        latest = _read_json(_latest_path(state_dir), None)
        if not force and isinstance(latest, dict) and latest.get("schema_version") == SCORECARD_SCHEMA:
            computed_at = _parse_ts(latest.get("computed_at_utc"))
            if computed_at is not None and timedelta() <= (now - computed_at) < timedelta(
                minutes=_RECOMPUTE_MINUTES
            ):
                return latest  # watermark no-op — idle cycles stay cheap

        # #780: refresh the held-out results on the recompute path (already
        # watermark-gated here AND HEAD+time-gated inside run_heldout).
        # Wrapped fail-open on its own — a heldout bug must never break the
        # scorecard or demand collection.
        try:
            from nanobot.runtime import heldout as _heldout

            _heldout.run_heldout(state_dir, selfevo_repo, now=now)
        except Exception:
            pass

        rows = _ledger_rows(state_dir, now)
        loop = _loop_section(rows, _confirmed_cycle_ids(state_dir))
        snapshot: dict[str, Any] = {
            "schema_version": SCORECARD_SCHEMA,
            "computed_at_utc": _iso(now),
            "window_days": _WINDOW_DAYS,
            "loop": loop,
            # #800: cost per integration reflects ALL work (decay archivals
            # included) — the denominator is integrations_total, explicitly,
            # while the fitness numerator loop["integrations"] excludes them.
            "cost": _cost_section(state_dir, now, loop["integrations_total"]),
            "quality": _quality_section(state_dir, selfevo_repo),
            "value": _value_section(state_dir, selfevo_repo, now),
            "heldout": _heldout_section(state_dir),
            "integrity": _integrity_section(rows),
            "feeds": _feeds_section(state_dir, now),
            # #865: visibility-only snapshot of active operator flags — never
            # fed into fitness/targets/gaps below.
            "control_plane": _control_plane_snapshot(state_dir),
        }
        # Gap analysis runs against the PRE-append history so the trend
        # window never compares the snapshot against itself.
        history = _read_history(state_dir)
        snapshot["gaps"] = _compute_gaps(snapshot, history, now)

        # #879: tech-tree of improvement directions — a RANKING INPUT to
        # demand/goal-review (mirrors the #815 soft vector bias), never a
        # scheduler. Runs against THIS snapshot (the harness-computed
        # result, never an instance claim) and updates the portfolio
        # sidecar; the control-plane key is overwritten with the
        # POST-update snapshot so a reader sees this cycle's fresh
        # current-direction pick, not the stale pre-update one
        # ``_control_plane_snapshot`` captured above. Wrapped fail-open as
        # ONE block, separate from the module's own per-function fail-open
        # guards: a tech_tree bug must never break the scorecard or lose
        # the sections already computed above.
        try:
            from nanobot.runtime import hypothesis_backlog as _hyp_backlog
            from nanobot.runtime import tech_tree

            tech_tree.ensure_seeded(state_dir)
            tech_tree.record_gains(state_dir, snapshot)
            tech_tree.maybe_mint_node(state_dir, _hyp_backlog.supported_hypotheses(state_dir))
            tech_tree.select_current_direction(state_dir)
            snapshot["control_plane"]["tech_tree"] = tech_tree.portfolio_snapshot(state_dir)
        except Exception:
            pass

        _write_json(_latest_path(state_dir), snapshot)
        try:
            path = _history_path(state_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # #781: refresh the static loop-explorer page on the same recompute
        # cadence (update_explorer is itself watermark-gated on ledger
        # size/mtime + 30 min). Wrapped fail-open on its own — a rendering
        # bug must never break the scorecard or demand collection.
        try:
            from nanobot.runtime import loop_explorer as _loop_explorer

            _loop_explorer.update_explorer(state_dir, now=now)
        except Exception:
            pass

        # #768: the periodic goal-review rides the same recompute cadence
        # (itself hard-gated by SELFEVO_GOAL_REVIEW_ENABLED, default OFF,
        # plus its own daily watermark — a no-op in the common case). Runs
        # AFTER latest.json is written so the review reads THIS snapshot.
        # Wrapped fail-open on its own — a review bug must never break the
        # scorecard or demand collection.
        try:
            from nanobot.runtime import goal_review as _goal_review

            _goal_review.maybe_goal_review(state_dir, selfevo_repo, now=now)
        except Exception:
            pass
        return snapshot
    except Exception:
        return {
            "schema_version": SCORECARD_SCHEMA,
            "computed_at_utc": _iso(now or datetime.now(timezone.utc)),
            "window_days": _WINDOW_DAYS,
            "loop": {},
            "cost": {},
            "quality": {},
            "value": {},
            "heldout": {},
            "integrity": {},
            "gaps": [],
            "control_plane": _control_plane_snapshot(state_dir),
        }


def goal_gaps(state_dir: Path, selfevo_repo: Path | None) -> list[dict[str, Any]]:
    """Current goal-gap list — metrics violating their :data:`_TARGETS`
    entry, ordered V1 before V2. Rides :func:`compute_scorecard`'s
    watermark (cheap when fresh). Fail-open to ``[]``."""
    try:
        snapshot = compute_scorecard(state_dir, selfevo_repo)
        gaps = snapshot.get("gaps")
        if not isinstance(gaps, list):
            return []
        return [g for g in gaps if isinstance(g, dict) and g.get("metric")]
    except Exception:
        return []
