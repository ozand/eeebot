---
title: Checkpoint bridge phases and memoize bounded cycle decisions
status: accepted
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2180", "#2177", "#2050", "#1997", "#2162", "#1989", "#2058", "#2068", "ADR-043", "ADR-055"]
tags: [runtime, bridge, durability, state-machine]
---

# Status

Accepted as a provisional candidate for `#2180`; not effective until independent PASS review and all applicable required checks on this exact candidate revision, followed by authorized merge of that unchanged revision. Under the delegated #2180-C assignment recorded in [issue comment 6796022247](https://github.com/ozand/eeebot/issues/2180#issuecomment-6796022247), the assigned delivery executor may technically accept this ADR; the coordinator retains merge and issue-closure responsibility. Acceptance is planned through the #2180 implementation PR. This is delegated technical acceptance, not personal operator acceptance. The first post-execution boundary is staged work only: every other #2180 outcome remains required and is not complete.

# Context

`nanobot/runtime/bridge.py:_main_impl_body` orchestrates demand, proposal, planning, diary, branch, execution, gating, integration, and reflection. A process interruption can lose in-memory context while branch commits remain. Existing recovery authority is bounded and distributed: ADR-043 governs validated operator resume under the shared bridge lock; #2162 separately designs stale-call reconciliation and #1765 owns runtime recovery behavior. #1989 separately tracks a real subprocess SIGKILL/branch-resume regression. This proposal cannot replace or weaken those gates, claim those issues complete, or decide how ambiguous calls are resolved.

# Decision

1. The intended outcome for #2180 remains checkpointed phase execution across the cycle, bounded persisted per-cycle memos for non-repeatable decisions, persisted sleeping/backoff rather than long in-phase blocking waits, and safe post-execution recovery of existing cycle-branch work. The phase list below is illustrative rather than a required exhaustive linear decomposition; implementation may stage a bounded post-execution recovery boundary first, but staging does not drop or close the remaining outcomes.
2. A future `memo(key, value)` is a bounded first-write-wins record for a decision/value, not an atomic transaction with an external effect. A memo can prevent duplicate local decision creation only when its persistence and phase transition ordering prove that property. It cannot prove whether a subprocess, model provider, Git action, diary write, or other external effect completed. Any claim that a recovered phase avoids repeated paid work is conditional on separately verified effect/attempt evidence and authorization; it is not a consequence of memo presence alone. Any crash between an effect and its durable receipt remains uncertain and must be held/reconciled under the separately governed recovery contract (including ADR-056); do not blindly re-execute.
3. Persisted backoff/sleeping is an intended outcome of #2180: record bounded `{status: "sleeping", until, next_phase}` and yield without a long blocking sleep where the host timer semantics support it. Validate actual timer wake behavior before replacing an existing wait; no current host behavior or deployment is asserted.
4. The first bounded post-execution fault experiment reuses the existing bridge cycle-start path (`_prepare_repository_for_cycle` → `_restore_to_main` → `_finish_pending_pushes`) under ADR-043's shared bridge lock. Inject interruption after a late integration push reaches `origin/main` but before the terminal `pushed_late` row is durably recorded. The current `push_pending` record has the original base and branch name but no durable branch-head receipt, so when recovery sees `origin/main` has moved it cannot distinguish its own completed push from another integration; it records `superseded` and keeps the cycle branch for forensic/operator reconciliation. This is a safe no-repeat hold, not successful recovery or proof of which effect moved main. It must not perform another merge or push. A later positive recovery requires durable branch identity/base/head, evidence tying the exact merge to that branch and cycle, current authority, remaining budgets and fresh gate evidence; if any is missing or stale, hold. Checkpoint state or matching SHA alone never authorizes integration. #1989's real subprocess SIGKILL/branch-resume test remains separate.
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
- **Use an external workflow server (for example, Temporal or Celery):** not evaluated here and outside this bounded proposal. No target compatibility or resource comparison is claimed; reconsideration requires the evidence and separate decision described by ADR-055.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| Selected phase transitions persist versioned bounded checkpoints; first-write-wins memo is established only for local persisted decisions across restart | `tests/test_cycle_phase_machine.py::test_phase_checkpoint_and_memo_idempotency` | deferred (#2180) |
| When a late integration push completed but its terminal receipt was interrupted, cycle-start recovery observes the moved base, does not repeat the merge/push, records the conservative `superseded` outcome and retains the branch for reconciliation; this does not claim to identify the effect | `tests/test_bridge_late_push_pickup.py::test_post_push_crash_holds_without_duplicate_integration` | passing locally on Python 3.13; one bounded fault test only, not successful recovery or full issue completion |
| Post-execution recovery validates branch identity/SHA, current authority, budgets and fresh gate evidence before any new effect; uncertain settlement or any mismatch holds for reconciliation rather than duplicate action | `tests/test_cycle_phase_machine.py::test_post_exec_phase_resumes_verified_branch_sha` | deferred (#2180); not covered by receipt-only reconciliation |
| Persisted sleeping/backoff yields without blocking and does not resume before deadline under the supported timer behavior | `tests/test_cycle_phase_machine.py::test_sleeping_phase_yields_until_deadline` | deferred (#2180); intended outcome retained, implementation sequencing to be decided in issue |
| Crash between a subprocess/model/Git effect and its durable receipt cannot be treated as atomic completion or blindly repeated; stale gates, changed branch/base/head, revoked authority, exhausted budget and cancellation all hold without a new effect | Focused injected-failure tests in `tests/test_cycle_phase_machine.py` (exact names to be fixed by the implementation contract) | deferred (#2180) |
| Full phase coverage and accepted recovery outcomes remain complete, or are separately re-governed; this candidate and its receipt-only hold do not close #2180 | Full phase/recovery acceptance walk in issue #2180 | deferred (#2180) |

# References

- Issues `#2180`, `#2177`, `#2050`, `#1997`, `#2162`, `#1989`, `#2058`, `#2068`; ADR-043 and ADR-055. These related issues retain their distinct ownership and criteria; they are not scope transferred here.
- `@earendil-works/pi-durable` (`packages/durable/test/examples/11-tasks.ts`, `12-recovery.ts`).
