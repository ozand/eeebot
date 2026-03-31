# Roadmap Epics

Last updated: 2026-03-28 UTC

## Purpose

This document translates the project charter into concrete epics that can be
tracked, prioritized, and broken into implementation tasks.

Primary reference:

- `docs/PROJECT_CHARTER.md`

## Epic Index

- `PC-EPIC-001` Weak-Host Runtime Fitness
- `PC-EPIC-002` Truthful Capability Surface
- `PC-EPIC-003` Self-Evolution Control Plane
- `PC-EPIC-004` Memory For Constrained Agents
- `PC-EPIC-005` Tool Growth And Local Agency
- `PC-EPIC-006` Device And World Interfaces
- `PC-EPIC-007` Research Transfer Pipeline
- `PC-EPIC-008` Evidence, Sync, And Promotion
- `PC-EPIC-009` Autonomous Project Creation
- `PC-EPIC-010` Owner Utility, Interfaces, And Creative Artifacts
- `PC-EPIC-011` Portable Base Configuration And Host Profiles
- `PC-EPIC-012` Safe Bootstrap, Recovery, And Rebuild

## PC-EPIC-001 Weak-Host Runtime Fitness

Goal:

- make the system stable, efficient, and recoverable on old `eeepc` hardware.

Key themes:

- memory ceilings,
- CPU-aware scheduling,
- bounded background work,
- disk retention and compaction,
- low-cost startup and idle behavior,
- degraded-mode survivability.

Acceptance signals:

- the system can run for long periods without uncontrolled resource growth,
- background cycles do not starve operator requests,
- logs and state stay compact enough for the target host,
- failure and restart paths are predictable.

## PC-EPIC-002 Truthful Capability Surface

Goal:

- make every capability claim match live runtime reality.

Key themes:

- shared capability snapshot,
- free-form answer grounding,
- mode/policy/tool distinction,
- simulator and live regression probes,
- stale-memory rejection.

Acceptance signals:

- `/cap_status` and natural-language answers agree,
- the system distinguishes unavailable vs blocked vs unverified,
- historical context is not presented as live fact without revalidation.

## PC-EPIC-003 Self-Evolution Control Plane

Goal:

- make self-improvement bounded, structured, and reviewable.

Key themes:

- cycle lifecycle,
- proposal/apply/evaluate/report flow,
- lane isolation,
- improvement metadata,
- policy-gated mutation.

Acceptance signals:

- each cycle has a clear outcome and evidence trail,
- bounded self-changes can be evaluated and rolled forward safely,
- operator-priority work remains responsive.

## PC-EPIC-004 Memory For Constrained Agents

Goal:

- improve continuity and learning without prompt or state explosion.

Key themes:

- short-term working memory,
- compact summaries,
- anti-repeat guards,
- host-state memory,
- owner-preference memory,
- retention and aging.

Acceptance signals:

- the system repeats itself less,
- prompt growth remains bounded,
- useful past context is recoverable at low cost.

## PC-EPIC-005 Tool Growth And Local Agency

Goal:

- let the system build and refine small tools that improve local capability.

Key themes:

- diagnostics,
- optimization helpers,
- parsers and research tools,
- code analysis helpers,
- bounded automation,
- tool quality evaluation.

Acceptance signals:

- repeated friction leads to reusable tools,
- new tools are testable and lightweight,
- tool growth increases capability without runaway complexity.

## PC-EPIC-006 Device And World Interfaces

Goal:

- expose the host's physical and connectivity surfaces as bounded agent interfaces.

Key themes:

- camera,
- microphone,
- Bluetooth,
- Wi-Fi and network diagnostics,
- local media ingestion,
- permission and privacy boundaries.

Acceptance signals:

- the system can inventory host devices,
- safe interfaces exist for useful device interaction,
- device usage is policy-bounded and observable.

## PC-EPIC-007 Research Transfer Pipeline

Goal:

- convert `T:\Code\agents_library\` from passive reference material into a steady
  source of adoptable patterns.

Key themes:

- research indexing,
- pattern extraction,
- ranking by host fit,
- adaptation templates,
- implementation tracking,
- rejection reasoning.

Acceptance signals:

- there is a repeatable path from research note to experiment to adoption,
- adopted patterns are traceable to source research,
- heavyweight or low-value ideas are filtered out early.

## PC-EPIC-008 Evidence, Sync, And Promotion

Goal:

- keep self-evolution accountable and promotable.

Key themes:

- cycle manifests,
- evidence export,
- workspace export,
- promotion candidates,
- namespace and branch policy,
- recovery history.

Acceptance signals:

- important host-born changes leave durable evidence,
- promotion into canonical repos is reviewable,
- sync planes remain separated from canonical source ownership.

## PC-EPIC-009 Autonomous Project Creation

Goal:

- let the system create new repos/projects when repeated work no longer fits in a
  bounded host-mutation lane.

Key themes:

- project seed manifests,
- repo bootstrap,
- projects index metadata,
- lifecycle tracking,
- governance boundaries.

Acceptance signals:

- new projects have explicit seeds and lineage,
- hidden complexity is not buried inside evidence repos,
- new project creation remains auditable.

## PC-EPIC-010 Owner Utility, Interfaces, And Creative Artifacts

Goal:

- ensure the system grows in ways that are visibly useful and interesting to the
  owner/operator, while keeping maintainers focused on capability enablement
  rather than manual output production.

Key themes:

- runtime-generated dashboards,
- operator interaction layers,
- workflow utilities,
- creative artifact generation,
- constrained-host games or graphics,
- demoscene-style experiments,
- owner-feedback-driven refinement,
- evaluation loops for owner-facing outputs.

Acceptance signals:

- the system produces owner-facing outputs that are actually used or valued,
- the operator experience becomes richer over time,
- created artifacts can be evaluated and iterated on,
- creative exploration remains compatible with weak-host constraints.

## PC-EPIC-011 Portable Base Configuration And Host Profiles

Goal:

- define a reusable, weak-host-friendly baseline that can be deployed across
  similar machines.

Key themes:

- base invariants,
- host overlays,
- portable path assumptions,
- resource budgets,
- dependency minimization,
- deployment validation across similar host profiles.

Acceptance signals:

- the baseline is explainable and reproducible,
- similar weak hosts can adopt the system with limited manual divergence,
- host-specific behavior is documented as overlay rather than hidden drift.

## PC-EPIC-012 Safe Bootstrap, Recovery, And Rebuild

Goal:

- guarantee the system can be brought up from scratch and recovered after severe
  drift, corruption, or bad self-evolution outcomes.

Key themes:

- minimal trusted runtime,
- staged capability unlocks,
- bootstrap verification,
- rebuild drills,
- rollback and replay rules,
- canonical-source-plus-evidence recovery model.

Acceptance signals:

- the system can be rebuilt from a known-good baseline,
- severe runtime drift does not trap the project in irrecoverable state,
- recovery procedures are documented, testable, and reviewable.

## Suggested Priority Bands

### Near-term

- `PC-EPIC-001` Weak-Host Runtime Fitness
- `PC-EPIC-002` Truthful Capability Surface
- `PC-EPIC-003` Self-Evolution Control Plane
- `PC-EPIC-008` Evidence, Sync, And Promotion
- `PC-EPIC-011` Portable Base Configuration And Host Profiles
- `PC-EPIC-012` Safe Bootstrap, Recovery, And Rebuild

### Mid-term

- `PC-EPIC-004` Memory For Constrained Agents
- `PC-EPIC-005` Tool Growth And Local Agency
- `PC-EPIC-007` Research Transfer Pipeline
- `PC-EPIC-010` Owner Utility, Interfaces, And Creative Artifacts

### Longer-term

- `PC-EPIC-006` Device And World Interfaces
- `PC-EPIC-009` Autonomous Project Creation

## Planning Rule

When adding tasks to `todo.md` or other project backlogs, prefer mapping them to
one of these epic IDs so short-term work stays connected to the long-term project
charter.

For `PC-EPIC-010`, prefer tasks that improve the system's ability to generate
owner-facing outputs rather than tasks that rely on maintainers manually producing
those outputs outside the runtime.
