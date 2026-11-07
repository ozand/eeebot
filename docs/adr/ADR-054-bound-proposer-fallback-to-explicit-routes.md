---
title: Bound proposer fallback to explicitly authorized routes
status: proposed
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2047", "ADR-001"]
tags: [runtime, proposer, routing]
---

# Status

Proposed design only. It is not effective or implemented; independent review and exact-revision acceptance gates precede implementation. No route, credential, host configuration, deployment, or live call is authorized by this record.

# Context

#2047 item 2 delivered failed-call telemetry in PR #2049. Item 1 remains open: a proposer route can be in cooldown while another route might be available. The proposer is an optional candidate author, not the planner or executor; a proposer failure alone does not prove a cycle stopped. ADR-001 specifies a single model selection precedence; fallback after a failed call changes that behavior and its observable attempt evidence.

Current `llm_proposer.propose` uses one requested model and the existing LiteLLM gateway client. Failed-call telemetry records bounded categorical metadata. No alternate route or its trust/data equivalence has been established for activation. This decision does not select a provider or model.

# Decision

This is a partial proposer-only increment, not satisfaction of #2047's original ordered fallback lists per role or registry fallback test. Those broader obligations remain unmet and the issue remains open until separately implemented and verified.

Proposer fallback is disabled by default. Only an explicit operator-configured alternate through the same authorized gateway may be considered; absent, malformed, identical to primary, or unauthorized route means no alternate call. Do not invent a default alias. Activation and the alternate's data/trust and budget equivalence require the responsible owner's decision; this proposal does not establish either.

Allow at most one alternate attempt per proposer operation, and only after affirmative supplier unavailability: a 429 or 503 with a documented no-deployment/cooldown signal, or a valid numeric Retry-After at or above a proposed threshold of 60 seconds. The proposed threshold is a finite integer between 1 and 3600 seconds, inclusive; invalid threshold configuration fails closed before any alternate. Missing, non-finite, negative, malformed or HTTP-date Retry-After is unknown and alone never qualifies. A 400, malformed request, authentication/authorization failure, invalid model, unknown error, parse/validation rejection, or non-supplier timeout must not trigger fallback. Supplier signals must come from a bounded recognized classifier, not arbitrary provider text alone.

The current client has a per-call timeout and SDK automatic retries; a shared operation-wide time/cost bound is not established. For enabled fallback, propose one monotonic deadline of 120 seconds for both routes, no hidden SDK retries, and at most two actual network attempts total. Each call gets at most remaining time; if no time remains the alternate is not admitted. The identical prompt and existing `max_tokens=400` are the input/output ceilings for each admitted attempt, not proof that cost is unchanged: up to two calls can cost more. This resource tradeoff and route data/trust equivalence require owner approval before activation; no live budget increase is authorized. Disabled mode preserves current behavior.

Use the existing private failed-call and successful-call telemetry streams, not a new telemetry service. Propose allowlisted bounded fields only: operation correlation token, attempt index 1 or 2, fallback count 0 or 1, configured requested route (maximum 256 characters under existing route policy), and `served_model` only from the gateway's actual response, otherwise null/unknown. A requested route is never a substitute for an unknown served model. Preserve legacy rows/readers when fields are absent; retain no prompt text, credentials, raw error body, or arbitrary exception message. Fallback attribution must not depend on joining unrelated rows by timestamp. This is an additive persisted-format proposal, requiring privacy and consumer compatibility review before acceptance.

Failed alternate yields the existing unavailable/no-proposal path; it does not assign a task or stop the planner. Cancellation propagates, and an ambiguous provider error fails closed without another route.

# Consequences

## Easier

An explicitly authorized alternate can preserve proposer availability through a documented supplier cooldown without hiding the fallback.

## Harder

Alternate routes can differ in data handling, capability and cost. Their equivalence and finite combined time/cost bound must be established before activation; fallback may still fail.

## What does not change

No default model, planner/executor route, credential, security boundary, retry budget, existing telemetry privacy, or host activation is changed by this proposal. Item 2 remains the separately delivered telemetry increment.

# Alternatives considered

- Keep one route: simplest, but leaves the explicit #2047 fallback outcome unmet during qualifying cooldown.
- Fallback for every error: rejected; request and authorization defects must not be hidden.
- Automatically choose any available model: rejected; capability, privacy and budget equivalence are not established.
- Retry indefinitely or switch gateways: rejected; neither is bounded nor authorized.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| Explicit alternate only; no alternate on disabled, invalid, duplicate or unauthorized config | `tests/test_llm_proposer.py::test_explicit_fallback_configuration` | deferred (#2047) |
| Qualifying cooldown and finite threshold admit at most one alternate; request/auth/unknown and missing, invalid or HTTP-date Retry-After do not | `tests/test_llm_proposer.py::test_fallback_supplier_cooldown_only` | deferred (#2047) |
| Primary and alternate attempts plus known served model and count are observable without raw errors or secrets | `tests/test_llm_proposer.py::test_fallback_telemetry_privacy_and_attribution` | deferred (#2047) |
| One monotonic deadline covers at most two network attempts with SDK retries disabled in enabled mode; exhaustion/cancellation cannot issue another call | `tests/test_llm_proposer.py::test_fallback_exhaustion_is_bounded` | deferred (#2047) |

# References

- #2047 issue and design research: https://github.com/ozand/eeebot/issues/2047#issuecomment-6699060287
- ADR-001: `docs/adr/ADR-001-agent-architecture.md`.
- Existing code: `nanobot/runtime/llm_proposer.py`, `nanobot/runtime/model_registry.py`, `nanobot/observability/llm_telemetry.py`.
