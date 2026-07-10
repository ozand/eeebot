# Report output contract

- **Issue:** #705 (loop-redesign ticket E)
- **Status:** design/contract only — specifies shapes, not code. The report
  script(s) implementing this contract are follow-up **#710**; the concrete
  ledger fields some parts of this contract read (notably the
  `human_intervention_needed` proxy in `metrics.md` §9) may still need
  writer-side additions scoped to **#707**.
- **story_id:** docs/specs/self-evolving-runtime/spec.md

## Purpose

`metrics.md` defines *what* to compute. This document defines the *shape* a
report over those metrics must take so that #706 (the shadow experiment) has
a single, stable go/no-go input, and so #710 has an unambiguous contract to
implement against without re-deciding output shape mid-implementation.

## Liveness watchdog signal

The primary "is the loop actually alive" indicator for #706 — distinct from,
and checked before, the nine ratio/cost metrics in `metrics.md`, because a
dead or stalled loop makes every ratio computed over its (empty) recent
window meaningless.

**Definition.** The loop is **alive** in a trailing window if, within the
last `N` cycles (or the last `T` hours, whichever the report is
parameterized with — see "Parameters" below), at least one of the following
is true:

- at least one done-ledger entry exists (a productive, integrated spawn), or
- at least one failure-ledger entry exists whose `reason` is *not*
  `precheck_duplicate_vs_done_ledger` (i.e. at least one genuinely-new
  proposal was attempted, even if it did not integrate), or
- at least one telemetry row (#675) with a `cycle_id` not already accounted
  for by the above exists within a shorter, tighter recency window (default:
  last `T_telemetry` hours, expected much smaller than `T` — telemetry is
  the fastest-updating signal, useful to distinguish "the bridge is still
  running cycles but hasn't reached a terminal outcome yet" from "the bridge
  process itself is not running at all").

**Inputs.** Done-ledger `ts`/recency; failure-ledger `ts`+`reason` recency;
telemetry `ts`/`cycle_id` recency. All three are read-only scans of existing
files (#704/#675), bounded by the report's window parameters — no new state.

**Parameters** (thresholds, not hardcoded — the report's `--json`/table
output must echo back whatever values it ran with, per "Report output
contract" below):

| Parameter | Meaning | Suggested default |
|---|---|---|
| `N` | cycle-count window for the done/failure-ledger check | 20 cycles |
| `T` | wall-clock window for the done/failure-ledger check | 24 hours |
| `T_telemetry` | wall-clock window for the telemetry-only fast-recency check | 2 hours |

**States.**

| State | Condition |
|---|---|
| `healthy` | At least one done-ledger entry (productive spawn) within `N`/`T`, AND `duplicate_rate` (metrics.md §2) over the same window is not itself saturating (report parameter, suggested default: below 80%). |
| `degraded` | At least one non-duplicate failure-ledger or telemetry entry within the window (the loop is running cycles and attempting novel work), but zero done-ledger entries within `N`/`T` (no productive spawn has landed) — OR `duplicate_rate` is at/above the saturation threshold even though the loop is technically producing entries. This is the "chronic-fragility" pattern named in #702 §5 (machinery running, producing nothing productive). |
| `dead` | Zero entries of any kind (done, failure, or telemetry) within `T`/`T_telemetry` — the bridge process is not completing cycles at all. |

**Why this shape.** `dead` catches the case where the bridge itself stopped
running (crash, lock permanently stuck, host down) — a pure absence-of-data
signal that none of the nine ratio metrics can distinguish from "healthy but
computed over a genuinely empty window" on their own (per each metric's
"empty window → `n/a`" edge case in `metrics.md`). `degraded` catches the
specific historical failure mode #702 documents: the loop keeps running
(telemetry/failure-ledger activity exists) but never produces a productive,
integrated change — the exact "machinery kept getting repaired... loop
repeatedly produced zero productive spawns" pattern from #702 §5. `healthy`
is the only state in which the nine `metrics.md` ratios should be read at
face value for a go/no-go decision.

## Gate-fail reason breakdown

A categorical, counted table over the failure ledger's `stage`+`reason`
fields (#704) for the report's window — the diagnostic complement to the
scalar `duplicate_rate`/`protected_surface_rejections`/`gate_pass_rate`
metrics, showing *where* attrition concentrates.

**Shape:** one row per distinct `(stage, reason)` pair observed in the
window, plus counts and share-of-total-failures.

| stage | reason | count | share_of_failures |
|---|---|---|---|
| `precheck` | `precheck_duplicate_vs_done_ledger` | … | … |
| `precheck` | `precheck_mutation_surface_violation` | … | … |
| `precheck` | `precheck_head_not_on_main` | … | … |
| `precheck` | `precheck_dirty_tree` | … | … |
| `precheck` | `precheck_lock_not_held` | … | … |
| `gate` | `gate_failed` (generic smoke-test red, S1) | … | … |
| `gate` | `mutation_surface_violation` (S2, integration-time) | … | … |
| `gate` | `blocked_file_present` (S3, no-secret check) | … | … |
| `gate` | suite-shrink-guard trip (S4 — reason string as emitted by `_run_smoke_tests_with_shrink_guard`) | … | … |
| `gate` | gate timeout / harness exception (S1 fail-safe path) | … | … |
| `no_commit` | (no sub-reason — subagent produced no diff) | … | … |
| *(unclassified)* | a failure-ledger row with missing/unrecognized `stage` or `reason` | … | … |

Rows with zero count in the window may be omitted from the human-readable
table but **must** be present (as zero) in the `--json` output, so a
consumer diffing two windows can tell "this reason didn't occur" from "this
reason wasn't in the enum yet." The `unclassified` row is mandatory
whenever any failure-ledger row does not cleanly match one of the named
`(stage, reason)` combinations named in #704/#703 — it must never be
silently dropped or folded into an existing bucket (same principle as
`metrics.md`'s handling of unclassified rows generally).

**Ordering:** grouped by `stage` in the order precheck → gate → no_commit →
unclassified (mirroring the pipeline order proposals move through), then by
descending count within each stage group.

## Report output contract

Two output forms, both driven by the same computed data — **this section
specifies the shapes only; #710 implements the script that produces them.**

### (a) `--json` schema

Top-level shape:

```json
{
  "window": {
    "start": "2026-07-01T00:00:00Z",
    "end": "2026-07-08T00:00:00Z",
    "n_cycles": 42,
    "params": {
      "liveness_N": 20,
      "liveness_T_hours": 24,
      "liveness_T_telemetry_hours": 2,
      "duplicate_rate_saturation_threshold": 0.8,
      "intervention_persistence_cycles": 3
    }
  },
  "generated_at": "2026-07-08T12:00:00Z",
  "liveness": {
    "state": "healthy",
    "last_done_entry_ts": "2026-07-08T09:12:00Z",
    "last_failure_entry_ts": "2026-07-08T11:40:00Z",
    "last_telemetry_ts": "2026-07-08T11:58:00Z"
  },
  "metrics": {
    "genuinely_new_proposal_rate": {
      "value": 0.62, "numerator": 26, "denominator": 42, "n_window": 42
    },
    "duplicate_rate": {
      "value": 0.38, "numerator": 16, "denominator": 42, "n_window": 42
    },
    "productive_spawn_rate": {
      "value": 0.71, "numerator": 22, "denominator": 31, "n_window": 31
    },
    "gate_pass_rate": {
      "value": 0.68, "numerator": 15, "denominator": 22, "n_window": 22
    },
    "integration_rate": {
      "of_gated": {"value": 0.68, "numerator": 15, "denominator": 22, "n_window": 22},
      "of_spawned": {"value": 0.48, "numerator": 15, "denominator": 31, "n_window": 31}
    },
    "protected_surface_rejections": {
      "count": 3, "rate": 0.071, "denominator": 42, "n_window": 42
    },
    "cost_per_integrated_change": {
      "token_cost": {"value": 148500, "unit": "tokens", "numerator_total_tokens": 2227500, "denominator_integrated": 15},
      "wall_clock_cost": {"value": 640, "unit": "seconds", "numerator_total_seconds": 9600, "denominator_integrated": 15}
    },
    "harvestable_upstream_ratio": {
      "general_count": 5, "host_local_count": 8, "unclassified_count": 2,
      "value": 0.333, "denominator_total_integrated": 15
    },
    "human_intervention_needed": {
      "value": 0.024, "numerator": 1, "denominator": 42, "n_window": 42,
      "note": "stop-guard/restore-failure components pending #707 ledger fields; only persistent lock/dirty-tree proxies counted"
    }
  },
  "gate_fail_breakdown": [
    {"stage": "precheck", "reason": "precheck_duplicate_vs_done_ledger", "count": 16, "share_of_failures": 0.59},
    {"stage": "precheck", "reason": "precheck_mutation_surface_violation", "count": 3, "share_of_failures": 0.11},
    {"stage": "gate", "reason": "gate_failed", "count": 5, "share_of_failures": 0.185},
    {"stage": "no_commit", "reason": null, "count": 3, "share_of_failures": 0.11},
    {"stage": "unclassified", "reason": null, "count": 0, "share_of_failures": 0.0}
  ]
}
```

Rules on this shape:

- Any metric that is undefined per its `metrics.md` edge-case rule (empty
  window / zero denominator) reports `"value": null` with the `numerator`/
  `denominator` still shown (e.g. `0`/`0`) — never a fabricated `0` or `1`.
- `n_window` is repeated per-metric (not just once at the top level) because
  different metrics have different denominator populations (total proposals
  vs. spawned cycles vs. gated cycles vs. integrated changes) per
  `metrics.md`'s per-metric definitions — a single top-level `n_cycles`
  would obscure that.
- Every metric object states its numerator and denominator explicitly, never
  just a bare ratio, so a consumer (human or #706's automated go/no-go
  check) can re-derive and sanity-check the value without re-querying the
  ledgers.
- `human_intervention_needed` must carry a `note` (or equivalent) surfacing
  the #707 dependency called out in `metrics.md` §9 for as long as that gap
  is open, so the JSON itself documents its own partial coverage rather than
  silently reporting an under-counted number as if it were complete.

### (b) Human-readable table layout

Rendered in this order (matches the pipeline/priority order a reader should
scan in):

1. **Header** — window start/end, `n_cycles`, generated-at timestamp.
2. **Liveness** — one line: state (`HEALTHY`/`DEGRADED`/`DEAD`) plus the
   three last-seen timestamps, so it reads at a glance before any ratio.
3. **Core metrics table** — one row per metric, columns:
   `metric | value | numerator | denominator | n_window`. Rates render as
   percentages to 1 decimal place (`62.0%`); `n/a` values render literally as
   `n/a`, never `0%`. Ordered: `genuinely_new_proposal_rate`,
   `duplicate_rate`, `productive_spawn_rate`, `gate_pass_rate`,
   `integration_rate` (both sub-forms as two rows), `protected_surface_rejections`.
4. **Cost table** — `cost_per_integrated_change`'s two sub-metrics as two
   rows: `token_cost_per_integrated_change` (unit: tokens),
   `wall_clock_cost_per_integrated_change` (unit: seconds, also rendered as
   `Xm Ys` for readability).
5. **Harvest table** — `harvestable_upstream_ratio`'s three-way count
   (`general`/`host_local`/`unclassified`) plus the ratio, all four numbers
   shown (never collapsed to just the ratio, per `metrics.md` §8's edge-case
   rule).
6. **Intervention row** — `human_intervention_needed`'s value plus its
   coverage-gap note rendered inline (e.g. `2.4% (1/42) [partial coverage —
   see #707]`).
7. **Gate-fail reason breakdown** — the table from "Gate-fail reason
   breakdown" above, grouped/ordered as specified there, zero-count rows
   omitted in this human view (unlike the JSON, which keeps them).

## Non-goals

- **Read-only.** This report computes purely from existing ledgers/artifacts
  (#704's done/failure ledgers, #675 telemetry, #693 prompt dump) and the
  integration ledger's `rollback` record as already surfaced into the done/
  failure ledgers. It performs no writes anywhere.
- **No runtime/gate/loop coupling.** The report is an offline, after-the-fact
  computation. It does not run inside the bridge's cycle loop, does not
  influence a precheck/gate decision, and does not gate whether a cycle
  proceeds — it is consumed by a human or by #706's separate go/no-go
  decision process, never by `main()` itself.
- **No new state.** No new ledger, cache, or database is introduced by this
  contract; every input is a field already named in #704 (or explicitly
  flagged above as a #707 gap in an existing ledger's `reason` enum, not a
  new ledger).
- **Not an implementation.** This document specifies shapes (JSON schema,
  table columns/ordering) for #710 to build against; no script, function
  signature, or file path for the report tool itself is prescribed here
  beyond what's needed to name its inputs (the ledger/telemetry paths
  already fixed by #704/#675/#693).

## Cross-links

- `docs/changes/705-observability-metrics/metrics.md` — the nine metrics
  this report renders; every metric name, edge case, and field source cited
  above is defined there.
- `docs/changes/704-ledger-artifact-memory/design.md` — ledger file
  layout/fields/rotation this report reads.
- `docs/changes/703-safety-shell-invariants/precheck-contract.md` — P1-P3
  reason strings enumerated in the gate-fail breakdown.
- `docs/changes/702-ledger-loop-architecture-decision/decision.md` §5 — the
  "machinery running, zero productive spawns" pattern the `degraded`
  liveness state is designed to catch.
- **#706** (shadow experiment) — the consumer of this entire contract; its
  go/no-go criteria should be expressed in terms of this report's `--json`
  output fields.
- **#710** (follow-up, not started) — implements the report script(s)
  against this contract.
- **#707** (follow-up, not started) — implements the ledger write points
  this report depends on, including the `human_intervention_needed`
  coverage gap named in `metrics.md` §9.
