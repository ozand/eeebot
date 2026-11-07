---
title: Bound proposer fallback to explicitly authorized routes
status: accepted
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2047", "ADR-001"]
tags: [runtime, proposer, routing]
---

# Status

Provisional acceptance candidate under delegated technical authority; not effective or implemented. Proposed design `c3b7adae0b8dba05f20bd5b00d58ed5fa6fc051f` received independent privacy and consumer review PASS. This candidate becomes effective only after independent PASS and applicable checks on the exact final revision followed by authorized merge unchanged; the governing PR or issue retains those receipts. No route, credential, host configuration, deployment, or live call is authorized by this record.

# Context

#2047 item 2 delivered failed-call telemetry in PR #2049. Item 1 remains open: a proposer route can be in cooldown while another route might be available. The proposer is an optional candidate author, not the planner or executor; a proposer failure alone does not prove a cycle stopped. ADR-001 specifies a single model selection precedence; fallback after a failed call changes that behavior and its observable attempt evidence.

Current `llm_proposer.propose` uses one requested model and the existing LiteLLM gateway client. Failed-call telemetry records bounded categorical metadata. No alternate route or its trust/data equivalence has been established for activation. This decision does not select a provider or model.

# Decision

This is a partial proposer-only increment, not satisfaction of #2047's original ordered fallback lists per role or registry fallback test. Those broader obligations remain unmet and the issue remains open until separately implemented and verified.

Proposer fallback is disabled by default. Only an explicit operator-configured alternate through the same authorized gateway may be considered; absent, malformed, identical to primary, or unauthorized route means no alternate call. Do not invent a default alias. Activation and the alternate's data/trust and budget equivalence require the responsible owner's decision; this proposal does not establish either.

Allow at most one alternate attempt per proposer operation, and only after structured HTTP status 429 or 503 plus either the exact case-insensitive gateway marker `no deployments available for selected model` in a bounded 512-character diagnostic, or a valid numeric Retry-After at or above a proposed threshold of 60 seconds. A diagnostic longer than that bound or a missing/invalid status is ambiguous and does not qualify. Known request/auth/invalid-model evidence takes precedence and denies fallback even if a supplier marker is also present. The proposed threshold is a finite integer between 1 and 3600 seconds, inclusive; invalid threshold configuration fails closed before any alternate. Missing, non-finite, negative, malformed or HTTP-date Retry-After is unknown and alone never qualifies. A 400, malformed request, authentication/authorization failure, invalid model, unknown error, parse/validation rejection, or non-supplier timeout must not trigger fallback. Supplier signals must come from a bounded recognized classifier, not arbitrary provider text alone.

The current client has a per-call timeout and SDK automatic retries; a shared operation-wide time/cost bound is not established. For enabled fallback, propose one monotonic deadline of 120 seconds and at most two actual network attempts for the entire `maybe_propose` pass, spanning primary, alternate, sizing and dedup retries; disable hidden SDK retries. Each actual call consumes one attempt and receives at most remaining time. If no time or attempts remain, no further call is admitted. Provider exhaustion returns the existing unavailable/no-proposal result before sizing or dedup can retry it. A valid but rejected proposal may use a remaining attempt for existing sizing/dedup feedback, but cannot reset the deadline or count. Before each enabled-mode network attempt, reject an assembled system-plus-user input exceeding a proposed 32,768 UTF-8 bytes, including any sizing/dedup feedback; do not truncate or send it. The alternate for a given request uses the same accepted bytes as its primary; feedback on a later permitted attempt is included in that attempt's own cap. Keep existing `max_tokens=400` per attempt. These are not proof that cost is unchanged: up to two calls can cost more. The 120-second deadline is a cooperative client timeout, not a hard termination guarantee for blocking or cancellation-resistant SDK operations. This resource tradeoff and route data/trust equivalence require owner approval before activation; no live budget increase is authorized. Disabled mode preserves current behavior.

Use the existing private failed-call and successful-call telemetry streams, not a new telemetry service. Propose allowlisted bounded fields only: locally generated random UUID operation token, attempt index 1 or 2, fallback count 0 or 1, and operator-approved safe route alias. Persist the actual served model only if the gateway response value exactly maps to an operator-approved safe alias; otherwise record null/unknown. Safe aliases are explicit configured names matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` and must not contain URLs, credentials, or secrets; reject invalid configuration before any call and never log rejected values. Do not persist arbitrary response model strings or caller-provided correlation IDs. A requested route is never a substitute for an unknown served model. Preserve legacy rows/readers when fields are absent; new fallback metadata must contain no prompt text, credentials, raw error body, or arbitrary exception message. This does not remove or expand the existing separate `record_llm_prompt` prompt/response capture, retention or visibility. Fallback attribution must not depend on joining unrelated rows by timestamp. This is an additive persisted-format proposal, requiring privacy and consumer compatibility review before acceptance.

Disabled mode preserves existing fields, including legacy `model` behavior. In enabled mode, validate configured route aliases before every writer/log; never log rejected values. For legacy success `model`, emit the approved requested alias only when the response value is absent; if a response value maps to an approved alias, emit that alias; otherwise emit the approved requested alias, without claiming it is the actual served model. New `served_model` is the response-derived mapped alias only, otherwise null. Never pass arbitrary provider model text through legacy telemetry or prompt-capture metadata. The existing prompt/response capture itself is unchanged; no additional capture is authorized.

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

Source-only review at `e6524af87c4832e09739627804a3ba7b15ba6e91` found that `scripts/llm_calls_report.py`, `scripts/loop_metrics_report.py` and `nanobot/runtime/scorecard.py` consume selected fields from top-level daily call files; failure records live in a separate subdirectory. Existing `tests/test_llm_proposer.py` asserts the exact failure-field set. This bounded inventory is not proof of compatibility with unlisted or external consumers. #2173 must test legacy rows with absent fields, preserve named report aggregates, intentionally update the strict failure fixture, and exercise unsafe configured/response values without weakening privacy assertions.

| Claim | Test | Currently |
|---|---|---|
| Explicit alternate only; no alternate on disabled, invalid, duplicate or unauthorized config | `tests/test_llm_proposer.py::test_explicit_fallback_configuration` | deferred (#2173) |
| Structured 429/503 plus exact bounded gateway marker or valid threshold admits at most one alternate; conflicting defect evidence, ambiguous status and invalid or HTTP-date Retry-After deny | `tests/test_llm_proposer.py::test_fallback_supplier_cooldown_only` | deferred (#2173) |
| Only approved safe aliases and generated operation UUID enter new fallback metadata; unsafe inputs and provider model strings do not leak through enabled-path writers/logs; unknown served remains null | `tests/test_llm_proposer.py::test_fallback_telemetry_privacy_and_attribution` | deferred (#2173) |
| UTF-8 input cap rejects overflow before network; a single cooperative deadline and two-attempt cap span the entire proposer pass including sizing/dedup retries, with SDK retries disabled; exhaustion/cancellation cannot issue another call | `tests/test_llm_proposer.py::test_fallback_exhaustion_is_bounded` | deferred (#2173) |

# References

- #2047 issue and design research: https://github.com/ozand/eeebot/issues/2047#issuecomment-6699060287
- ADR-001: `docs/adr/ADR-001-agent-architecture.md`.
- Existing code: `nanobot/runtime/llm_proposer.py`, `nanobot/runtime/model_registry.py`, `nanobot/observability/llm_telemetry.py`.
