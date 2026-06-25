# Base Configuration Profile

Last updated: 2026-03-28 UTC

## Purpose

This document defines the portable baseline configuration for deploying the
`eeepc` system on similar weak hosts.

The purpose of the baseline is not to capture every host-specific detail.
It is to define the smallest trustworthy starting point from which the system can:

- boot,
- operate safely,
- evolve within policy,
- recover after failure,
- and be redeployed on another comparable machine.

## Portability Target

The target host class is:

- old or weak consumer hardware,
- constrained CPU and memory,
- small or fragile storage budgets,
- unreliable or limited network conditions,
- Debian-like Linux environments or similarly minimal Unix-like hosts.

This document should favor host-class portability over one-machine convenience.

## Baseline Goals

The base profile should be:

- lightweight,
- deterministic,
- inspectable,
- recoverable,
- reproducible,
- safe by default.

## Base Invariants

The following should remain stable across similar host deployments unless there is
an explicit architectural reason to change them:

- canonical source and evidence remain separate,
- host-local state is not treated as canonical source,
- runtime paths and state surfaces are documented,
- bootstrap steps are replayable,
- resource ceilings are considered first-class constraints,
- risky capabilities remain policy-gated,
- important self-improvement leaves evidence.

## What Belongs In The Baseline

The baseline should define:

- minimum runtime dependencies,
- required directory and state layout,
- default config and policy files,
- logging and retention defaults,
- health and smoke-check expectations,
- capability gating defaults,
- sync/export expectations,
- rollback and rebuild assumptions.

## What Should Not Be Embedded In The Baseline

Do not bake in:

- undocumented host-specific hacks,
- owner-specific one-off outputs,
- large optional subsystems that weak hosts cannot afford,
- secrets in source-controlled files,
- assumptions that only one exact machine layout can work.

## Host Profiles And Overlays

The system should separate:

- base profile invariants,
- host-specific overlays,
- runtime-discovered local facts.

Examples of host overlays may include:

- filesystem path differences,
- available devices,
- channel enablement,
- network restrictions,
- service-user and permission models,
- tighter or looser resource budgets.

Host overlays should be explicit and documented, not accidental.

## Resource Budget Expectations

Every deployment should define bounded expectations for:

- startup cost,
- idle memory use,
- background cycle cost,
- disk growth,
- log retention,
- failure-mode degradation.

If a capability cannot fit inside the target host class with a bounded budget, it
is not part of the portable baseline yet.

## Minimal Runtime Contract

After baseline deployment, the system should be able to:

- start reliably,
- expose basic operator control,
- inspect its own state and code,
- report capabilities truthfully,
- record evidence,
- survive restart without losing architectural integrity.

## Validation Before Promotion

Before promoting a baseline change, verify:

1. it remains compatible with the target weak-host class,
2. bootstrap docs still work,
3. recovery paths remain valid,
4. no new hidden host-specific dependency was introduced,
5. the change improves or preserves clarity, portability, and recoverability.

## Relationship To Runtime Evolution

The baseline is not the final state of the running system.

It is the trusted starting point from which the running system may evolve.

That means:

- the baseline should stay smaller and cleaner than the full lived host state,
- host-local growth should remain attributable and recoverable,
- redeploying the baseline on a fresh similar host should still be possible.

## Practical Rule

When deciding whether something belongs in the baseline, ask:

- does this help another similar weak host start safely from scratch,
- or does it only preserve an accident of the current machine.

If it is only preserving an accident, it probably does not belong in the base
configuration profile.
