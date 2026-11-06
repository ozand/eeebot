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

Proposer fallback is disabled by default. Only an explicit operator-configured alternate through the same authorized gateway may be considered; absent, malformed, identical to primary, or unauthorized route means no alternate call. Do not invent a default alias. Activation and the alternate's data/trust and budget equivalence require the responsible owner's decision; this proposal does not establish either.

Allow at most one alternate attempt per proposer operation, and only after affirmative supplier unavailability: a 429 or 503 with no-deployment/cooldown evidence, or a documented long Retry-After. Define a finite threshold in implementation configuration and tests rather than treating every transient response as a cooldown. A 400, malformed request, authentication/authorization failure, invalid model, unknown error, parse/validation rejection, or non-supplier timeout must not trigger fallback. Never infer supplier cause from arbitrary provider text alone. The alternate receives the same authorized input and parameters, not a broader data set, and remains under the existing per-operation time and cost bound; no unbounded retry chain.

Record primary and alternate attempts, selected route and actual served model when known, and an explicit fallback count using bounded, non-secret categorical metadata. Never record prompt text, credentials, raw error text, or an invented served model. Missing evidence remains unknown. Failed alternate yields the existing unavailable/no-proposal path; it does not assign a task or stop the planner. Any necessary telemetry-schema change is part of this design decision and needs privacy/compatibility review before acceptance.

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
| Qualifying cooldown makes at most one alternate attempt; request/auth/unknown failures do not | `tests/test_llm_proposer.py::test_fallback_supplier_cooldown_only` | deferred (#2047) |
| Primary and alternate attempts plus known served model and count are observable without raw errors or secrets | `tests/test_llm_proposer.py::test_fallback_telemetry_privacy_and_attribution` | deferred (#2047) |
| Both failures preserve existing no-proposal behavior and bounded time/cost | `tests/test_llm_proposer.py::test_fallback_exhaustion_is_bounded` | deferred (#2047) |

# References

- #2047 issue and design research: https://github.com/ozand/eeebot/issues/2047#issuecomment-6699060287
- ADR-001: `docs/adr/ADR-001-agent-architecture.md`.
- Existing code: `nanobot/runtime/llm_proposer.py`, `nanobot/runtime/model_registry.py`, `nanobot/observability/llm_telemetry.py`.
