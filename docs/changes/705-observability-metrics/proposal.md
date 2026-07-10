# Proposal: loop-health observability metrics + report contract

- **Issue:** #705 (loop-redesign ticket E, stacked on #702's decision, #703's
  safety-shell freeze, and #704's ledger schema)
- **Status:** design-only; no code changed by this document. Report scripts
  that implement this contract are follow-up **#710**; the ledger write
  points these metrics read from are follow-up **#707** (some fields already
  exist today via #675/#693; the done/failure ledgers themselves are #704's
  design, written by #707).
- **story_id:** docs/specs/self-evolving-runtime/spec.md

## Problem

#702 ratified a state-light, ledger-based core loop, replacing a
control-plane planner that suffered ~7 sequential fixes without converging
(#656, #664, #690, #695, #697, #700) — the loop kept collapsing to
already-done proposals and produced zero productive spawns for extended
periods, invisibly, because there was no queryable go/no-go signal for "is
the loop actually alive and doing new work." #706's shadow experiment will
run the LLM-proposal path side by side with the current planner and needs a
**concrete, computable set of health metrics** to decide, objectively,
whether the new path clears its go/no-go bar. Today there is no metric
catalog and no report contract — only ad hoc per-result-file inspection.

This document defines that catalog and contract, computed purely from
artifacts #704 already specifies (the done/failure ledgers) and telemetry
that already exists (#675 `record_llm_call`, #693 `record_llm_prompt`),
without inventing new runtime state.

## Deliverables

1. **`metrics.md`** — a catalog of nine named metrics
   (`genuinely_new_proposal_rate`, `duplicate_rate`, `productive_spawn_rate`,
   `gate_pass_rate`, `integration_rate`, `protected_surface_rejections`,
   `cost_per_integrated_change`, `harvestable_upstream_ratio`,
   `human_intervention_needed`). Each metric specifies its definition,
   numerator/denominator (or aggregation, for non-ratio metrics), the exact
   ledger/field source, edge-case handling, and why it matters for #706's
   go/no-go decision.
2. **`report-spec.md`** — the report **output contract only**: a liveness
   watchdog signal (the primary "is the loop alive" indicator for #706), a
   gate-fail reason breakdown table, and both a `--json` schema and a
   human-readable table layout that a follow-up script (#710) implements and
   that #706 consumes to make its go/no-go call. No script is written here.

## Non-goals

- No code, scripts, or runtime changes (see `report-spec.md`'s non-goals
  section for the full list).
- No new ledger fields beyond what #704 already specifies; any input this
  document needs that #704 does not yet provide is flagged explicitly as a
  dependency on #707 (ledger write-up) or #710 (report implementation)
  rather than invented here.
- No redesign of #675/#693 telemetry, or of #702/#703's frozen shell.

## Cross-links

- `docs/changes/702-ledger-loop-architecture-decision/decision.md` —
  architecture direction; names #705 as a prerequisite for #706.
- `docs/changes/703-safety-shell-invariants/precheck-contract.md` — P1-P3
  precheck outcomes this document's failure-ledger-derived metrics count.
- `docs/changes/704-ledger-artifact-memory/design.md` — the done/failure
  ledger schema and field definitions this document's metrics are computed
  from; also its own "Coverage check — #705 metrics" section, which this
  document supersedes with the full nine-metric catalog.
- **#706** (shadow experiment) — the consumer: uses the liveness signal and
  the nine metrics as its go/no-go criteria.
- **#710** (follow-up, not started) — implements the report script(s)
  against `report-spec.md`'s contract.
- **#707** (follow-up, not started) — implements the ledger write points
  (#704's schema) these metrics read from.
