# Fast-Track Autonomy Plan

Status: planning document. Use runtime state, issues, and operator surfaces for current execution truth.

Last updated: 2026-03-28 UTC

## Purpose

This document defines the shortest safe path from the current metadata-heavy
autonomy stack to a bot that can perform useful real autonomous actions on weak
hardware.

The guiding principle is:

do not unlock generic execution first.
Unlock a small, truthful, bounded action lane first.

## Why This Plan Exists

The project already has a strong governance and provenance spine.

What it lacks is a minimal real executor surface that can:

- inspect the host truthfully,
- perform a small number of useful actions,
- mutate bounded local surfaces,
- produce evidence for every action,
- report honestly what happened.

This plan optimizes for shipping that capability quickly.

## Strategy

The fast-track path is:

1. define a strict action registry,
2. make execution diagnostics-first,
3. enforce truthfulness and evidence,
4. open one bounded mutation lane,
5. add a lightweight autonomous loop,
6. expand only after the first lane proves stable.

## Non-Negotiable Safety Minimums

These are hard requirements, not tuning goals.

- allowlist-only actions,
- diagnostics before mutation,
- workspace-bounded scope by default,
- one action attempt must always leave evidence,
- no success claims without evidence,
- fail closed on unknown or ambiguous actions,
- no generic shell as the primary executor surface,
- no canonical-source mutation in the first autonomy lane,
- post-action verification for writes,
- explicit stop and rollback guidance.

## What To Defer For Speed

The following should not block the first useful autonomy slice:

- broad plugin or tool ecosystems,
- advanced memory tuning,
- broad device or sensor interfaces,
- heavy optimization or ML-guided planning.

Do not defer the guarded autonomous loop once it exists.
After the guarded loop is implemented, it becomes the default managed path for self-mutation on this host.

## Fast-Track Phases

### Phase 0 - Executor contract

Goal:

- freeze the shape of the first executor lane.

Deliverables:

- action registry contract,
- action policy classes,
- action lifecycle definition,
- denial and stop conditions.

Exit criteria:

- executor semantics are explicit enough to implement without improvisation.

### Phase 1 - Diagnostics-first executor core

Goal:

- create the first real executor path without opening generic execution.

Deliverables:

- `ActionRegistry`,
- `ActionSpec`, `ActionResult`, and `ActionDiagnostic`,
- diagnostics-first execution pipeline,
- hard fail-closed routing for unknown actions.

Exit criteria:

- the system can execute several useful read-only actions through the registry.

### Phase 2 - First useful read-only actions

Goal:

- make the bot immediately useful without mutation.

Recommended first actions:

- `workspace.inventory`,
- `policy.snapshot`,
- `capability.truth-check`,
- `roadmap.trace`,
- `runtime.diagnostics_snapshot`,
- `regression.probe`.

Exit criteria:

- the bot can answer meaningful operational questions using real runtime data.

### Phase 3 - Evidence and truth wiring

Goal:

- ensure every action leaves a truthful, durable trail.

Deliverables:

- structured action request/decision/result records,
- evidence refs for both success and failure,
- refusal reasons,
- verification results.

Exit criteria:

- the bot cannot report completion without an evidence-backed result.

### Phase 4 - Bounded mutation lane v1

Goal:

- allow one small class of real local changes.

Recommended first mutation actions:

- `evidence.pack`,
- `doc.patch.propose`,
- `test.patch.bounded`,
- later `workspace.apply_patch`.

Exit criteria:

- the bot can make one real bounded local change safely and verify it.

### Phase 5 - Autonomous loop coordinator

Goal:

- let the bot choose, diagnose, execute, verify, and report on a bounded action
  without continuous human steering.

Exit criteria:

- the bot can complete one end-to-end bounded autonomous cycle truthfully.

### Execution discipline after guarded loop exists

Once commit/push -> candidate -> apply -> health -> rollback exists, the system must use that path as the normal autonomous mutation path.
A summary is not a valid stopping point while actionable bounded work remains.
Every status/proof update must include:
- current time,
- what is being done now,
- what is delegated now,
- and must not end on conditional continuation language.

### Phase 6 - Launch criteria and controlled expansion

Goal:

- decide when the action set may widen.

Deliverables:

- launch checklist,
- regression probes,
- weak-host budget checks,
- rollback expectations.

Exit criteria:

- expansion decisions are based on evidence, not optimism.

## Action Registry v1

### Read-only actions

- `workspace.inventory`
- `policy.snapshot`
- `capability.truth-check`
- `roadmap.trace`
- `runtime.diagnostics_snapshot`
- `regression.probe`

### Bounded-mutation actions

- `evidence.pack`
- `doc.patch.propose`
- `test.patch.bounded`
- `workspace.apply_patch` (later in v1, after proof)

## Definition Of Success

The fast-track autonomy plan succeeds when the bot can:

1. inspect runtime state truthfully,
2. choose an allowlisted action,
3. run diagnostics before acting,
4. execute one bounded real action,
5. verify the result,
6. emit evidence,
7. report honestly what happened,
8. stop safely when policy, budget, or evidence is insufficient.

## Recommended Priority Shift

Raise in priority:

- truthfulness enforcement,
- Telegram live reality checks,
- diagnostics-first executor work,
- weak-host budget minimums,
- bounded mutation lane.

Lower in priority:

- further deepening of metadata-only promotion layers,
- broad schema hardening not tied to executor usefulness,
- device/world interfaces,
- advanced self-improvement sophistication.

## Immediate Next Step

Status note: this section records the plan-stage next step at time of writing; it should not be treated as a live execution indicator.

The next implementation step should be:

- define the executor contract and action registry v1,
- then implement the diagnostics-first executor core,
- then enable a first read-only useful action set.
