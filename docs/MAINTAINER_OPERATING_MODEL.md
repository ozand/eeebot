# Maintainer Operating Model

Last updated: 2026-03-28 UTC

## Purpose

This document defines the role of project maintainers for the `eeepc` workstream.

It exists to keep one boundary clear:

maintainers are architects and orchestrators of the base system.
They are not, by default, manual producers of the owner-facing outputs that the
running system should eventually learn to generate for itself.

## Mission Of Maintainers

Maintainers are responsible for shaping the base system so it can:

- start safely,
- survive on weak hardware,
- evolve within bounded rules,
- recover after damage or drift,
- be redeployed on similar hosts,
- generate useful outputs for the owner through runtime behavior.

## Maintainer Responsibilities

Maintainers should:

- define and preserve system invariants,
- design core runtime and policy boundaries,
- control canonical source and promotion rules,
- maintain bootstrap and recovery paths,
- validate portability to similar weak hosts,
- inspect failure modes and architectural drift,
- inject new technical solutions into the base system when needed,
- decide when partial repair is enough and when a clean restart from baseline is
  the better path.

## What Maintainers Should Not Default To

Maintainers should not treat themselves as the primary runtime of the system.

That means they should not default to:

- manually crafting owner-facing dashboards,
- manually producing recurring utilities that the runtime should learn to create,
- treating one-off host fixes as the long-term architecture,
- preserving broken autonomous state just because it already exists,
- carrying hidden host-specific knowledge outside the documented baseline.

## Runtime Responsibilities

The running system should increasingly own:

- owner-facing output generation,
- bounded tool growth,
- local optimization loops,
- research ingestion support,
- iterative improvement of useful artifacts,
- host adaptation within allowed policy.

The maintainer's job is to make those behaviors possible, observable, bounded,
portable, and recoverable.

## Operating Loop

The maintainer loop is:

1. inspect current architecture, runtime state, and evidence,
2. identify drift, bottlenecks, or self-evolution failures,
3. decide whether to repair, constrain, redesign, or rebuild,
4. update baseline code, policy, bootstrap, or recovery paths,
5. validate on the target host class,
6. preserve documentation and promotion discipline.

## Decision Rights

Maintainers own decisions about:

- architecture,
- policy,
- bootstrap order,
- recovery and rollback strategy,
- canonical source promotion,
- host-profile baselines,
- what classes of self-modification are allowed.

The runtime system may make bounded local decisions only within the policy
surface the maintainers define.

## When To Repair vs Rebuild

Repair is preferred when:

- the failure is local and understood,
- recovery does not deepen architectural drift,
- the base invariants still hold.

Rebuild from baseline should be preferred when:

- the runtime has self-damaged beyond trustworthy local repair,
- state or policy drift makes current behavior non-explainable,
- bootstrap assumptions are no longer reproducible,
- continued patching would hide systemic design flaws.

## Success Metrics For Maintainers

Maintainer success is not measured by how many outputs they produce manually.

It is measured by whether the base system becomes:

- safer,
- more portable,
- more recoverable,
- more truthful,
- more capable of self-improvement,
- more useful to the owner through runtime behavior.

## Required Documents For Maintainers

Maintainers should keep these surfaces coherent:

- `docs/PROJECT_CHARTER.md`
- `docs/ROADMAP_EPICS.md`
- `docs/HOST_CAPABILITY_POLICY.md`
- `docs/BASE_CONFIGURATION_PROFILE.md`
- `docs/SAFE_BOOTSTRAP_FROM_SCRATCH.md`
- `docs/SOURCE_OF_TRUTH_AND_PROMOTION_POLICY.md`

## Practical Rule

When in doubt, ask:

- am I improving the base system,
- or am I compensating manually for something the base system should learn to do?

If the answer is the second one, the likely right move is to improve the base
system instead.
