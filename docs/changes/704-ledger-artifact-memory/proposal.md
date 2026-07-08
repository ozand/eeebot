# Proposal: ledger/artifact memory schema

- **Issue:** #704 (loop-redesign ticket C, stacked on #702's decision and
  #703's safety-shell freeze)
- **Status:** design only; no code changed by this document
- **story_id:** docs/specs/self-evolving-runtime/spec.md

## Context

#702 ratified a **state-light / ledger-based** core loop: the LLM proposes
one bounded task per cycle against *operational/artifact truth*, not a
semantic control graph. That decision named the truth sources (done ledger,
failure ledger, integration record, prompt/response dump, telemetry) but
deliberately deferred their concrete schema to this ticket. #703 froze the
safety shell and its precheck contract, and already names one dependency
back onto this ticket: precheck check P2 (duplicate-vs-done novelty) reuses
`_task_already_done`'s git-log approximation only "until #704 lands" — after
which P2 should read the done ledger directly.

This document specifies that schema, grounded in what the runtime already
writes rather than inventing new machinery.

## Goals

- Define file layout, fields, write points, and read consumers for every
  ledger/artifact the loop's context-builder, precheck, harvest, and
  observability need.
- Make explicit which of the five ledgers named in #702 are **already
  implemented** (integration record, prompt dump, telemetry) versus
  **net-new** (done ledger, failure ledger).
- Specify a retention/compaction policy for the net-new ledgers consistent
  with the existing #693 gzip+retention pattern, so the constrained host's
  on-disk growth stays bounded.
- Confirm the schema is sufficient, on paper, for #705's metrics (novelty
  rate, integration rate, harvest yield) and for the context-builder's
  "avoid already-done work" need — without adding anything beyond that.

## Non-goals

- **Not a control graph.** No reward, lane/experiment assignment, discard
  state, HADI, or stall-switch field is reintroduced here, in any ledger. Per
  #702 §2, any of that surviving at all is optional, out of scope for this
  ticket (#708), and must stay passive analytics that never gates liveness.
- **No code.** This is a schema/contract document. Implementation of the
  ledgers (writers, readers, rotation) belongs to #707 (and, for the shadow
  experiment's read path, #706); this ticket does not touch `nanobot/`,
  `scripts/`, `tests/`, or `docs/specs/*`.
- **Not redesigning the integration record, prompt dump, or telemetry.**
  Those three already exist (#653/#666 rollback record, #693, #675) and work;
  this document maps to them and calls out at most a thin surfacing need, it
  does not change their fields or storage.
- **Not the metrics/dashboard.** #705 defines what is computed from these
  ledgers; this ticket only guarantees the raw fields exist.

## Sequencing

- Depends on #702 (direction) and #703 (the P2 precheck contract that will
  consume the done ledger).
- A **minimal form** of the done + failure ledgers (write path can be as
  simple as "append one JSON line per cycle outcome") is a prerequisite for:
  - **#705** (metrics) — novelty rate, integration rate, and harvest yield are
    all computable only if done/failure entries exist per cycle.
  - **#706** (shadow experiment) — needs to record LLM-proposed-task outcomes
    somewhere durable to compare against the current planner.
  - **#707** (replacement core loop implementation) — gated on #706, but its
    context-builder and precheck P2 both read the done/failure ledgers this
    document defines.
- This ticket does not gate #702/#703 (already settled) and is not itself
  gated on anything beyond them.

## Risks

- **Schema churn**: mitigated by deriving every field from a concrete named
  consumer (#705's metrics, #703's P2, harvest #672) rather than speculative
  fields — see the "existing vs net-new" table and field lists in
  `design.md`.
- **Duplicating existing artifacts**: mitigated by explicitly mapping three
  of the five ledgers to code that already exists (cited by path/function in
  `design.md`) instead of re-specifying them.
