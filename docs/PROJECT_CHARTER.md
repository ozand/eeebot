# Project Charter

Last updated: 2026-03-28 UTC

## Purpose

This document defines the mission, architecture principles, development direction,
and target capability model for the `eeepc` project built on top of `nanobot`.

It exists to make one thing explicit:

`eeepc` is not only a fork of `nanobot`.
It is a resource-aware, self-evolving agent system designed to live on weak,
real hardware and improve itself under controlled, reviewable conditions.

## Core Mission

Build a version of `nanobot` that can:

1. run reliably on a weak `eeepc` host with constrained CPU, memory, disk, and I/O,
2. adapt its own behavior to that host instead of assuming server-class resources,
3. study agent-system best practices and selectively transfer them into itself,
4. improve its code, tools, prompts, memory, and runtime behavior over time,
5. produce useful value for its owner/operator through practical assistance,
   interfaces, tools, and artifacts,
6. do all of the above without losing operator oversight, evidence, or recovery.

## Project Thesis

The weak host is not an inconvenience.
It is a design constraint and a research asset.

The goal is not to build the biggest autonomous agent.
The goal is to build the most effective agent we can on constrained hardware,
using disciplined engineering, bounded autonomy, and continuous self-improvement.

## Problem Statement

Most agent systems are designed for cloud-scale assumptions:

- abundant RAM,
- abundant CPU,
- always-on external services,
- permissive deployment environments,
- heavy frameworks and orchestration layers.

`eeepc` intentionally rejects those assumptions.

This project asks a different question:

Can a lightweight agent running on old consumer hardware become more capable over
time by understanding its own constraints, studying better patterns, and
improving itself safely?

## System Context

The project has four interacting surfaces.

### 1. `nanobot` engine repo

Path:

- `T:\Code\eeepc`

Purpose:

- canonical engine repo,
- operator-facing assistant runtime,
- chat/gateway logic,
- tools, providers, channels, memory, and loop behavior.

### 2. `eeepc` control-plane repo

Path:

- `T:\Code\servers_team\Project\servers\eeepc`

Purpose:

- host-native runtime policy,
- self-evolving execution plane,
- deployment and operational contracts,
- evidence/export/promotion control surfaces.

### 3. live host evolution plane

Examples:

- host state,
- bounded mutable workspaces,
- cycle reports,
- evidence manifests,
- outbox and simulator data,
- local experiments and temporary self-improvement artifacts.

Purpose:

- let the running system evolve and observe itself,
- without confusing host-local mutation with canonical product source.

### 4. external research library

Path:

- `T:\Code\agents_library\`

Purpose:

- research input for agent patterns,
- source of memory/tooling/self-improvement ideas,
- transfer surface for best practices that can be adapted to weak hardware.

## Canonical Project Definition

`eeepc` is a resource-native, operator-supervised, self-evolving agent system
whose engine starts from `nanobot`, whose runtime is shaped by a weak Debian host,
and whose long-term development is guided by evidence-backed self-improvement.

## Owner Utility Requirement

The system does not exist only to preserve itself or optimize its own internals.

It must become more useful to its owner/operator over time.

That usefulness may take several forms:

- practical help with engineering, research, and daily workflows,
- operator-facing terminal dashboards, status views, and visualizations,
- small utilities discovered or built through agent research,
- creative artifacts such as graphics, experiments, games, or demoscene-style work,
- iterative improvement of existing tools based on owner feedback and repeated use.

Self-improvement is therefore justified not only by internal efficiency gains, but
also by increased owner value, delight, and long-term usefulness.

## Maintainer Role Doctrine

Project maintainers are architects and orchestrators of the base system.

Their primary responsibility is to:

- design the core runtime and policy boundaries,
- preserve canonical ownership and promotion discipline,
- create a safe and portable base configuration,
- keep bootstrap, recovery, and redeployment paths healthy,
- enable the running system to become more capable over time.

Maintainers are not, by default, the manual producers of owner-facing dashboards,
artifacts, games, visualizations, or other outputs.

Instead, maintainers should build the system that can eventually produce those
outputs for the owner under policy, evidence, and constrained-host rules.

## Portable Base-System Objective

This project is not only about one host.

The long-term objective is to create a base configuration that can be deployed on
other similar weak hosts and safely grow from a minimal starting point.

That means the project should separate:

- base invariants that must travel across hosts,
- host-local overlays and tunings,
- runtime-generated state,
- evidence and promotion surfaces.

The more the system depends on undocumented host-specific accidents, the less it
meets the project objective.

## Success Criteria

The project is successful if it can consistently do the following:

- stay operational on weak hardware without collapsing under resource pressure,
- answer operators truthfully about its real runtime capabilities,
- inspect and improve parts of itself under bounded policy,
- preserve evidence for what changed and why,
- promote accepted improvements back into canonical source repos,
- create new tools or project surfaces when justified by repeated need,
- create useful owner-facing interfaces, utilities, or artifacts when justified,
- make those owner-facing outputs increasingly producible by the runtime system,
- become more effective over time without becoming architecturally bloated.

## Non-Goals

The project is not trying to:

- become a heavy cloud-only orchestration platform,
- import large frameworks just because they are fashionable,
- maximize model size at the expense of host survivability,
- allow unconstrained self-modification,
- treat host-local edits as canonical truth,
- replace review, policy, or recovery with blind autonomy.

## Core Architectural Principles

### P1 - Resource-first design

Every major design decision must be evaluated against weak-host reality:

- memory footprint,
- CPU usage,
- startup cost,
- steady-state background cost,
- storage growth,
- recovery cost after failure.

If a feature is powerful but too expensive for the target host, it is not yet a
fit for this project.

### P2 - Port patterns, not platform

External systems such as `claw0` and other agent research sources should inform
design through patterns, not wholesale adoption.

Examples:

- durable queues,
- retry and resilience wrappers,
- memory compaction,
- prompt layering,
- execution lanes,
- bounded tool growth.

Avoid importing heavyweight architectures that conflict with constrained-host
operation.

### P3 - Host embodiment

The host is part of the agent's working world.

The agent should be able to inspect and reason about:

- OS characteristics,
- process and memory pressure,
- disk usage,
- network surfaces,
- available devices,
- runtime files,
- deployment and service state,
- its own code and instructions.

This does not mean unlimited mutation.
It means the host is a legitimate subject of agent perception and adaptation.

### P4 - Bounded self-improvement

The agent is allowed to improve itself only within explicit safety boundaries.

Allowed categories may include:

- prompts,
- selected config surfaces,
- allowlisted code targets,
- tools and helper scripts,
- diagnostics,
- documentation,
- evidence/report generation.

High-risk areas must remain review-gated.

### P5 - Evidence before promotion

Host-born change is real, but provisional.

The default lifecycle is:

1. observe,
2. analyze,
3. change in bounded scope,
4. record evidence,
5. evaluate result,
6. promote only if accepted.

No important self-improvement should become canonical source without a reviewable
trail.

### P6 - Truthful runtime introspection

The agent must distinguish between:

- currently available,
- unavailable,
- blocked by policy,
- not yet verified,
- historically known but not revalidated.

This principle applies to free-form answers as much as to explicit status
commands.

### P7 - Operator priority and recoverability

Direct operator requests must outrank background self-improvement work.

The system must remain:

- interruptible,
- inspectable,
- restart-tolerant,
- debuggable,
- recoverable from canonical code plus evidence history.

### P8 - Tool growth as a first-class capability

The agent should be able to create new tools for:

- system inspection,
- resource optimization,
- internet research,
- code analysis,
- local device usage,
- operational automation,
- evidence compaction and reporting.

But each new tool should be:

- bounded,
- testable,
- justified by repeated need,
- suitable for constrained hardware.

### P9 - Research-to-runtime transfer

`T:\Code\agents_library\` should not remain passive reference material.

The project should develop a repeatable path for:

1. discovering relevant agent patterns,
2. ranking them by host-fit and expected value,
3. adapting them to `nanobot` and `eeepc`,
4. validating them under real resource limits,
5. promoting only what survives constrained-host evaluation.

### P10 - Controlled divergence with accountable promotion

The self-evolving runtime may advance faster than the canonical repo in local
host behavior, experiments, and provisional tools.

That divergence is allowed.
What matters is that important improvements remain promotable back into
reviewable canonical source.

### P11 - Owner utility and artifact value

The system should optimize for owner usefulness, not only internal autonomy.

This includes:

- practical assistance,
- quality-of-life tooling,
- readable terminal experiences,
- evaluable visual or interactive artifacts,
- creative experiments that can be judged, refined, and learned from.

Useful artifacts are legitimate outputs of the system so long as they remain
bounded, reviewable, and suitable for the target host.

### P12 - Runtime output over maintainer handcrafting

When the project needs owner-facing interfaces, utilities, or creative outputs,
the preferred path is to improve the runtime system so it can generate them.

Maintainers may prototype or scaffold such capabilities when needed, but the
steady-state goal is not manual artifact production.

The steady-state goal is a better base system that can produce better outputs.

### P13 - Re-bootstrapable autonomy

The system should be able to recover from severe drift or self-damage by falling
back to canonical source, baseline configuration, and retained evidence.

Safe self-evolution therefore requires:

- a minimal trusted bootstrap path,
- explicit recovery checkpoints,
- replayable configuration,
- bounded mutation zones,
- repeatable bring-up on similar weak hosts.

## Operating Model

The project operates through five loops.

### Loop A - operator loop

- receive operator request,
- inspect current runtime state,
- respond or act with highest priority,
- surface truthfully what is possible now.

### Loop B - self-improvement loop

- inspect own behavior,
- detect friction, failure, waste, or repeated need,
- generate bounded improvement candidate,
- apply and evaluate if policy allows,
- emit evidence and promotion metadata.

### Loop C - host adaptation loop

- observe resource usage and device availability,
- identify bottlenecks or unused capabilities,
- optimize configuration, tooling, routing, storage, or scheduling,
- preserve only improvements that help the weak host.

### Loop D - research ingestion loop

- analyze external agent research,
- extract lightweight patterns,
- test local fit,
- integrate selectively,
- reject patterns that increase complexity without payoff.

### Loop E - owner utility loop

- observe owner requests, repeated friction, and stated interests,
- identify opportunities for useful or creative output,
- build bounded interfaces, tools, or artifacts,
- gather owner feedback or usage signals,
- refine what proves valuable.

## Runtime Output Doctrine

Owner-facing outputs should be described and treated as products of the running
system whenever possible.

Examples:

- the system generates a dashboard,
- the system produces a visualization,
- the system creates a small utility,
- the system iterates on a creative artifact.

Maintainers own the machinery that makes those outcomes possible:

- architecture,
- policy,
- bootstrap,
- portability,
- observability,
- recovery,
- promotion paths.

## Self-Development Safety Lifecycle

The expected self-development path is staged.

### Stage A - minimal trusted runtime

- establish the smallest reliable base,
- confirm operator control,
- confirm restart and recovery basics.

### Stage B - introspection before mutation

- inspect host, runtime, and code surfaces first,
- measure bottlenecks and gaps,
- avoid mutation before the system can explain what it sees.

### Stage C - bounded self-change

- enable allowlisted self-edits,
- evaluate local results,
- keep changes attributable and reversible.

### Stage D - evidence and promotion discipline

- record what changed and why,
- export evidence,
- treat host-born improvements as provisional until promoted.

### Stage E - runtime-generated outputs and broader autonomy

- let the system produce owner-facing outputs,
- expand tool growth only after base safety proves durable,
- prefer growth that remains portable to similar weak hosts.

## Backlog Epics

The following epics translate the project vision into concrete architecture work.

### Epic E1 - Weak-Host Runtime Fitness

Goal:

- make `nanobot` reliably survivable on old `eeepc` hardware.

Scope:

- memory budgeting,
- CPU-aware scheduling,
- disk-growth control,
- startup and idle optimization,
- degraded-mode operation,
- low-cost logging and retention.

Example outcomes:

- explicit runtime budgets,
- memory pressure detection,
- backoff under overload,
- compacted ledgers and state.

### Epic E2 - Truthful Capability Surface

Goal:

- ensure every capability statement reflects live verified state.

Scope:

- shared capability snapshot model,
- free-form answer grounding,
- mode/policy/runtime distinction,
- simulator and live regression probes.

Example outcomes:

- `/cap_status` and free-form answers agree,
- no stale memory is presented as current fact,
- blocked tools are explained accurately.

### Epic E3 - Self-Evolution Control Plane

Goal:

- make self-improvement systematic, bounded, and reviewable.

Scope:

- cycle orchestration,
- improvement proposal format,
- apply/evaluate/report flow,
- promotion candidate creation,
- lane isolation for operator vs background work.

Example outcomes:

- cleaner cycle lifecycle,
- durable report artifacts,
- explicit promotion events,
- recovery-safe autonomous edits.

### Epic E4 - Memory For Constrained Agents

Goal:

- build memory that improves action quality without exhausting resources.

Scope:

- short-term working memory,
- compact long-term summaries,
- anti-repeat guards,
- host-state memory,
- operator-intent memory,
- memory aging and compaction.

Example outcomes:

- fewer repeated failures,
- fewer repeated explanations,
- lower prompt bloat,
- better continuation across cycles.

### Epic E5 - Tool Growth And Local Agency

Goal:

- let the agent extend itself with lightweight tools suited to the host.

Scope:

- parser and research tools,
- system-diagnostics tools,
- code-analysis helpers,
- local automation tools,
- device-facing tools where safe and useful.

Example outcomes:

- host inspection toolkit,
- constrained-web research path,
- reusable optimization helpers,
- bounded sensor/device utilities.

### Epic E6 - Device And World Interfaces

Goal:

- treat built-in host peripherals and connectivity as possible agent senses and
  actuators.

Scope:

- camera access,
- microphone capture,
- Bluetooth inspection,
- Wi-Fi/network diagnostics,
- local media ingestion,
- safe device policy boundaries.

Example outcomes:

- host hardware inventory,
- device capability matrix,
- staged device tool adapters,
- clear permission and privacy model.

### Epic E7 - Research Transfer Pipeline

Goal:

- turn `agents_library` into an operational improvement feed.

Scope:

- research indexing,
- pattern extraction,
- relevance scoring,
- constrained-host adaptation templates,
- implementation tracking.

Example outcomes:

- research-to-backlog mapping,
- adopted pattern ledger,
- rejected-pattern reasons,
- repeatable transfer workflow.

### Epic E8 - Evidence, Sync, And Promotion

Goal:

- preserve autonomy without losing auditability or canonical ownership.

Scope:

- cycle export manifests,
- host evidence sync,
- workspace sync,
- promotion candidate metadata,
- branch/repo policy enforcement.

Example outcomes:

- compact cycle history,
- promotable host-born changes,
- safe GitHub sync boundaries,
- recoverable evolution trail.

### Epic E9 - Autonomous Project Creation

Goal:

- let the system create separate repos/projects when repeated work no longer fits
  bounded host mutation.

Scope:

- project seed manifests,
- repo bootstrap logic,
- project registration,
- ownership and namespace governance,
- promotion path back to canonical repos where needed.

Example outcomes:

- lightweight side projects for tools,
- explicit project lineage,
- no hidden code growth inside evidence repos.

### Epic E10 - Owner Utility, Interfaces, And Creative Artifacts

Goal:

- ensure the system can generate visible value for the owner/operator, without
  turning maintainers into manual output producers.

Scope:

- runtime-capable dashboard and interface generation,
- owner-facing workflow tools generated or refined by the system,
- small utilities discovered through agent research,
- evaluable artifact creation,
- constrained-host creative software,
- games, graphics, and demoscene-style experiments where appropriate,
- feedback loops that teach the runtime what outputs are worth improving.

Example outcomes:

- the runtime can generate a host dashboard that is pleasant and useful,
- the runtime can iterate on lightweight operator interfaces,
- artifact generators can be reviewed and improved,
- creative outputs help the system learn what the owner values.

### Epic E11 - Portable Base Configuration And Host Profiles

Goal:

- define a reusable baseline that can be deployed across similar weak hosts.

Scope:

- base invariants,
- host profile overlays,
- resource budgets,
- portable path and dependency assumptions,
- deployment validation for similar hardware classes.

Example outcomes:

- a host-agnostic baseline profile,
- explicit weak-host overlays,
- fewer undocumented host-specific assumptions,
- repeatable deployment on similar systems.

### Epic E12 - Safe Bootstrap, Recovery, And Rebuild

Goal:

- ensure the system can be brought up from scratch and recovered after drift,
  corruption, or self-damage.

Scope:

- bootstrap stages,
- minimal trusted runtime,
- recovery checkpoints,
- rebuild drills,
- rollback guidance,
- proof that canonical source plus evidence can restore forward progress.

Example outcomes:

- documented bring-up sequence,
- safer cold-start autonomy,
- recovery drills with measurable outcomes,
- less dependence on irreproducible host state.

## Target Capability Model

The project should evolve through three target stages.

### Stage 1 - Foundational Runtime

Definition:

- `nanobot` runs reliably on the weak host,
- core channels and operator paths work,
- evidence and policy boundaries are in place,
- the system can report what it can and cannot do.

Required capabilities:

- stable startup and restart,
- bounded resource usage,
- truthful capability reporting,
- durable follow-up and reporting,
- canonical/evidence separation,
- portable baseline assumptions,
- simulator-backed regression coverage.

Success test:

- the system is dependable before it is ambitious.

### Stage 2 - Adaptive Host Agent

Definition:

- the system actively studies the host and adapts itself to run better there.

Required capabilities:

- resource sensing,
- bottleneck detection,
- host-specific tuning,
- low-cost memory and prompt compaction,
- creation of small host-utility tools,
- creation of owner-facing dashboards or utility views,
- better use of available local devices and connectivity.

Success test:

- the system becomes measurably more efficient on the target host over time.

### Stage 3 - Evidence-Governed Self-Evolving System

Definition:

- the system can continuously improve its behavior, tools, and surrounding
  project surfaces under policy, with evidence and promotion discipline.

Required capabilities:

- autonomous improvement cycles,
- proposal and evaluation flow,
- bounded self-editing,
- research ingestion from `agents_library`,
- new tool creation,
- iterative owner-facing tool and artifact improvement,
- project bootstrap when scope outgrows local mutation,
- safe rebuild from baseline after major failure,
- promotion of accepted improvements into canonical repos.

Success test:

- the system not only survives on weak hardware, but compounds capability without
  losing control.

## Design Tensions To Manage

This project intentionally lives inside several tensions.

They should be managed, not denied:

- autonomy vs operator control,
- experimentation vs recoverability,
- memory richness vs prompt cost,
- tool growth vs attack surface,
- local divergence vs canonical stability,
- broader capability vs weak-host affordability.

Good design in `eeepc` means resolving these tensions in favor of durable,
truthful, resource-aware progress.

## Project Decision Filters

Before adding a major feature, ask:

1. does it help on the real weak host,
2. can it be explained and debugged locally,
3. is it cheaper than the complexity it adds,
4. can it degrade gracefully,
5. can its effects be measured,
6. can it be bounded by policy,
7. can it leave evidence and a recovery trail,
8. does it help the system adapt, learn, or act more effectively.

If the answer is mostly no, it is probably not a fit.

## Official Direction

The official direction of this repository is therefore:

- keep `nanobot` as the engine base,
- optimize it for constrained `eeepc` hardware,
- evolve it into a truthful and self-improving agent,
- let the host become a bounded environment for self-study and adaptation,
- make the base system portable to similar weak hosts,
- transfer the best lightweight agent ideas from research into runtime,
- keep maintainers focused on architecture, bootstrap, recovery, and governance,
- make owner-facing outputs increasingly runtime-generated rather than manual,
- preserve canonical ownership, evidence, and promotion discipline as it grows.
