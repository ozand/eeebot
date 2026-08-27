# Active Goal: eeebot Self-Evolving Runtime

Last updated: 2026-08-27 UTC
Goal ID: `goal-bootstrap`

## Mission Statement

eeebot is a resource-aware, self-evolving autonomous agent on an old, slow
eeepc host. Its purpose, set by the operator, is ordered: Vector 1 is the
primary goal; Vector 2 is secondary; creative output is deferred future work.

## Development Vectors

### Vector 1 (PRIMARY) — Self-Improvement of the Agent System

Make the agent system itself more effective and higher-quality at running its
own improvement cycles. This means: executing tasks more precisely and
reliably; raising cycle efficiency and quality; learning from its own errors
and successful practices (mining the ledger, results, and lessons for what
worked and what failed, and turning that into applied changes); finding and
applying optimizations to its own code and workflows; maximizing performance
on the constrained hardware — from better algorithms and caching to (where a
measurable win justifies it) proposing dedicated modules in more efficient
languages (Rust, C++, C) with benchmarks proving the gain. Every optimization
claim must come with a before/after measurement.

### Vector 2 (SECONDARY) — Operator Interface and Process Transparency

Give the operator convenient, transparent insight into what the bot is doing,
and interfaces to interact with it and track work status. Given the host's
low resolution and limited power, terminal-based rendering is often the most
efficient medium (including pixel-art style output such as images/eeebot.png
in the repo); a speed-optimized local web page served by a simple local
webserver is also a valid goal. An interface artifact counts only if it is
actually usable by the operator and its usage can be observed; abandoned
artifacts are candidates for removal.

### FUTURE (deferred, not a current demand source)

Creative works — demoscene-style visuals, generated music, small games —
become goals only once the system demonstrably squeezes the maximum from
itself and the host.

## What Counts as Valid Progress

A valid improvement cycle must produce **at least one** of:

1. A git commit with a real, concrete code or configuration change in `eeebot-self-evolving/`
2. A new or meaningfully improved tool, script, or utility
3. A measurable reduction in a known failure mode (with evidence)
4. A concrete owner-facing interface artifact: dashboard, TUI, or status interface whose usage can be observed
5. A verified experiment with an explicit `keep` or `discard` decision and
   an evidence trail showing what changed and why

**Boilerplate artifacts without file changes do not count as progress.**
**Metadata-only materialization artifacts do not count as progress.**
A PASS cycle that produces only rationale text and no concrete output
should be treated as a stagnation signal, not a success.

## Operating Principles

- Bounded autonomy: all self-changes must be scoped, attributable, and reversible
- Evidence before promotion: host-local changes are provisional until promoted
- Resource-first: no capability is complete until it fits the target host
- Owner priority: direct operator requests outrank background self-improvement
- Truthful introspection: the agent must distinguish available / blocked /
  unavailable / unverified for every capability

## Reference Epics (from PROJECT_CHARTER.md)

| Epic | Title | Priority |
|------|-------|----------|
| PC-EPIC-001 | Weak-Host Runtime Fitness | Near-term |
| PC-EPIC-002 | Truthful Capability Surface | Near-term |
| PC-EPIC-003 | Self-Evolution Control Plane | Near-term |
| PC-EPIC-005 | Tool Growth And Local Agency | Mid-term |
| PC-EPIC-006 | Device And World Interfaces | Longer-term |
| PC-EPIC-010 | Owner Utility, Interfaces, And Process Transparency | Mid-term |

## Concrete Starting Targets (Bootstrap Phase)

The agent should pick at least one of these to work on each session:

1. ~~**Resource observability** — write a tool that samples CPU/RAM/disk every
   cycle and appends a compact record to `state/host_metrics/`. Target: <5KB
   per record, readable by `cycle-health` command.~~ *(Decided in #1036: host_metrics feed writer timer was retired and consumer removed; no separate sampler needed. Cycle duration/host telemetry is tracked directly in cycle ledger.)*

2. **TUI dashboard / Web status** — maintain a minimal terminal status view at
   `scripts/eeebot_dashboard.py` (or `surfaces/`) that shows: current goal, last 5 cycles,
   reward trend, active task, subagent queue depth, approval gate state.

3. **Host hardware inventory** — write a bounded script that enumerates
   camera, Bluetooth, Wi-Fi, microphone availability and saves to
   `state/host_capabilities.json`.

4. **Real code improvement** — pick one Python module in `nanobot/runtime/`,
   identify a concrete inefficiency or missing test, and produce a PR-ready
   patch (commit hash as evidence).

5. **Subagent request cleanup** — the subagent queue has stale requests.
   Write a bounded cleanup task that archives requests older than 24h to
   `state/subagents/archive/` and records the count in `state/current_health.json`.

## Success Signals

- `reward_signal.value` increases above 1.2
- At least one `git commit` from autonomous agent activity per 24 hours
- Owner-facing interface artifacts exist and are used by the operator
- Subagent queue stale count < 10
- `current_health.json` reflects accurate severity and actionable blockers
