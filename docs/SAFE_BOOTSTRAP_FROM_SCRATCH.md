# Safe Bootstrap From Scratch

Last updated: 2026-03-28 UTC

## Purpose

This document defines the expected bootstrap path for bringing the `eeepc`
system up from a fresh, reset, or no-longer-trusted host state.

It exists because the system must be able to recover not only from ordinary
errors, but also from architectural drift, broken autonomy, and partial
self-damage.

## Bootstrap Goal

Reach a minimal trusted runtime that can:

- accept operator control,
- inspect itself and its host,
- report capabilities truthfully,
- evolve only within bounded policy,
- preserve evidence for later improvement and promotion.

## When To Use This Path

Use this bootstrap path when:

- deploying to a fresh similar weak host,
- rebuilding after severe runtime corruption,
- recovering from non-trustworthy self-modification,
- validating that the system remains portable,
- proving that canonical source plus baseline configuration is enough to restart.

## Bootstrap Principles

- prefer minimal trusted bring-up over feature completeness,
- verify each stage before unlocking the next one,
- do not assume hidden local state is trustworthy,
- separate canonical source, runtime state, and exported evidence,
- keep rollback and restart possible at every major stage.

## Stage 0 - Host Qualification

Confirm that the host is within the supported weak-host class:

- OS family is compatible,
- basic storage is available,
- required permissions and service model are understood,
- network assumptions are known,
- critical host paths are documented.

If the host does not satisfy minimum assumptions, stop and record the mismatch.

## Stage 1 - Canonical Source And Baseline Bring-Up

Restore or acquire:

- canonical source,
- baseline configuration,
- required dependencies,
- documented directory/state layout.

At this stage, the goal is not full autonomy.
The goal is a clean and explainable base.

## Stage 2 - Minimal Trusted Runtime

Bring up only the minimum runtime surface needed to:

- start the core agent path,
- accept direct operator requests,
- inspect internal state,
- emit basic health or status information.

Do not unlock broad self-modification yet.

## Stage 3 - Introspection Before Mutation

Before allowing meaningful self-edit behavior, confirm the system can inspect:

- its own code,
- its prompts and config,
- runtime files and services,
- host resource conditions,
- available tools and device surfaces.

The system should explain what it can see before it is allowed to change much.

## Stage 4 - Bounded Self-Change Enablement

Enable only explicit low-risk mutation zones first, such as:

- prompts,
- selected config,
- bounded docs,
- reporting and evidence surfaces,
- approved lightweight helper tools.

Keep high-risk or system-level changes review-gated.

## Stage 5 - Evidence, Sync, And Promotion Bring-Up

Confirm that the system can:

- record cycle evidence,
- retain recovery-relevant history,
- separate evidence from canonical source,
- produce promotion candidates without blindly promoting them.

This stage is required before deeper autonomy should be trusted.

## Stage 6 - Broader Autonomous Growth

Only after earlier stages are stable should the system expand toward:

- richer self-improvement loops,
- runtime-generated owner-facing outputs,
- device-aware tooling,
- broader research transfer,
- project bootstrap beyond the local host mutation lane.

## Recovery And Rollback Rule

If the system becomes non-explainable, non-recoverable, or no longer trustworthy:

- stop relying on current mutated runtime state,
- fall back to canonical source and baseline configuration,
- restore the minimal trusted runtime,
- replay only what evidence and policy justify.

The system should never require blind trust in damaged local state.

## Verification Checklist

Before considering bootstrap successful, confirm:

1. core runtime starts cleanly,
2. operator control works,
3. capability reporting is truthful,
4. baseline paths and state surfaces are consistent,
5. restart does not destroy integrity,
6. evidence can be written or exported,
7. bounded mutation remains bounded.

## Practical Outcome

The end state of bootstrap is not a fully evolved system.

It is a trustworthy seed state from which safe self-development can begin again.
