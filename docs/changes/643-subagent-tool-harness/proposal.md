# 643 — Variant 2: minimal Python tool-harness for bounded subagents

## Context

#641 removed the `pi_dev` subagent executor profile: shelling out to the
external `pi` binary with `--no-tools` disabled its entire agentic harness, so
the call was functionally equivalent to one direct LiteLLM request through the
same proxy nanobot already uses — a dependency (external binary, self-compiled
Node runtime on an unsupported i386 host) paid for with no harness benefit.
#641 explicitly scoped out "variant 2": giving bounded subagents a real
code-editing harness (read/edit/run tools, not just a single LLM completion).

That idea backlog entry (superseded by this change folder) already did the
research: the open-source core of pi (https://github.com/earendil-works/pi,
`packages/coding-agent` + `packages/agent`) has small, well-tested tool
contracts worth reusing. This proposal turns that research into a design.

## Decision (operator, 2026-07-05)

If/when we build variant 2, it is a **small Python port of proven patterns**
from pi's open-source core — not a re-adoption of the `pi` binary. There is no
Node runtime dependency, no external product dependency, no `--no-tools`
degradation. The harness lives entirely inside `nanobot/runtime/`, calling the
same LiteLLM client the rest of the runtime already uses.

## Goals

- Bounded subagents can read, edit, and (later) run commands inside a
  confined workspace, under the existing stop-guards (R11-R13,
  `docs/specs/self-evolving-runtime/spec.md`) and promotion gating
  (`docs/specs/subagent-bridge/spec.md`) — not as a parallel authority.
- Core stays small: ~1.0-1.5k LOC target for loop + tools + shared
  truncation/veto plumbing (pi's comparable core is ~2.3k LOC across 7 tools
  plus 450 LOC shared; we need fewer tools and no rendering/session layers).
- Zero new external dependencies: Python stdlib + the existing LiteLLM
  client. No Node, no new pip packages, no new host services.
- Every tool call is policy-gated and journaled the same way bridge results
  are today (`state/subagents/results/`).

## Non-goals

- No TUI, no extension system, no general-purpose agent framework — this is
  a bounded tool-call loop for one subagent role, not a product surface.
- No sandbox-free execution. pi assumes a supervised human CLI session and
  deliberately has no path confinement or command allowlist; eeebot runs
  unattended on a host with no operator watching in real time, so isolation
  is a hard requirement, not an option.
- No replacement of the coordinator loop, the bridge's branch-isolation/smoke
  gate/integration path, or the R11-R13 stop-guards — the harness runs
  *inside* the existing bounded-subagent-execution boundary, it does not
  introduce a second one.
- No expansion of the executor model contract (`un/qwen3.6-27b-mtp`,
  `docs/specs/subagent-bridge/spec.md` R4) — the harness changes what tools
  the executor can call, not which model runs it.

## Sequencing

- Starts only after the #641 rollout is verified closed (single built-in
  executor path stable in production).
- Implementation is gated on explicit autonomy-surface sign-off: promotion
  gating and stop-guard coverage for file mutation and command execution must
  be reviewed and approved before phase 2 (edit/write) or phase 3 (command
  execution) code is written. Phase 1 (read-only tools) carries no autonomy
  expansion and can proceed once scoped as its own Issue.
- This change folder covers **design only**; implementation is separate
  work, tracked as follow-up Issues linked from #643.

story_id: docs/specs/subagent-bridge/spec.md
