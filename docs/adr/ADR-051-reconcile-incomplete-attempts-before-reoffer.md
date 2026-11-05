---
title: Reconcile incomplete attempts before reoffer under the bridge lock
status: accepted
date: 2026-11-05
authors: [delegated delivery]
related: ["#2162", "#1765", "#1919", "ADR-043", "ADR-050"]
tags: [runtime, recovery, planning, idempotency]
---

# Status

Provisional acceptance candidate prepared 2026-11-05 under the owner's delegated technical authority for #2162. The Proposed design at `4584053b2e84483bc3d0d8c0c90694970e11e261` received independent PASS. This candidate becomes effective only after independent PASS and applicable checks on this exact revision followed by authorized merge of the unchanged revision; retain those receipts in the linked PR or issue. This is not personal operator acceptance. Runtime changes and tests remain in #1765. This source-only decision does not authorize host access, deployment or state surgery. The #1765 draft predates identification of its ADR gate; later acceptance cannot retroactively cure that chronology.

# Context

At #1919 candidate `06df1419b324117fd8ab4d9a104ec167d92cc6ff`, the stale check reads executor text without its structured call evidence and handles non-supplier errors as defects. Running registration lacks the original candidate-derived retry key. The normal incomplete counter and marker are separate writes; a stale terminal ledger row can clear running before accounting is proven. A crash can occur between these steps. These are source findings, not a demonstrated host incident.

The bridge wrapper's POSIX flock serializes only conforming supported callers; `_NullLock` and direct `_main_impl` calls do not establish exclusion. ADR-043's strict operator resume handles stopped planner state, not stale executor accounting, and explicitly does not create a multi-file transaction. ADR-050 describes call evidence, not this replay policy.

# Decision

For new attempts, persist and verify the exact already-computed retry key, cycle id, task id when available, branch and own plan in the existing running registration before executor spawn. Do not reconstruct the key from the plan or use `kill:<cycle>` as its substitute. Read terminal status, text and bounded structured call evidence from the same registered task record; validate its identity before classifying. Use ADR-050's classifier rules: affirmative supplier evidence remains supplier, definitive request defects remain defects, and evidenced incomplete calls with unknown cause are not either. Missing, invalid, ambiguous or mismatched evidence is unknown, never affirmative cause.

Normal handling, stale reconciliation and any direct entry to the relevant state writers must require the same exclusive bridge lock held continuously across accounting and state transitions. On missing/unsupported locking, contention, failed acquisition or a direct caller without that proof, refuse before mutation or new dispatch; `_NullLock` is not proof. The lock covers only cooperating writers; unrelated writers and hostile filesystem changes are outside this guarantee. No claim of an atomic multi-file transaction follows.

Use the existing per-key incomplete counter file with a versioned, validated count and the last accounted cycle id written together by same-directory atomic replacement, durable flush and verified readback. Both normal and stale paths use one charging operation. Under the lock, before registration or dispatch, initialize-if-absent a zero baseline by atomic replacement, durable flush and verified readback only for an eligible identity: its exact key is known, the existing state is readable and quiescent, and there is no unresolved running or pending attempt, handled marker, counter or other legacy ambiguity for that identity. This bounded absence rule assumes cooperating writers and a non-hostile filesystem; it does not prove complete historical absence or protect against external deletion. A valid old counter may be migrated under the lock only when its exact identity and count are verified and no attempt for it is unresolved; preserve its count. Ambiguous, invalid, unreadable or conflicting records stop, never become zero. Persist baseline or migration in the same counter file before registration; registration records and verifies its matching baseline identity. Retain that counter through resolution and later attempts; a counter missing after registration is ambiguous, never reinitialized. If initialization crashes before or after replace/readback or before verified registration, restart inspects the actual durable baseline and quiescent state before resuming; uncertainty stops rather than overwriting or resetting it. Implementation must inspect affected cleanup paths and ensure they retain the baseline; a bounded source search is not proof that no other deletion path exists. The same cycle replay must not increment again; a distinct cycle may increment only after the prior attempt's accounting and required marker/pending transition have been verified and its running registration resolved. An older/out-of-order replay, conflicting identity or inability to prove this ordering stops without mutation or reoffer. Last-cycle id alone is not an idempotency proof: the lock, verified registration and no-next-attempt-before-resolution invariant are all required.

Before any new dispatch, keep/edit resolution, terminal-ledger-driven running clear, or reoffer for that identity, reconcile the registered incomplete attempt: verify or write its charge, then enforce and verify the existing handled/pause marker when the cap is reached, then persist and verify the appropriate pending/running transition. A terminal ledger row alone does not prove this sequence. A crash before counter replacement retries the charge only after verifying the previously durable baseline or prior count. After replacement but before readback, restart must read and validate the durable record: a matching cycle means already charged; an intact prior record permits a charge; missing, malformed or conflicting evidence stops, never assumes the write failed. After verified replacement replay recognizes the cycle and finishes marker/transition; after marker or transition write, replay verifies or repairs the remaining steps. No later same-key charge may overtake an unresolved one. Under the lock every normal or stale charge, marker and state mutation callback must verify the currently persisted registration matches its exact key, cycle and baseline link before mutation; after clear or replacement, an older callback is rejected without changing counter, marker or running state. A failed or uncertain write/readback stops the tick and retains the registration, branch and available evidence; retry only reconciles, not dispatches. Keep or edit cannot create a fresh key to evade the cap; a changed plan may proceed only after the prior identity's resolution and under the normal validated new-work policy, not as a reset of the unresolved attempt. Supplier errors do not consume the incomplete budget.

For legacy running records or retry counters whose identity or accounting cannot be verified, preserve the branch and evidence, expose an explicit unresolved/unknown status, and prohibit automatic reoffer and budget mutation. Do not infer a key, retire work, clear the barrier through ordinary keep/edit/resume, or require undocumented file surgery. Resumption requires independently verifiable reconciliation of the original identity and accounting; this decision does not promise bounded-time resumption if the needed information is irrecoverably missing. The barrier must be as narrow as proven safe; if isolation of that work cannot be proved, stop the affected automation rather than claim only one task is blocked. This limited availability is the owner-reconciled boundary in #2162, not a claim that historical evidence was recovered.

Do not add raw payloads to recovery state or a new general journal, transaction framework or host control. Existing evidence and counter retention must survive restart through resolution and any cap marker; pruning must not remove a still-relevant counter or identity. No provider, deployment, or private state access is authorized by this decision.

# Consequences

### Easier

A known incomplete attempt can be accounted once and reconciled after a crash without asserting supplier or planner fault.

### Harder

A lock, durable writes and verified ordering are mandatory; availability can be limited indefinitely for irrecoverably ambiguous legacy evidence. Existing external or nonconforming writers are not protected.

### Unchanged

Supplier and request-defect classification, privacy boundaries, original #1765 criteria and unrelated ADR-043 planner-resume authority remain intact.

# Alternatives considered

Blindly increment on recovery risks double charging; skip accounting risks unlimited retries. A last-cycle token without enforced serialization and ordering is insufficient. Reuse ADR-043 operator resume alone cannot resolve executor identity. Manual state repair or automatic retirement guesses missing history; a general journal or global quarantine framework is disproportionate. The conservative legacy stop is explicit and may limit automation until evidence permits safe reconciliation.

# Test Contract

The named scenarios below are implementation obligations deferred to #1765, not claims that these tests exist on main or already cite this ADR.

| Claim | Required test | Status |
|---|---|---|
| Same registered task evidence distinguishes supplier, request defect, incomplete and missing/mismatched unknown without invented cause | `tests/test_open_increment.py::test_stale_call_classification_uses_matching_task_evidence` | deferred (#1765) |
| Eligible baseline or quiescent migration precedes registration; initialization crashes, legacy ambiguity and counter missing after registration stop or reconcile safely; replacement before readback and marker/pending/running crash boundaries replay without double charge or cap bypass | `tests/test_open_increment.py::test_incomplete_recovery_crash_boundaries_replay_once` | deferred (#1765) |
| Same-key cycles serialize; old callbacks after clear/replacement are rejected; direct/unsupported/busy lock callers refuse before mutation | `tests/test_open_increment.py::test_incomplete_recovery_lock_and_ordering` | deferred (#1765) |
| Missing legacy identity, ambiguous counter, malformed/unwritable state and restart retain evidence and block unsafe reoffer until verified reconciliation | `tests/test_open_increment.py::test_incomplete_legacy_unknown_stays_safely_unresolved` | deferred (#1765) |
| Keep/edit cannot reset unresolved budget; cap survives marker repair and restart; unrelated keys and supplier paths retain their contract | `tests/test_open_increment.py::test_incomplete_cap_survives_resolution_and_restart` | deferred (#1765) |

# References

- Governing issue and research: https://github.com/ozand/eeebot/issues/2162#issuecomment-6654615479
- Owner-reconciled legacy availability boundary: https://github.com/ozand/eeebot/issues/2162#issuecomment-6658940929
- Implementation issue #1765 and candidate #1919; `nanobot/runtime/bridge.py`, `nanobot/runtime/open_increment.py`, `nanobot/runtime/cycle_ledger.py`.
- ADR-043, ADR-050; neither authorizes this new recovery contract before its own acceptance.
