---
title: Record intent before effect and declare explicit tool replay policy
status: accepted
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2178", "#2177", "#1765", "#2162", "#1989", "ADR-050", "ADR-055"]
tags: [runtime, durability, tools, recovery]
---

# Status

Accepted as a provisional candidate; not effective until independent PASS review and required checks on the exact candidate revision, followed by authorized merge of that unchanged revision. The #2178 delivery executor is delegated technical acceptance upon that merge; this does not imply personal operator acceptance. Acceptance is planned through the #2178 PR, with the exact candidate SHA and acceptance evidence retained there. Runtime behavior remains unimplemented pending effective acceptance and separately reviewed implementation. The bounded design review passed on proposal blob `3a6c6d9a7a9e928a7f72f59add2e4b6e2ccdc616`; that review does not cover this candidate.

# Context

At source revision `4f860ff5691cd0addf249af77e03faed615cae2e`, the bridge already verifies a running registration before executor spawn (`bridge.py::_evaluate_candidate` / `open_increment.record_attempt_started`); the existing focused test `tests/test_agent_chooses.py::test_registration_persistence_failure_blocks_executor_startup` passes on that revision. The registration currently identifies a cycle/branch and is read back, but has no unique attempt identity; the subagent task ID is minted inside `SubagentManager.spawn` and attached only after spawn returns. This is source/test evidence for that existing executor gate, not evidence that a new intent journal exists or covers other invocation paths.

Provider/tool paths have nested retries: `LLMProvider.chat_with_retry` makes the existing configured network attempts and may issue an image-stripped request; the proposer also has bounded validation/fallback passes. The journal must distinguish parent owner, logical operation, and each physical provider/tool attempt without resetting, expanding, or conflating existing retry and cost budgets. Missing terminal evidence does not show whether a request/effect happened, whether a remote request is still running, or whether its result was consumed by the parent. Intent proves admission was authorized, not outcome or exactly-once execution.

Current bridge behavior already holds a pending executor increment for planning resolution; #2162 separately owns safe identity/accounting for stale executor attempts. ADR-035 is Proposed in this baseline and is not treated as authority here. A new call-evidence writer must not duplicate or override that recovery authority. This proposal does not claim the bridge lock protects independent service processes or every entrypoint. A proposed owner-scoped evidence boundary should isolate corrupt/unsettled records and not add a new global bridge stop. Full autonomous path inventory and owner mapping remain implementation acceptance work, not assumed complete by these observations.

# Decision

1. Every external attempt belongs to a stable opaque `owner_id` for its durable parent, whose continuation/reissue must be held; it also has a logical `operation_id` and distinct `attempt_id` per application-controlled provider request or tool execution. The parent supplies/reloads the same `owner_id` before admitting effects; a restart must not mint a replacement owner ID to bypass an unresolved operation. Persist `operation_id` before the first attempt and a new `attempt_id` before each retry. Do not derive IDs from prompts, arguments, secrets, content hashes, or timestamps. An ephemeral process/subagent task ID is not durable parent identity. Map every in-scope autonomous route to its parent before full acceptance; routes without a stable owner or verifiable parent-resolution evidence are explicitly unavailable, not covered.

For bridge executor calls, pre-spawn parent/attempt identity and retry ownership remain under #2162. At baseline `4f860ff5691cd0addf249af77e03faed615cae2e`, the bridge writes `open_increment.running` before `mgr.spawn`, but it lacks a unique attempt ID; `SubagentManager.spawn` mints its task ID internally and the bridge attaches it only after return. The existing registration-failure test passed on that baseline (1 passed); it verifies only the existing spawn gate. This ADR consumes the single identity/retry authority reconciled by #2162 and creates no parallel `running` schema. Until that identity is published and effectively accepted, cross-cycle executor hold/reoffer remains uncovered.
2. Use a local versioned JSONL journal with one file per owner and no global index. Before implementation, #2178 defines tested per-record/per-owner byte and record limits and settled-record retention. Serialize admission, reconciliation and pruning for one owner under its exclusive lock; do not hold a global lock across external calls. Append a complete record under the owner lock, flush and `fsync` before effect admission or result release; sync the parent directory on file creation. Read back and validate identity before state transition. If the supported lock or required sync fails/is unavailable, deny admission or hold that owner. The existing bridge flock does not cover independent services. Store opaque IDs, allowlisted component/target, event/status, schema version, bounded numeric metadata and timestamps only—not prompts/responses/tool arguments, secrets, private payloads or content-derived fingerprints. This is a process-crash contract on supported local filesystems, not a sudden power-loss guarantee.
3. Persist intent before admitting each attempt; failure to verify it denies that effect. Persist terminal evidence for the exact tuple before returning a result to caller code or starting a retry. If an effect may have happened but terminal persistence fails, withhold the result, retain the intent as unsettled and mark that owner unavailable. Never convert this into supplier/defect failure or automatically resend. The persisted intent consumes exactly one slot in the original attempt/retry and cost budget because the effect was admitted; do not refund that slot or add a synthetic failure charge from missing terminal evidence. Reconciliation may not exceed the remaining original budget. A terminal means the attempt returned to the process; it does not prove that the parent received or durably consumed the result. Therefore a terminal alone never releases the owner after restart: release requires the same owner's existing durable parent authority to prove either result consumption or closure that forbids any continuation/reissue. If that evidence does not exist for a route, keep it unavailable and retain its unresolved evidence; do not add a generic repair command or infer consumption from time. Existing retry delays, classifications, proposer fallback and cost limits remain unchanged. Journal each application-controlled attempt. If an SDK performs unobservable internal retries and those cannot be captured without changing current behavior, that route remains uncovered and cannot claim exact attempt accounting or replay safety.
4. Holds are scoped to stable owner: an unsettled owner cannot reissue that operation, and a restarted parent cannot evade the hold with a fresh `owner_id`; unrelated owner partitions remain eligible. Corrupt/truncated/ambiguous owner files are held without truncate/delete/reset and do not by themselves block other owners. A missing/unreadable journal is unavailable when the existing parent says an owner is active; never infer empty. Retain all unresolved evidence without time-based eviction. #2178 declares retention; prune settled records only after the same owner's durable parent authority verifies consumption or closure with no continuation/reissue, under the owner lock. At per-owner capacity, preserve the owner and refuse new admissions for that owner rather than evict unresolved evidence. Disk-full or terminal-write failure leaves that owner held; other owners continue only if their own strict writes succeed. The owner-lock identity/path must remain stable while the owner may be referenced: pruning may remove journal records but must not unlink/recreate the lock file or replace the owner directory and thereby split concurrent lockers. A local lock serializes cooperating writers only; it does not prove a remote request or child process stopped.
5. `replay_policy` defaults to `"unsafe"`; read-only names are not automatic opt-ins. Replay requires equivalent authorized input/state, current permission, quiescence, remaining original budget and no cancellation. The journal stores no inputs/results and grants no automatic replay capability; paths lacking separately authorized inputs remain held. No manual repair or state surgery is added. #1765 classification and #1989 SIGKILL/branch-resume remain separately owned. Staging is sequencing only; all autonomous routes remain acceptance obligations until inventoried, implemented and verified.

# Consequences

### Easier

Per-attempt evidence can distinguish a recorded admission without a matching settlement from a completed attempt, while explicitly preserving uncertainty about remote effects. Read-only calls are not automatically safe to repeat; they may be replayed only with evidence satisfying this contract.

### Harder

Every in-scope invocation path must durably record admission and exact attempt settlement; recovery must establish quiescence and authorization, and tool owners must maintain evidence-backed replay classifications. Storage cost and recovery complexity require measurement.

### What does not change

Existing tool schemas exposed to the LLM, mutation-surface gates, and telemetry privacy rules remain unchanged.

# Alternatives considered

- **Infer interruption from missing cycle outcomes or wall-clock gaps:** rejected because timeouts and crashes remain indistinguishable from silent drops.
- **Default `replay_policy` to `"safe"`:** rejected as unsafe; forgetting to mark a mutating tool would duplicate side effects after a crash.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| Complete autonomous provider/tool path inventory maps every caller to a stable owner ID before effect and preserves each existing nested retry/cost budget; unmapped paths are explicitly unavailable, not assumed covered | `tests/test_intent_admission.py::test_autonomous_invocation_inventory_and_budget_parity` | deferred (#2178) |
| Owner-partition intent is durably verified before effect; injected intent-write failure denies admission, while terminal-write failure withholds the result and leaves only that owner unsettled | `tests/test_intent_admission.py::test_intent_and_terminal_persistence_fail_closed_per_owner` | deferred (#2178) |
| Operation/attempt IDs pair exact terminals; late/mismatched terminals, live calls, corrupt owner partitions, unsupported lock and concurrent owner admission fail closed without blocking an unrelated healthy owner | `tests/test_intent_admission.py::test_owner_scoped_recovery_and_corruption_isolation` | deferred (#2178) |
| A crash after terminal fsync but before parent consumption cannot reissue under the same or a freshly minted owner ID; only durable same-owner consumption/closure evidence releases the hold | `tests/test_intent_admission.py::test_terminal_before_parent_consumption_remains_held` | deferred (#2178) |
| Replay defaults unsafe; changed/missing input, revoked permission, active attempt, cancellation or exhausted original budget prevents replay; read-only name alone is insufficient | `tests/test_intent_admission.py::test_replay_requires_equivalent_input_current_authority_and_budget` | deferred (#2178); no automatic replay is claimed until evidence exists |
| Owner records obey declared size/retention limits, unresolved records are never pruned, settled records prune only after durable parent resolution, and persisted metadata excludes prompts, arguments, secrets and private fingerprints | `tests/test_intent_admission.py::test_owner_records_are_bounded_private_and_retained_until_settled` | deferred (#2178) |
| Concurrent admission and pruning use the same persistent per-owner lock identity; journal cleanup cannot unlink/recreate its lock path and admit through a second lock inode | `tests/test_intent_admission.py::test_owner_lock_identity_survives_pruning` | deferred (#2178) |

# References

- Issues `#2178`, `#2177`, `#1765`, `#2162`, `#1989`; `ADR-050`, `ADR-055`. #2162 is the conditional owner of bridge executor attempt identity/retry accounting; no ADR-051 acceptance or publication is implied here.
- Source/test review baseline: `4f860ff5691cd0addf249af77e03faed615cae2e`; source inspection of pre-spawn registration and post-spawn task ID, plus `py -3.13 -m pytest tests/test_agent_chooses.py::test_registration_persistence_failure_blocks_executor_startup -q` (`1 passed`). This test verifies only the pre-existing registration gate, not the proposed journal, retry hook, or recovery behavior.
- `@earendil-works/pi-durable` (`packages/durable/src/harness/tool.ts`, `replay: "safe" | "unsafe"`) inspires an explicit classification only; its semantics do not establish eeebot equivalence or recovery authority.
