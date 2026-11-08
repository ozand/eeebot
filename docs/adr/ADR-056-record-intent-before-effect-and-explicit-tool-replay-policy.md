---
title: Record intent before effect and declare explicit tool replay policy
status: proposed
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2178", "#2177", "#1765", "#2162", "#1989", "ADR-050", "ADR-051", "ADR-055"]
tags: [runtime, durability, tools, recovery]
---

# Status

Proposed; not yet effective or implemented. Governing design proposal for `#2178`.

# Context

When a host process crashes or times out during a model/tool call (`#1765`, design issue `#2162`), a missing completion record does not show whether the call started, completed remotely, or produced an effect. Intent written before admission can establish what the runtime authorized to attempt, but cannot establish remote completion, prevent a late response, or make a side effect atomic. `nanobot` tools also lack a machine-readable replay classification. Recovery therefore needs per-attempt identity and an explicit fail-closed reconciliation policy, not timestamp inference. This proposal is not a verified inventory claim that the named example tools cover every autonomous-runtime registry or provider/tool route.

# Decision

1. Candidate tool definitions may declare `replay_policy: Literal["safe", "unsafe"] = "unsafe"` on their base class, but scope must first inventory the autonomous executor's actual registry and every invocation route; completeness is not inferred from example names. `safe` means an invocation may be repeated only when the recovery contract establishes that repeating it cannot create a prohibited duplicate effect **and** that the replay is semantically valid for the same authorized input/state. A name or read-only classification alone is insufficient. `read_file`, `list_dir`, and `search_memory` are only candidates: filesystem contents, permissions, and memory indexes can change, so opt-in requires per-invocation evidence that pins or establishes equivalent input/state; otherwise hold for reconciliation. Mutating/external tools (`edit_file`, `write_file`, `exec`) remain `unsafe` absent a separately established idempotency contract.
2. Before admitting each model request or tool call, durably persist bounded intent metadata with `operation_kind` (`model` or `tool`), a stable logical `operation_id`, a unique `attempt_id`, cycle, component/target, tool policy where applicable, schema/version and timestamp. Append completion only for that same pair. Model requests have no `safe` tool policy: because billing and remote completion may be uncertain, an unmatched model attempt is held for reconciliation, never automatically resent. Persistence failure denies admission. Never infer completion from elapsed time.
3. Serialize admission and reconciliation under a specified existing single-writer/bridge lock, after verifying every relevant admission/recovery writer cooperates with that exact lock; this proposal does not claim that the shared bridge lock currently covers every tool/provider entrypoint. Before classifying or reoffering an unmatched attempt, establish the old local executor/call is quiescent; a lock alone does not prove a remote provider stopped. A late terminal belongs only to its exact attempt and cannot settle a replacement. If quiescence, identity, required input/state, or effect outcome is uncertain, keep the attempt held and surface bounded evidence for authorized reconciliation; do not replay or mint a fresh budgeted attempt automatically. Unsupported lock, malformed/ambiguous legacy records, or persistence errors fail closed without state surgery.
4. A replay requires current authorization, the original authorized input (or evidence of equivalent input), remaining original budget/deadline and cancellation state. If private inputs are unavailable, do not reconstruct them from public telemetry; hold. Paid model calls and unsafe effects are never blindly retried. Keep records bounded and exclude raw prompts, tool arguments, secrets and private payloads; define schema, retention and size limits before implementation.
5. This proposal does not implement or absorb #1765 runtime recovery, #2162's stale-call design/acceptance (the separate proposed ADR-051), or #1989's separately owned real-subprocess SIGKILL/branch-resume regression. #2162 owns the stale incomplete-call reconciliation contract; #1765 owns its implementation and runtime behavior. These remain separate scopes with their own acceptance criteria. #2178 may stage one selected invocation path first, but that is sequencing only: all remaining intended paths/tests stay deferred or unmet in #2178, and the staged subset does not complete this proposal or authorize closure. No external orchestration service or shell/file-write auto-replay is proposed.

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
| The inventoried autonomous tool registry defaults to `replay_policy == "unsafe"`; each `safe` opt-in is justified by invocation state/equivalence evidence, not merely a read-only tool name | `tests/test_tool_replay_policy.py::test_tool_replay_policy_defaults_fail_closed` | deferred (#2178) |
| Recovery pairs intent and terminal records by logical operation and unique attempt; late terminals, ambiguous legacy records, and non-quiescent calls cannot settle or dispatch a replacement | `tests/test_tool_replay_policy.py::test_unmatched_intent_recovers_by_replay_policy` | deferred (#2178); missing terminal means unsettled/unknown effect, not proof of non-execution |
| A tool replay requires equivalent original input/state, quiescence, current authorization and remaining original budget/cancellation; changed mutable input is held | `tests/test_tool_replay_policy.py::test_replay_requires_equivalent_input_and_current_authority` | deferred (#2178) |
| Intent and terminal records persist bounded operation/attempt metadata without raw arguments, prompts, or secrets | `tests/test_tool_replay_policy.py::test_intent_records_exclude_raw_payloads` | deferred (#2178) |

# References

- Issues `#2178`, `#2177`, `#1765`, `#2162`, `#1989`; `ADR-050`, proposed `ADR-051` (separate #2162 decision), `ADR-055`.
- `@earendil-works/pi-durable` (`packages/durable/src/harness/tool.ts`, `replay: "safe" | "unsafe"`) inspires an explicit classification only; its semantics do not establish eeebot equivalence or recovery authority.
