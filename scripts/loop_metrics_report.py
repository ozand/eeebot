#!/usr/bin/env python3
"""
loop_metrics_report.py — read-only #705 metrics report over the #720 cycle ledger.

Issue #710 implements the report *script* against two design-only contracts:
``docs/changes/705-observability-metrics/metrics.md`` (the nine metric
definitions) and ``report-spec.md`` (the JSON/table output shapes). #705 was
written before #720 landed and assumed a done/failure-ledger split
(``docs/changes/704-ledger-artifact-memory/design.md``) that #720 deliberately
did not build; #720's ``nanobot.runtime.cycle_ledger`` instead appends every
phase of a cycle (``started`` / ``dedup`` / ``gate`` / ``outcome``) to a single
flat ``cycles.jsonl``. This script computes the #705 contract's MINIMAL
computable form directly against that ledger shape:

- cycle counts by terminal ``outcome`` (success/partial/failed/
  skipped-duplicate/timeout/incomplete)
- ``duplicate_rate`` / ``genuinely_new_proposal_rate`` (outcome ==
  ``skipped-duplicate`` vs. total terminal cycles)
- ``productive_spawn_rate`` / ``gate_pass_rate`` / ``integration_rate``
  (derived from ``gate`` rows and the terminal ``outcome``)
- a gate-fail reason breakdown (from ``gate`` rows with ``allowed=False``,
  plus terminal ``failed``/``timeout`` outcomes that never reached a gate row)
- a dedup-decision breakdown (from ``dedup`` rows), including the top
  ``matched_against`` values — a heuristic-quality signal
- the liveness watchdog (``healthy``/``degraded``/``dead``) per
  ``report-spec.md``'s thresholds

Metrics named in the #705 catalog whose inputs do not exist in the #720
ledger — ``protected_surface_rejections`` (no ``target_paths`` field, a #707
gap), ``cost_per_integrated_change`` (would require a telemetry join, out of
scope: this script's only source of truth is the ledger, no results-dir or
telemetry scraping), ``harvestable_upstream_ratio`` (no
``general_or_host_local`` field, a #672 harvest-classification gap), and
``human_intervention_needed`` (no stop-guard/restore-failure ledger field,
the #707 gap the metric catalog itself names) — are emitted as
``"value": null`` with an explanatory ``"note"``, per the #705 contract's
explicit gap-visibility rule ("never a fabricated 0 or 1").

Adapted from the instance's autonomously-built ``loop_health_report.py``
(P10, commit 6a365ac), harvested per the #672 harvest flow: the ``--test``
self-check style and the compact human-table rendering approach are reused;
the primary data source here is the ledger (cycles.jsonl + rotated
``cycles-*.jsonl.gz``), not a results-dir JSON scrape, to keep one source of
truth.

Read-only: this script never writes anything but stdout.

Usage:
    python3 scripts/loop_metrics_report.py [--state-dir PATH] [--days N] [--json]
    python3 scripts/loop_metrics_report.py --test
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Same default as nanobot.runtime.bridge.STATE_DIR — kept independent (no
# import of runtime code) so this script has zero non-stdlib dependencies.
_DEFAULT_STATE_DIR = "/var/lib/eeepc-agent/self-evolving-agent/state"

# Liveness thresholds — report-spec.md "Parameters" table.
LIVENESS_DAYS_DEFAULT = 7
DUPLICATE_SATURATION_THRESHOLD = 0.8

# Legacy/default degraded-liveness staleness threshold, in seconds (#740).
# Used verbatim (as a no-op age check — see compute_liveness) whenever the
# window has fewer than two `proposed`-phase ledger rows to derive a
# cadence-aware value from, and as the floor beneath the cadence-derived
# threshold (``max(default, 2 * median_proposal_gap)``) so a degenerate,
# very-tight proposal cadence can't collapse the degraded threshold to
# near-zero and flap on noise.
LIVENESS_STALE_SECONDS_DEFAULT = 3600


def _default_state_dir() -> Path:
    env_dir = os.environ.get("STATE_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return Path(_DEFAULT_STATE_DIR)


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_ledger_rows(state_dir: Path, days: int) -> list[dict[str, Any]]:
    """Read ``ledger/cycles.jsonl`` plus any in-window rotated ``.gz`` files.

    Rows without a parseable ``ts`` are dropped (best-effort, matches the
    ledger's own fail-open philosophy) rather than crashing the report.
    """
    ledger_dir = Path(state_dir) / "ledger"
    if not ledger_dir.is_dir():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict[str, Any]] = []

    def _consume(lines: list[str]) -> None:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(rec.get("ts"))
            if ts is None or ts < cutoff:
                continue
            rows.append(rec)

    active_path = ledger_dir / "cycles.jsonl"
    if active_path.exists():
        try:
            _consume(active_path.read_text(encoding="utf-8").splitlines())
        except OSError:
            pass

    for gz_path in sorted(ledger_dir.glob("cycles-*.jsonl.gz")):
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                _consume(fh.read().splitlines())
        except (OSError, gzip.BadGzipFile):
            continue

    return rows


def group_by_cycle(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group ledger rows by ``cycle_id`` into ``{started, dedup, gate: [...], outcome}``.

    If more than one row of a singleton phase (``started``/``dedup``/
    ``outcome``) exists for a cycle_id, the chronologically last one wins —
    mirrors ``metrics.md``'s "Conventions" rule for duplicate `cycle_id`
    entries (count once, keep the last, never double-count).
    """
    cycles: dict[str, dict[str, Any]] = {}
    for row in rows:
        cycle_id = row.get("cycle_id") or ""
        if not cycle_id:
            continue
        bucket = cycles.setdefault(
            cycle_id, {"started": None, "dedup": None, "gate": [], "outcome": None}
        )
        phase = row.get("phase")
        if phase == "gate":
            bucket["gate"].append(row)
        elif phase in ("started", "dedup", "outcome"):
            bucket[phase] = row
    return cycles


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    value = round(numerator / denominator, 4) if denominator else None
    return {"value": value, "numerator": numerator, "denominator": denominator, "n_window": denominator}


def compute_metrics(cycles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total = len(cycles)

    outcome_counts: Counter[str] = Counter()
    for data in cycles.values():
        outcome_row = data["outcome"]
        outcome_counts[outcome_row["outcome"] if outcome_row else "incomplete"] += 1

    terminal_total = sum(v for k, v in outcome_counts.items() if k != "incomplete")
    duplicate_count = outcome_counts.get("skipped-duplicate", 0)
    success_count = outcome_counts.get("success", 0)

    duplicate_rate = _rate(duplicate_count, terminal_total)
    genuinely_new_proposal_rate = _rate(terminal_total - duplicate_count, terminal_total)

    # Spawned = reached a terminal outcome other than the pre-spawn duplicate skip.
    spawned_cycle_ids = [
        cid
        for cid, data in cycles.items()
        if data["outcome"] and data["outcome"]["outcome"] != "skipped-duplicate"
    ]
    spawned_total = len(spawned_cycle_ids)

    gated_cycle_ids = [cid for cid in spawned_cycle_ids if cycles[cid]["gate"]]
    gated_total = len(gated_cycle_ids)
    gate_pass_count = sum(
        1 for cid in gated_cycle_ids if cycles[cid]["gate"][-1].get("allowed") is True
    )

    productive_spawn_rate = _rate(gated_total, spawned_total)
    gate_pass_rate = _rate(gate_pass_count, gated_total)
    integration_rate_of_gated = _rate(success_count, gated_total)
    integration_rate_of_spawned = _rate(success_count, spawned_total)

    gate_fail_breakdown = _gate_fail_breakdown(cycles)
    dedup_breakdown = _dedup_breakdown(cycles)

    metrics: dict[str, Any] = {
        "genuinely_new_proposal_rate": genuinely_new_proposal_rate,
        "duplicate_rate": duplicate_rate,
        "productive_spawn_rate": productive_spawn_rate,
        "gate_pass_rate": gate_pass_rate,
        "integration_rate": {
            "of_gated": integration_rate_of_gated,
            "of_spawned": integration_rate_of_spawned,
        },
        "protected_surface_rejections": {
            "value": None,
            "note": (
                "no target_paths field in the #720 flat cycle ledger "
                "(cf. #705 metrics.md §6) — pending a #707 ledger-write-point addition"
            ),
        },
        "cost_per_integrated_change": {
            "value": None,
            "note": (
                "requires a telemetry join (llm_calls/*.jsonl); this script's "
                "source of truth is the cycle ledger only, per #710 scope"
            ),
        },
        "harvestable_upstream_ratio": {
            "value": None,
            "note": (
                "no general_or_host_local classification field in the #720 ledger "
                "(cf. #705 metrics.md §8) — pending a #672 harvest-pass classification"
            ),
        },
        "human_intervention_needed": {
            "value": None,
            "note": (
                "no stop-guard/restore-failure ledger field yet "
                "(#705 metrics.md §9's named #707 dependency)"
            ),
        },
    }

    return {
        "n_cycles": total,
        "outcome_counts": dict(outcome_counts),
        "metrics": metrics,
        "gate_fail_breakdown": gate_fail_breakdown,
        "dedup_breakdown": dedup_breakdown,
    }


def _gate_fail_breakdown(cycles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Categorical (stage, reason) counts, ordered gate -> outcome -> unclassified."""
    counts: Counter[tuple[str, str | None]] = Counter()

    for data in cycles.values():
        for gate_row in data["gate"]:
            if gate_row.get("allowed") is False:
                reason = gate_row.get("reason") or None
                counts[("gate", reason)] += 1

        outcome_row = data["outcome"]
        if outcome_row and outcome_row["outcome"] in ("failed", "timeout"):
            # Only attribute to "outcome" stage if no gate row already explains
            # the failure — avoids double counting the same terminal failure.
            has_gate_fail = any(g.get("allowed") is False for g in data["gate"])
            if not has_gate_fail:
                reason = outcome_row.get("reason") or None
                stage = "outcome" if reason else "unclassified"
                counts[(stage, reason)] += 1

    total_failures = sum(counts.values())
    stage_order = {"gate": 0, "outcome": 1, "unclassified": 2}
    rows = []
    for (stage, reason), count in counts.items():
        rows.append(
            {
                "stage": stage,
                "reason": reason,
                "count": count,
                "share_of_failures": round(count / total_failures, 4) if total_failures else None,
            }
        )
    rows.sort(key=lambda r: (stage_order.get(r["stage"], 99), -r["count"]))
    return rows


def _dedup_breakdown(cycles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    decision_counts: Counter[str] = Counter()
    matched_against: Counter[str] = Counter()

    for data in cycles.values():
        dedup_row = data["dedup"]
        if dedup_row is None:
            decision_counts["(no dedup row)"] += 1
            continue
        decision_counts[dedup_row.get("decision") or "unclassified"] += 1
        matched = dedup_row.get("matched_against")
        if matched:
            matched_against[matched] += 1

    return {
        "by_decision": dict(decision_counts),
        "top_matched_against": [
            {"matched_against": k, "count": v} for k, v in matched_against.most_common(10)
        ],
    }


_SERVES_CLASSES = ("priority", "vector 1", "vector 2", "hypothesis")


def _serves_class(serves: Any) -> str:
    """#751 goal-alignment classification of a ``'proposed'`` ledger row's
    ``serves`` field. ``"missing"`` covers both pre-#751 rows (no ``serves``
    key at all) and any row whose value doesn't parse — never a crash, per
    this script's own gap-visibility convention. ``"other"`` would mean a
    row wrote a ``serves`` value that ``llm_proposer.validate_sizing``
    should have rejected before write (defensive only; not expected to ever
    accumulate in a report driven by real writes)."""
    text = str(serves or "").strip()
    if not text:
        return "missing"
    low = text.lower()
    if low.startswith("priority "):
        return "priority"
    if low.startswith("vector 1"):
        return "vector 1"
    if low.startswith("vector 2"):
        return "vector 2"
    if low.startswith("hypothesis "):
        return "hypothesis"
    return "other"


_PROPOSER_REJECT_REASONS = ("empty_context", "sizing_rejected", "self_dedup", "error")


def _goal_alignment_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """#751: distribution of ``serves``-classes across ``'proposed'`` ledger
    rows in the window, plus the count of honest ``'proposer_skip'``
    (``no_valuable_task``) events. Rows written before #751 (no ``serves``
    key) count under ``"missing"`` rather than breaking the report.

    #762: also a breakdown of ``'proposer_reject'`` rows (the formerly-silent
    ``maybe_propose`` exits) by reason. Legacy ledgers with no such rows
    report zeros for every reason — never a crash, per this script's own
    gap-visibility convention; an unrecognized reason counts under
    ``"other"`` (defensive only)."""
    proposed_rows = [r for r in rows if r.get("phase") == "proposed"]
    class_counts: Counter[str] = Counter(_serves_class(r.get("serves")) for r in proposed_rows)
    skip_rows = [r for r in rows if r.get("phase") == "proposer_skip"]
    reject_rows = [r for r in rows if r.get("phase") == "proposer_reject"]
    reject_counts: Counter[str] = Counter(
        (str(r.get("reason") or "").strip() if str(r.get("reason") or "").strip() in _PROPOSER_REJECT_REASONS else "other")
        for r in reject_rows
    )
    return {
        "proposed_total": len(proposed_rows),
        "by_serves_class": {name: class_counts.get(name, 0) for name in (*_SERVES_CLASSES, "missing", "other")},
        "no_valuable_task_skips": len(skip_rows),
        "proposer_rejects": {
            "total": len(reject_rows),
            "by_reason": {name: reject_counts.get(name, 0) for name in (*_PROPOSER_REJECT_REASONS, "other")},
        },
    }


# #761: staleness threshold for the report's decay-candidate count — kept in
# sync with nanobot.runtime.demand._DECAY_DAYS (no runtime import here, per
# this script's zero-non-stdlib-dependencies rule).
DECAY_DAYS = 14


def _value_verification(state_dir: Path) -> dict[str, Any]:
    """#761: value-verification block — declared vs confirmed completed-demand
    entries (from ``demand/completed.json``, confirmation written ONLY from
    harness-observed usage evidence by ``usage_evidence.confirm_serves``),
    plus a decay view over the ``usage/last_used.json`` sidecar (tracked
    artifacts whose newest evidence is older than ``DECAY_DAYS``, and the
    single oldest one). Missing/corrupt sidecars read as zeros — never a
    crash, per this script's own gap-visibility convention."""
    declared = confirmed = 0
    try:
        completed = json.loads(
            (Path(state_dir) / "demand" / "completed.json").read_text(encoding="utf-8")
        )
        entries = completed.get("entries") if isinstance(completed, dict) else None
        for entry in (entries or {}).values():
            if not isinstance(entry, dict):
                continue
            declared += 1
            if entry.get("confirmed") is True:
                confirmed += 1
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    decay_candidates = 0
    oldest_path: str | None = None
    oldest_ts: str | None = None
    try:
        usage = json.loads(
            (Path(state_dir) / "usage" / "last_used.json").read_text(encoding="utf-8")
        )
        entries = usage.get("entries") if isinstance(usage, dict) else None
        cutoff = datetime.now(timezone.utc) - timedelta(days=DECAY_DAYS)
        for path, entry in (entries or {}).items():
            if not isinstance(entry, dict):
                continue
            stamps = [
                ts
                for ts in (_parse_ts(entry.get("last_used")), _parse_ts(entry.get("last_touched")))
                if ts is not None
            ]
            if not stamps:
                continue
            newest = max(stamps)
            newest_iso = newest.isoformat().replace("+00:00", "Z")
            if newest < cutoff:
                decay_candidates += 1
            if oldest_ts is None or newest_iso < oldest_ts:
                oldest_ts = newest_iso
                oldest_path = str(path)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    return {
        "completed_declared": declared,
        "completed_confirmed": confirmed,
        "decay_candidates": decay_candidates,
        "oldest_unused": (
            {"path": oldest_path, "last_evidence": oldest_ts} if oldest_path else None
        ),
    }


# #765: the key scorecard metrics shown in the report, as (section, metric)
# pairs — mirrors nanobot.runtime.scorecard's snapshot layout (read-only
# here; this script never computes or writes a scorecard, it only renders
# the persisted latest.json/history.jsonl, per its zero-non-stdlib rule).
_SCORECARD_KEY_METRICS = (
    ("loop", "integrations"),
    ("loop", "repeat_failure_rate"),
    ("loop", "idle_share"),
    ("cost", "llm_calls"),
    ("cost", "tokens_per_integration"),
    ("quality", "compile_clean_ratio"),
    ("value", "confirmed_ratio"),
    ("value", "decay_candidates"),
    # #780: held-out verification pack — absent in pre-#780 snapshots; the
    # renderer already tolerates missing sections/metrics ("n/a").
    ("heldout", "checked"),
    ("heldout", "heldout_gap"),
)


def _scorecard_block(state_dir: Path) -> dict[str, Any]:
    """#765: latest persisted scorecard snapshot + the previous history
    entry (for trend arrows) + the open goal gaps. Missing/corrupt
    scorecard state reads as absent (``available: False``) — never a
    crash, per this script's gap-visibility convention."""
    latest: dict[str, Any] | None = None
    previous: dict[str, Any] | None = None
    try:
        data = json.loads(
            (Path(state_dir) / "scorecard" / "latest.json").read_text(encoding="utf-8")
        )
        if isinstance(data, dict) and data.get("schema_version"):
            latest = data
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    try:
        lines = (
            (Path(state_dir) / "scorecard" / "history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        # The last history line IS the latest snapshot; the previous one is
        # the trend baseline. Walk backwards past the latest's timestamp.
        latest_ts = (latest or {}).get("computed_at_utc")
        for line in reversed(lines[-50:]):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if latest_ts and row.get("computed_at_utc") == latest_ts:
                continue
            previous = row
            break
    except (OSError, AttributeError):
        pass

    gaps = (latest or {}).get("gaps")
    return {
        "available": latest is not None,
        "latest": latest,
        "previous": previous,
        "gaps": gaps if isinstance(gaps, list) else [],
    }


# ─── #782: RSI maturity ladder — informational L1-criteria line ─────────────
#
# CONSTITUTION.md "RSI maturity ladder": L0 Delegation / L1 Net Positive /
# L2 Ignition / L3 Inflection (weco.ai's framework). The current level is
# hardcoded L0 — no higher level is claimed without evidence; this block only
# reports, per criterion, how today's durable state measures against the L1
# bar. Read-only over existing sidecars; never a new runtime module, never a
# loop behavior change (spec R36).

# The operator owns this number: a declared (not measured) daily LLM token
# budget for L1 criterion (c). Change it deliberately, in review — it is the
# denominator of a maturity claim, not a tuning knob.
_L1_TOKEN_BUDGET_PER_DAY = 5_000_000

# L1 criterion (a): ≥1 confirmed integration per day for this many
# consecutive days, from non-operator demand kinds (id NOT 'priority-*').
_L1_STREAK_TARGET_DAYS = 7

# L1 criterion (d): heldout_gap target — mirrors the scorecard's own
# heldout_gap max target (no runtime import, per the zero-non-stdlib rule).
_L1_HELDOUT_GAP_TARGET = 0.2


def _rsi_block(state_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    """#782: per-criterion L1 status, computed from existing state only.

    Missing/corrupt sidecars degrade to ``status: "no data"`` per criterion —
    never a crash, never a fabricated pass/fail, per this script's
    gap-visibility convention. Criterion (b) — operator-intervention-free —
    is NOT mechanically detectable from state, so it honestly reports
    ``manual attestation required`` instead of faking a signal.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()

    # (a) confirmed-integrations-per-day streak from demand/completed.json:
    # entries with confirmed==True whose demand_id is NOT operator-sourced
    # ('priority-*'), bucketed by UTC day of their ts. The streak counts back
    # from today (a today-with-no-confirmation-yet falls back to yesterday —
    # a day still in progress must not break the streak), capped at target.
    streak: dict[str, Any] = {"status": "no data", "streak_days": None, "target_days": _L1_STREAK_TARGET_DAYS, "met": None}
    try:
        completed = json.loads(
            (Path(state_dir) / "demand" / "completed.json").read_text(encoding="utf-8")
        )
        entries = completed.get("entries") if isinstance(completed, dict) else None
        if isinstance(entries, dict):
            confirmed_days = set()
            for demand_id, entry in entries.items():
                if not isinstance(entry, dict) or entry.get("confirmed") is not True:
                    continue
                if str(demand_id).startswith("priority-"):
                    continue  # operator-sourced demand does not count toward L1
                ts = _parse_ts(entry.get("ts"))
                if ts is not None:
                    confirmed_days.add(ts.astimezone(timezone.utc).date())
            day = today if today in confirmed_days else today - timedelta(days=1)
            count = 0
            while day in confirmed_days and count < _L1_STREAK_TARGET_DAYS:
                count += 1
                day -= timedelta(days=1)
            streak = {
                "status": "ok",
                "streak_days": count,
                "target_days": _L1_STREAK_TARGET_DAYS,
                "met": count >= _L1_STREAK_TARGET_DAYS,
            }
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    # (c) tokens on the last FULL day (yesterday UTC) vs the declared budget,
    # from the daily llm_calls/YYYY-MM-DD.jsonl telemetry file (#675).
    last_full_day = (today - timedelta(days=1)).isoformat()
    tokens_block: dict[str, Any] = {
        "status": "no data",
        "day": last_full_day,
        "tokens": None,
        "budget": _L1_TOKEN_BUDGET_PER_DAY,
        "met": None,
    }
    try:
        lines = (
            (Path(state_dir) / "llm_calls" / f"{last_full_day}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        tokens = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            try:
                tokens += int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                continue
        tokens_block = {
            "status": "ok",
            "day": last_full_day,
            "tokens": tokens,
            "budget": _L1_TOKEN_BUDGET_PER_DAY,
            "met": tokens <= _L1_TOKEN_BUDGET_PER_DAY,
        }
    except (OSError, AttributeError):
        pass

    # (d) heldout_gap from the persisted scorecard snapshot (#780).
    heldout_block: dict[str, Any] = {
        "status": "no data",
        "heldout_gap": None,
        "target": _L1_HELDOUT_GAP_TARGET,
        "met": None,
    }
    try:
        latest = json.loads(
            (Path(state_dir) / "scorecard" / "latest.json").read_text(encoding="utf-8")
        )
        gap = ((latest or {}).get("heldout") or {}).get("heldout_gap") if isinstance(latest, dict) else None
        if isinstance(gap, (int, float)):
            heldout_block = {
                "status": "ok",
                "heldout_gap": gap,
                "target": _L1_HELDOUT_GAP_TARGET,
                "met": gap <= _L1_HELDOUT_GAP_TARGET,
            }
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    return {
        "level": "L0",
        "level_name": "Delegation",
        "l1_criteria": {
            "confirmed_streak": streak,
            "operator_intervention_free": {
                "status": "manual attestation required",
                "met": None,
            },
            "tokens_last_full_day": tokens_block,
            "heldout_gap": heldout_block,
        },
    }


def _median_gap_seconds(timestamps: list[datetime]) -> float | None:
    """Median gap (seconds) between consecutive, time-sorted timestamps.

    ``None`` when fewer than two timestamps are given — there is no gap to
    measure (#740's "legacy fixed threshold" fallback trigger).
    """
    if len(timestamps) < 2:
        return None
    ordered = sorted(timestamps)
    gaps = sorted((b - a).total_seconds() for a, b in zip(ordered, ordered[1:]))
    n = len(gaps)
    mid = n // 2
    if n % 2:
        return gaps[mid]
    return (gaps[mid - 1] + gaps[mid]) / 2


def compute_liveness(
    cycles: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    duplicate_rate_value: float | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Liveness watchdog, with a cadence-aware degraded threshold (#740).

    ``effective_threshold_seconds = max(LIVENESS_STALE_SECONDS_DEFAULT,
    2 * median_proposal_gap_seconds)``, where the median gap is measured
    across consecutive ``proposed``-phase rows (written by the LLM proposer,
    #730) in the report window. With fewer than two ``proposed`` rows in the
    window there is no cadence to measure, so no age-based check is applied
    at all — this reproduces the pre-#740 behavior exactly (liveness was
    already implicitly bounded by whether a ``success`` outcome exists in
    the window, with no separate "how long ago" check).
    """
    now = now or datetime.now(timezone.utc)
    last_cycle_ts: str | None = None
    last_productive_ts: str | None = None

    for data in cycles.values():
        for row in (data["started"], data["dedup"], *data["gate"], data["outcome"]):
            if row and row.get("ts"):
                if last_cycle_ts is None or row["ts"] > last_cycle_ts:
                    last_cycle_ts = row["ts"]
        outcome_row = data["outcome"]
        if outcome_row and outcome_row.get("outcome") == "success" and outcome_row.get("ts"):
            if last_productive_ts is None or outcome_row["ts"] > last_productive_ts:
                last_productive_ts = outcome_row["ts"]

    proposed_timestamps = [
        ts
        for ts in (_parse_ts(row.get("ts")) for row in rows if row.get("phase") == "proposed")
        if ts is not None
    ]
    median_gap_seconds = _median_gap_seconds(proposed_timestamps)

    if median_gap_seconds is not None:
        threshold_source = "proposal_cadence"
        effective_threshold_seconds = max(LIVENESS_STALE_SECONDS_DEFAULT, 2 * median_gap_seconds)
    else:
        threshold_source = "legacy_default"
        effective_threshold_seconds = float(LIVENESS_STALE_SECONDS_DEFAULT)

    stale = False
    if median_gap_seconds is not None and last_productive_ts is not None:
        last_productive_dt = _parse_ts(last_productive_ts)
        if last_productive_dt is not None:
            age_seconds = (now - last_productive_dt).total_seconds()
            stale = age_seconds > effective_threshold_seconds

    if not cycles:
        state = "dead"
    elif (
        last_productive_ts is not None
        and not stale
        and (duplicate_rate_value is None or duplicate_rate_value < DUPLICATE_SATURATION_THRESHOLD)
    ):
        state = "healthy"
    else:
        state = "degraded"

    return {
        "state": state,
        "last_productive_ts": last_productive_ts,
        "last_cycle_ts": last_cycle_ts,
        "effective_threshold_seconds": round(effective_threshold_seconds, 3),
        "threshold_source": threshold_source,
        "median_proposal_gap_seconds": (
            round(median_gap_seconds, 3) if median_gap_seconds is not None else None
        ),
    }


def build_report(state_dir: Path, days: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    rows = load_ledger_rows(state_dir, days)
    cycles = group_by_cycle(rows)
    computed = compute_metrics(cycles)
    liveness = compute_liveness(cycles, rows, computed["metrics"]["duplicate_rate"]["value"], now=now)
    goal_alignment = _goal_alignment_breakdown(rows)

    window_start = (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    window_end = now.isoformat().replace("+00:00", "Z")

    return {
        "window": {
            "start": window_start,
            "end": window_end,
            "n_cycles": computed["n_cycles"],
            "params": {
                "days": days,
                "duplicate_rate_saturation_threshold": DUPLICATE_SATURATION_THRESHOLD,
            },
        },
        "generated_at": window_end,
        "liveness": liveness,
        "outcome_counts": computed["outcome_counts"],
        "metrics": computed["metrics"],
        "gate_fail_breakdown": computed["gate_fail_breakdown"],
        "dedup_breakdown": computed["dedup_breakdown"],
        "goal_alignment": goal_alignment,
        "value_verification": _value_verification(state_dir),
        "scorecard": _scorecard_block(state_dir),
        "rsi": _rsi_block(state_dir, now=now),
    }


def _fmt_rate(metric: dict[str, Any]) -> str:
    value = metric.get("value")
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def render_table(report: dict[str, Any]) -> str:
    lines: list[str] = []
    window = report["window"]
    lines.append(
        f"Loop metrics report — window {window['start']} .. {window['end']} "
        f"({window['n_cycles']} cycles, generated {report['generated_at']})"
    )
    lines.append("")

    liveness = report["liveness"]
    lines.append(
        f"Liveness: {liveness['state'].upper()}  "
        f"last_productive={liveness['last_productive_ts'] or 'n/a'}  "
        f"last_cycle={liveness['last_cycle_ts'] or 'n/a'}"
    )
    median_gap = liveness.get("median_proposal_gap_seconds")
    lines.append(
        f"  effective_threshold={liveness['effective_threshold_seconds']:.0f}s "
        f"(source={liveness['threshold_source']}, "
        f"median_proposal_gap={'n/a' if median_gap is None else f'{median_gap:.0f}s'})"
    )
    lines.append("")

    lines.append("Outcome counts:")
    if report["outcome_counts"]:
        for outcome, count in sorted(report["outcome_counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {outcome:<20} {count}")
    else:
        lines.append("  (no cycles in window)")
    lines.append("")

    m = report["metrics"]
    lines.append("Core metrics:")
    lines.append(f"  {'metric':<32} {'value':>8} {'num':>6} {'den':>6}")
    for name in ("genuinely_new_proposal_rate", "duplicate_rate", "productive_spawn_rate", "gate_pass_rate"):
        row = m[name]
        lines.append(f"  {name:<32} {_fmt_rate(row):>8} {row['numerator']:>6} {row['denominator']:>6}")
    for sub_name, row in (("integration_rate.of_gated", m["integration_rate"]["of_gated"]),
                          ("integration_rate.of_spawned", m["integration_rate"]["of_spawned"])):
        lines.append(f"  {sub_name:<32} {_fmt_rate(row):>8} {row['numerator']:>6} {row['denominator']:>6}")
    lines.append("")

    lines.append("Gate-fail reason breakdown:")
    non_zero = [r for r in report["gate_fail_breakdown"] if r["count"] > 0]
    if non_zero:
        lines.append(f"  {'stage':<14} {'reason':<40} {'count':>6} {'share':>8}")
        for row in non_zero:
            share = f"{row['share_of_failures'] * 100:.1f}%" if row["share_of_failures"] is not None else "n/a"
            lines.append(f"  {row['stage']:<14} {str(row['reason']):<40} {row['count']:>6} {share:>8}")
    else:
        lines.append("  (no gate/outcome failures in window)")
    lines.append("")

    lines.append("Dedup-decision breakdown:")
    dedup = report["dedup_breakdown"]
    if dedup["by_decision"]:
        for decision, count in sorted(dedup["by_decision"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {decision:<28} {count}")
    else:
        lines.append("  (no dedup rows in window)")
    if dedup["top_matched_against"]:
        lines.append("  Top matched_against:")
        for entry in dedup["top_matched_against"]:
            lines.append(f"    {entry['matched_against']:<40} {entry['count']}")
    lines.append("")

    lines.append("Metrics pending upstream inputs (null, per #705 gap-visibility rule):")
    for name in ("protected_surface_rejections", "cost_per_integrated_change", "harvestable_upstream_ratio", "human_intervention_needed"):
        lines.append(f"  {name}: n/a — {m[name]['note']}")
    lines.append("")

    goal_alignment = report["goal_alignment"]
    lines.append(
        f"Goal alignment (#751 — 'serves' distribution over "
        f"{goal_alignment['proposed_total']} proposed rows; "
        f"{goal_alignment['no_valuable_task_skips']} no_valuable_task skips in window):"
    )
    by_class = goal_alignment["by_serves_class"]
    if goal_alignment["proposed_total"] or any(by_class.values()):
        for name, count in sorted(by_class.items(), key=lambda kv: -kv[1]):
            if count:
                lines.append(f"  {name:<12} {count}")
    else:
        lines.append("  (no proposed rows in window)")

    # #762: legacy ledgers predate this key entirely — tolerate its absence.
    rejects = goal_alignment.get("proposer_rejects") or {"total": 0, "by_reason": {}}
    lines.append("")
    lines.append(
        f"Proposer rejects (#762 — silent maybe_propose exits, {rejects['total']} in window):"
    )
    by_reason = rejects.get("by_reason") or {}
    if any(by_reason.values()):
        for name, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            if count:
                lines.append(f"  {name:<18} {count}")
    else:
        lines.append("  (no proposer_reject rows in window)")

    # #761: value verification — declared vs harness-confirmed serves, decay.
    # Legacy state dirs predate this key entirely — tolerate its absence.
    vv = report.get("value_verification") or {
        "completed_declared": 0,
        "completed_confirmed": 0,
        "decay_candidates": 0,
        "oldest_unused": None,
    }
    lines.append("")
    lines.append(
        "Value verification (#761 — harness-observed evidence only, "
        "never subagent claims):"
    )
    lines.append(f"  completed declared           {vv['completed_declared']}")
    lines.append(f"  completed confirmed          {vv['completed_confirmed']}")
    lines.append(f"  decay candidates (>{DECAY_DAYS}d)      {vv['decay_candidates']}")
    oldest = vv.get("oldest_unused")
    if oldest:
        lines.append(
            f"  oldest tracked artifact      {oldest['path']} "
            f"(last evidence {oldest['last_evidence']})"
        )
    else:
        lines.append("  oldest tracked artifact      n/a (no usage evidence yet)")

    # #765: instance scorecard — latest snapshot + trend arrows vs the
    # previous history entry + open goal gaps. Legacy state dirs predate
    # the scorecard entirely — tolerate its absence.
    sc = report.get("scorecard") or {"available": False, "latest": None, "previous": None, "gaps": []}
    lines.append("")
    lines.append("Instance scorecard (#765 — deterministic fitness snapshot):")
    if not sc.get("available"):
        lines.append("  (no scorecard state yet)")
    else:
        latest = sc.get("latest") or {}
        previous = sc.get("previous") or {}
        lines.append(f"  computed_at: {latest.get('computed_at_utc') or 'n/a'}")
        for section, metric in _SCORECARD_KEY_METRICS:
            cur = (latest.get(section) or {}).get(metric)
            prev = (previous.get(section) or {}).get(metric)
            arrow = "→"
            if isinstance(cur, (int, float)) and isinstance(prev, (int, float)):
                arrow = "↑" if cur > prev else ("↓" if cur < prev else "→")
            cur_text = "n/a" if cur is None else str(cur)
            lines.append(f"  {section + '.' + metric:<28} {cur_text:>10} {arrow}")
        gaps = sc.get("gaps") or []
        if gaps:
            lines.append("  Open goal gaps:")
            for gap in gaps:
                if not isinstance(gap, dict):
                    continue
                lines.append(
                    f"    [{gap.get('vector')}] {gap.get('metric')}: "
                    f"{gap.get('current')} vs target {gap.get('target')}"
                )
        else:
            lines.append("  Open goal gaps: (none)")

    # #782: RSI maturity ladder — informational only; the ladder and the L1
    # criteria are defined in CONSTITUTION.md ("RSI maturity ladder").
    # Legacy reports predate this key entirely — tolerate its absence.
    rsi = report.get("rsi") or {"level": "L0", "level_name": "Delegation", "l1_criteria": {}}
    criteria = rsi.get("l1_criteria") or {}
    lines.append("")
    lines.append("RSI level (#782 — informational; ladder in CONSTITUTION.md):")
    lines.append(f"  current level: {rsi.get('level', 'L0')} ({rsi.get('level_name', 'Delegation')})")
    lines.append("  L1 criteria today:")

    def _verdict(block: dict[str, Any]) -> str:
        if block.get("status") != "ok":
            return "--"  # the value column already says "no data"
        met = block.get("met")
        return "PASS" if met else "FAIL"

    streak = criteria.get("confirmed_streak") or {}
    if streak.get("status") == "ok":
        streak_text = f"{streak.get('streak_days')}/{streak.get('target_days', _L1_STREAK_TARGET_DAYS)} days"
    else:
        streak_text = "no data"
    lines.append(
        f"    (a) confirmed-integration streak   {streak_text:<24} {_verdict(streak)}"
        "  (non-priority demand, harness-confirmed)"
    )
    lines.append(
        "    (b) operator-intervention-free     manual attestation required"
        "  (not mechanically detectable)"
    )
    tok = criteria.get("tokens_last_full_day") or {}
    if tok.get("status") == "ok":
        tok_text = f"{tok.get('tokens'):,} / {tok.get('budget', _L1_TOKEN_BUDGET_PER_DAY):,}"
    else:
        tok_text = "no data"
    lines.append(
        f"    (c) tokens last full day           {tok_text:<24} {_verdict(tok)}"
        f"  (day {tok.get('day', 'n/a')}, operator-owned budget)"
    )
    ho = criteria.get("heldout_gap") or {}
    if ho.get("status") == "ok":
        ho_text = f"{ho.get('heldout_gap')} <= {ho.get('target', _L1_HELDOUT_GAP_TARGET)}"
    else:
        ho_text = "no data"
    lines.append(
        f"    (d) heldout_gap                    {ho_text:<24} {_verdict(ho)}"
    )

    return "\n".join(lines)


def _self_test() -> None:
    """Build a temp fixture ledger with a known mix of cycles and assert the report."""
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp()
    try:
        state_dir = Path(tmp)
        ledger_dir = state_dir / "ledger"
        ledger_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc)

        def ts(minutes_ago: int) -> str:
            return (now - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")

        rows = [
            # c1: proceeded -> gate allowed -> success (productive + integrated)
            {"phase": "started", "cycle_id": "c1", "ts": ts(50)},
            {"phase": "dedup", "cycle_id": "c1", "decision": "proceeded", "matched_against": None, "ts": ts(49)},
            {"phase": "gate", "cycle_id": "c1", "allowed": True, "reason": None, "ts": ts(48)},
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "reason": None, "ts": ts(47)},
            # c2: proceeded -> gate rejected (gate_failed)
            {"phase": "started", "cycle_id": "c2", "ts": ts(40)},
            {"phase": "dedup", "cycle_id": "c2", "decision": "proceeded", "matched_against": None, "ts": ts(39)},
            {"phase": "gate", "cycle_id": "c2", "allowed": False, "reason": "gate_failed", "ts": ts(38)},
            {"phase": "outcome", "cycle_id": "c2", "outcome": "failed", "reason": "gate_failed", "ts": ts(37)},
            # c3: skipped as duplicate before spawn
            {"phase": "started", "cycle_id": "c3", "ts": ts(30)},
            {"phase": "dedup", "cycle_id": "c3", "decision": "skipped_duplicate", "matched_against": "done:title-x", "ts": ts(29)},
            {"phase": "outcome", "cycle_id": "c3", "outcome": "skipped-duplicate", "reason": None, "ts": ts(29)},
            # c4: proceeded, no gate row, failed with no_commit-style reason
            {"phase": "started", "cycle_id": "c4", "ts": ts(20)},
            {"phase": "dedup", "cycle_id": "c4", "decision": "proceeded", "matched_against": None, "ts": ts(19)},
            {"phase": "outcome", "cycle_id": "c4", "outcome": "failed", "reason": "no_commit", "ts": ts(18)},
            # #751: goal-alignment rows — mix of serves-classes, one legacy
            # row with no serves at all, and two honest no-op skips. Reuse
            # the existing c1..c5 cycle_ids (a 'proposed' row for a cycle
            # that also has started/gate/outcome rows is the normal shape) so
            # these don't inflate n_cycles with extra incomplete buckets.
            {"phase": "proposed", "cycle_id": "c1", "task_title": "t1", "serves": "priority 5", "ts": ts(50)},
            {"phase": "proposed", "cycle_id": "c2", "task_title": "t2", "serves": "vector 1: reduces disk writes", "ts": ts(40)},
            {"phase": "proposed", "cycle_id": "c3", "task_title": "t3", "serves": "vector 2", "ts": ts(30)},
            {"phase": "proposed", "cycle_id": "c4", "task_title": "t4", "serves": "hypothesis h3", "ts": ts(20)},
            {"phase": "proposed", "cycle_id": "c5", "task_title": "t5", "ts": ts(10)},
            {"phase": "proposer_skip", "reason": "nothing valuable this cycle", "ts": ts(4)},
            {"phase": "proposer_skip", "reason": "still nothing", "ts": ts(3)},
            # #762: proposer_reject rows — silent maybe_propose exits.
            {"phase": "proposer_reject", "reason": "self_dedup", "task_title": "t-dup", "matched_against": "feat: t-dup done", "ts": ts(2)},
            {"phase": "proposer_reject", "reason": "self_dedup", "task_title": "t-dup2", "matched_against": "feat: t-dup done", "ts": ts(2)},
            {"phase": "proposer_reject", "reason": "sizing_rejected", "task_title": "t-big", "detail": "too big", "ts": ts(1)},
            {"phase": "proposer_reject", "reason": "error", "detail": "RuntimeError: boom", "ts": ts(1)},
        ]
        with open(ledger_dir / "cycles.jsonl", "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

        # #761: value-verification sidecars — one confirmed of two completed
        # entries, one stale + one fresh tracked artifact.
        demand_dir = state_dir / "demand"
        demand_dir.mkdir(parents=True)
        (demand_dir / "completed.json").write_text(
            json.dumps(
                {
                    "schema_version": "demand-completed-v1",
                    "entries": {
                        "priority-aaa": {"cycle_id": "c1", "ts": ts(47), "files_changed": ["scripts/a.py"], "confirmed": True},
                        "priority-bbb": {"cycle_id": "c5", "ts": ts(7), "files_changed": ["scripts/b.py"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        usage_dir = state_dir / "usage"
        usage_dir.mkdir(parents=True)
        stale_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        (usage_dir / "last_used.json").write_text(
            json.dumps(
                {
                    "schema_version": "usage-evidence-v1",
                    "entries": {
                        "scripts/old.py": {"last_used": stale_ts, "last_touched": None, "signal": "pycache"},
                        "scripts/a.py": {"last_used": ts(5), "last_touched": None, "signal": "pycache"},
                    },
                }
            ),
            encoding="utf-8",
        )

        # #765: scorecard sidecars — a previous history entry plus a latest
        # snapshot carrying one open V1 gap.
        scorecard_dir = state_dir / "scorecard"
        scorecard_dir.mkdir(parents=True)
        prev_snapshot = {
            "schema_version": "scorecard-v1",
            "computed_at_utc": ts(120),
            "loop": {"integrations": 1, "repeat_failure_rate": 0.2, "idle_share": 0.5},
            "cost": {"llm_calls": 10, "tokens_per_integration": 1000},
            "quality": {"compile_clean_ratio": 1.0},
            "value": {"confirmed_ratio": None, "decay_candidates": 0},
            "gaps": [],
        }
        latest_snapshot = {
            "schema_version": "scorecard-v1",
            "computed_at_utc": ts(5),
            "loop": {"integrations": 2, "repeat_failure_rate": 0.5, "idle_share": 0.5},
            "cost": {"llm_calls": 12, "tokens_per_integration": 900},
            "quality": {"compile_clean_ratio": 1.0},
            "value": {"confirmed_ratio": None, "decay_candidates": 1},
            "gaps": [
                {
                    "metric": "repeat_failure_rate",
                    "vector": "V1",
                    "current": 0.5,
                    "target": 0.3,
                    "evidence": "repeat_failure_rate=0.5 is above max target 0.3",
                }
            ],
        }
        (scorecard_dir / "latest.json").write_text(
            json.dumps(latest_snapshot), encoding="utf-8"
        )
        with open(scorecard_dir / "history.jsonl", "w", encoding="utf-8") as fh:
            fh.write(json.dumps(prev_snapshot) + "\n")
            fh.write(json.dumps(latest_snapshot) + "\n")

        # A rotated gzip file, well within the window, adding one more success.
        gz_rows = [
            {"phase": "started", "cycle_id": "c5", "ts": ts(10)},
            {"phase": "dedup", "cycle_id": "c5", "decision": "proceeded", "matched_against": None, "ts": ts(9)},
            {"phase": "gate", "cycle_id": "c5", "allowed": True, "reason": None, "ts": ts(8)},
            {"phase": "outcome", "cycle_id": "c5", "outcome": "success", "reason": None, "ts": ts(7)},
        ]
        import gzip as _gzip

        yesterday = (now.date()).isoformat()
        gz_path = ledger_dir / f"cycles-{yesterday}.jsonl.gz"
        with _gzip.open(gz_path, "wt", encoding="utf-8") as fh:
            for row in gz_rows:
                fh.write(json.dumps(row) + "\n")

        report = build_report(state_dir, days=7)

        assert report["window"]["n_cycles"] == 5, report["window"]["n_cycles"]
        assert report["outcome_counts"]["success"] == 2
        assert report["outcome_counts"]["failed"] == 2
        assert report["outcome_counts"]["skipped-duplicate"] == 1

        m = report["metrics"]
        assert m["duplicate_rate"]["value"] == pytest_approx(1 / 5)
        assert m["duplicate_rate"]["numerator"] == 1
        assert m["duplicate_rate"]["denominator"] == 5

        assert m["genuinely_new_proposal_rate"]["value"] == pytest_approx(4 / 5)

        # spawned = 4 (all but c3, the precheck-duplicate skip);
        # gated = 3 (c1, c2, c5 all reached a gate row; c4 did not)
        assert m["productive_spawn_rate"]["denominator"] == 4
        assert m["productive_spawn_rate"]["numerator"] == 3
        assert m["gate_pass_rate"]["denominator"] == 3
        assert m["gate_pass_rate"]["numerator"] == 2
        assert m["integration_rate"]["of_spawned"]["numerator"] == 2
        assert m["integration_rate"]["of_spawned"]["denominator"] == 4
        assert m["integration_rate"]["of_gated"]["numerator"] == 2
        assert m["integration_rate"]["of_gated"]["denominator"] == 3

        for name in ("protected_surface_rejections", "cost_per_integrated_change", "harvestable_upstream_ratio", "human_intervention_needed"):
            assert m[name]["value"] is None
            assert "note" in m[name]

        breakdown = {(r["stage"], r["reason"]): r["count"] for r in report["gate_fail_breakdown"]}
        assert breakdown[("gate", "gate_failed")] == 1
        assert breakdown[("outcome", "no_commit")] == 1

        dedup = report["dedup_breakdown"]
        assert dedup["by_decision"]["proceeded"] == 4
        assert dedup["by_decision"]["skipped_duplicate"] == 1
        assert dedup["top_matched_against"][0]["matched_against"] == "done:title-x"

        goal_alignment = report["goal_alignment"]
        assert goal_alignment["proposed_total"] == 5
        assert goal_alignment["by_serves_class"]["priority"] == 1
        assert goal_alignment["by_serves_class"]["vector 1"] == 1
        assert goal_alignment["by_serves_class"]["vector 2"] == 1
        assert goal_alignment["by_serves_class"]["hypothesis"] == 1
        assert goal_alignment["by_serves_class"]["missing"] == 1
        assert goal_alignment["by_serves_class"]["other"] == 0
        assert goal_alignment["no_valuable_task_skips"] == 2

        # #762: proposer_reject breakdown.
        rejects = goal_alignment["proposer_rejects"]
        assert rejects["total"] == 4
        assert rejects["by_reason"]["self_dedup"] == 2
        assert rejects["by_reason"]["sizing_rejected"] == 1
        assert rejects["by_reason"]["error"] == 1
        assert rejects["by_reason"]["empty_context"] == 0
        assert rejects["by_reason"]["other"] == 0

        assert report["liveness"]["state"] == "healthy"

        # #765: scorecard block — latest + previous (trend baseline) + gaps.
        sc = report["scorecard"]
        assert sc["available"] is True
        assert sc["latest"]["loop"]["integrations"] == 2
        assert sc["previous"]["loop"]["integrations"] == 1
        assert sc["gaps"][0]["metric"] == "repeat_failure_rate"
        assert sc["gaps"][0]["vector"] == "V1"
        table_with_scorecard = render_table(report)
        assert "Instance scorecard" in table_with_scorecard
        assert "repeat_failure_rate" in table_with_scorecard

        # #782: RSI block — L0 hardcoded; the fixture's only confirmed
        # completed entry is priority-sourced, so the streak is a real 0
        # (data present, no qualifying days); no llm_calls fixture and no
        # heldout section in the scorecard fixture → "no data" for (c)/(d).
        rsi = report["rsi"]
        assert rsi["level"] == "L0"
        crit = rsi["l1_criteria"]
        assert crit["confirmed_streak"]["status"] == "ok"
        assert crit["confirmed_streak"]["streak_days"] == 0
        assert crit["operator_intervention_free"]["status"] == "manual attestation required"
        assert crit["tokens_last_full_day"]["status"] == "no data"
        assert crit["heldout_gap"]["status"] == "no data"
        assert "RSI level" in table_with_scorecard
        assert "current level: L0 (Delegation)" in table_with_scorecard

        # #761: value-verification block.
        vv = report["value_verification"]
        assert vv["completed_declared"] == 2
        assert vv["completed_confirmed"] == 1
        assert vv["decay_candidates"] == 1
        assert vv["oldest_unused"]["path"] == "scripts/old.py"

        # Empty-ledger case: must render cleanly, not crash.
        empty_dir = Path(tempfile.mkdtemp())
        try:
            empty_report = build_report(empty_dir, days=7)
            assert empty_report["liveness"]["state"] == "dead"
            assert empty_report["window"]["n_cycles"] == 0
            # #762: a legacy/empty ledger yields all-zero reject counts.
            empty_rejects = empty_report["goal_alignment"]["proposer_rejects"]
            assert empty_rejects["total"] == 0
            assert all(v == 0 for v in empty_rejects["by_reason"].values())
            # #761: missing sidecars read as zeros, never a crash.
            empty_vv = empty_report["value_verification"]
            assert empty_vv["completed_declared"] == 0
            assert empty_vv["completed_confirmed"] == 0
            assert empty_vv["decay_candidates"] == 0
            assert empty_vv["oldest_unused"] is None
            # #765: missing scorecard state → available=False, no crash.
            empty_sc = empty_report["scorecard"]
            assert empty_sc["available"] is False
            assert empty_sc["gaps"] == []
            # #782: an empty state dir reports every RSI criterion honestly
            # as "no data" — never a fabricated pass/fail.
            empty_rsi = empty_report["rsi"]
            assert empty_rsi["level"] == "L0"
            assert empty_rsi["l1_criteria"]["confirmed_streak"]["status"] == "no data"
            assert empty_rsi["l1_criteria"]["tokens_last_full_day"]["status"] == "no data"
            assert empty_rsi["l1_criteria"]["heldout_gap"]["status"] == "no data"
            table = render_table(empty_report)
            assert "DEAD" in table
            assert "no scorecard state yet" in table
        finally:
            shutil.rmtree(empty_dir)

        print("PASS: loop_metrics_report self-tests passed.")
        print()
        print(render_table(report))
    finally:
        shutil.rmtree(tmp)


def pytest_approx(value: float):
    """Tiny stdlib-only stand-in so ``--test`` doesn't need pytest installed."""
    return round(value, 6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=None, help="state dir containing ledger/ (default: env-resolved)")
    parser.add_argument("--days", type=int, default=LIVENESS_DAYS_DEFAULT, help="window size in days (default: 7)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
    parser.add_argument("--test", action="store_true", help="run self-tests against a temp fixture ledger and exit")
    args = parser.parse_args(argv)

    if args.test:
        _self_test()
        return 0

    state_dir = args.state_dir or _default_state_dir()
    report = build_report(state_dir, days=args.days)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_table(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
