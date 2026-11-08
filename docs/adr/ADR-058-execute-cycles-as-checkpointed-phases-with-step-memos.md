---
title: Checkpoint bridge phases and memoize bounded cycle decisions
status: proposed
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2180", "#2177", "#2050", "#1997", "#2162", "#1989", "#2058", "#2068", "ADR-043", "ADR-055"]
tags: [runtime, bridge, durability, state-machine]
---

# Status

Proposed; not yet effective or implemented. Governing design proposal for `#2180`.

# Context

`nanobot/runtime/bridge.py:_main_impl_body` orchestrates demand, proposal, planning, diary, branch, execution, gating, integration, and reflection. A process interruption can lose in-memory context while branch commits remain. Existing recovery authority is bounded and distributed: ADR-043 governs validated operator resume under the shared bridge lock; #2162 separately designs stale-call reconciliation and #1765 owns runtime recovery behavior. #1989 separately tracks a real subprocess SIGKILL/branch-resume regression. This proposal cannot replace or weaken those gates, claim those issues complete, or decide how ambiguous calls are resolved.

# Decision

1. The intended outcome for #2180 remains checkpointed phase execution across the cycle, bounded persisted per-cycle memos for non-repeatable decisions, persisted sleeping/backoff rather than long in-phase blocking waits, and safe post-execution recovery of existing cycle-branch work. The phase list below is illustrative rather than a required exhaustive linear decomposition; implementation may stage a bounded post-execution recovery boundary first, but staging does not drop or close the remaining outcomes.
2. A future `memo(key, value)` is a bounded first-write-wins record for a decision/value, not an atomic transaction with an external effect. A memo can prevent duplicate local decision creation only when its persistence and phase transition ordering prove that property. It cannot prove whether a subprocess, model provider, Git action, diary write, or other external effect completed. Any claim that a recovered phase avoids repeated paid work is conditional on separately verified effect/attempt evidence and authorization; it is not a consequence of memo presence alone. Any crash between an effect and its durable receipt remains uncertain and must be held/reconciled under the separately governed recovery contract (including ADR-056); do not blindly re-execute.
3. Persisted backoff/sleeping is an intended outcome of #2180: record bounded `{status: "sleeping", until, next_phase}` and yield without a long blocking sleep where the host timer semantics support it. Validate actual timer wake behavior before replacing an existing wait; no current host behavior or deployment is asserted.
4. For post-execution recovery, first bound one experiment at the smoke-gate/integration boundary. Under ADR-043's shared bridge lock, validate the exact cycle-branch identity and expected base/head against durable evidence, current authority, budgets and gate freshness before acting. A mismatch, unsupported lock, uncertain effect, or stale evidence fails closed for authorized reconciliation; checkpoint state itself never authorizes a merge/integration or resets limits. #1989's real SIGKILL branch-resume test remains separate.
5. Define checkpoint schema/version, bounds, retention, release compatibility and expiry before implementation. No concurrent multi-cycle execution or general-purpose DAG engine is proposed. Do not absorb #1765 recovery, #2162 stale-call ownership, #2058 trust-anchor/exporter decisions, or #2068 disposition semantics into this issue.

# Consequences

### Easier

Checkpointed phases and memos could make bounded recovery more explicit and preserve eligible work, but they add durable state, migration, retention and reconciliation costs. No guarantee is made that a post-execution interruption can always be completed or avoid further model calls; that depends on verified branch/effect state, authority and fresh gates.

### Harder

Durable boundaries require an audited inventory of state crossing the selected phase transition; serializing the entire procedural stack is not assumed. Memo/checkpoint schemas and all recovery authority/evidence rules require focused implementation tests.

### What does not change

Single-writer bridge lock (`ADR-043`), mutation-surface/smoke gates, and promotion rules for runtime-slice changes remain unchanged.

# Alternatives considered

- **Keep monolithic `_main_impl_body` and add more recovery `if` branches:** rejected because implicit stack state makes every crash window a bespoke bug (`#2050`).
- **Adopt an external workflow server (Temporal/Celery):** rejected as too heavy for a 32-bit `eeepc` host (`ADR-055`).

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| Selected phase transitions persist versioned bounded checkpoints; first-write-wins memo is established only for local persisted decisions across restart | `tests/test_cycle_phase_machine.py::test_phase_checkpoint_and_memo_idempotency` | deferred (#2180) |
| Post-execution recovery validates cycle branch identity/SHA, authority, budgets and fresh gate evidence; uncertain external effects or mismatch hold for reconciliation rather than duplicate action | `tests/test_cycle_phase_machine.py::test_post_exec_phase_resumes_verified_branch_sha` | deferred (#2180); first bounded experiment, not full issue completion |
| Persisted sleeping/backoff yields without blocking and does not resume before deadline under the supported timer behavior | `tests/test_cycle_phase_machine.py::test_sleeping_phase_yields_until_deadline` | deferred (#2180); intended outcome retained, implementation sequencing to be decided in issue |
| Crash between external effect and memo/receipt cannot be treated as atomic completion or blindly repeated | Focused injected-failure test to be named in #2180 design before implementation | deferred (#2180) |

# References

- Issues `#2180`, `#2177`, `#2050`, `#1997`, `#2162`, `#1989`, `#2058`, `#2068`; ADR-043 and ADR-055. These related issues retain their distinct ownership and criteria; they are not scope transferred here.
- `@earendil-works/pi-durable` (`packages/durable/test/examples/11-tasks.ts`, `12-recovery.ts`).
