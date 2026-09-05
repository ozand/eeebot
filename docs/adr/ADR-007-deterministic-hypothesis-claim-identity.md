---
title: Key durable hypothesis novelty by problem condition and mechanism
status: proposed
date: 2026-09-05
authors: [eeebot maintainers]
related: ["#903", "#999", "#1345"]
tags: [hypotheses, identity, observability]
---

# ADR-007: Key durable hypothesis novelty by problem condition and mechanism

## Status

Proposed — implemented in #1345 pending merge and rollout.

## Context

The live `state/hypotheses/durable.json` snapshot from 2026-09-05 contains 20 rows but roughly four recurring themes: five harness-execution restatements, five stale-host-metrics restatements, six proposal-dedup restatements, and four genuinely distinct hypotheses. Existing exact-title and verb-invariant title checks treat renamed claims as novel. Five restatements can therefore consume the complete `TOP_N=5` candidate window.

Every strategist row already carries `hypothesis` and `action`. Together they describe the problem condition and the mechanism or target subsystem. The runtime must remain deterministic, stdlib-only, bounded, and fail-open on incomplete legacy rows.

## Decision

`append_hypotheses` retains exact-title equality as its cheap first check, then derives a deterministic claim key from recognized problem-condition and mechanism-target signals in the structured `hypothesis` and `action` fields. The title may help recognize vocabulary, but title wording and action verbs are not themselves identity.

When either structured field is absent, or the pair cannot be recognized, the writer falls back to the prior normalized-title identity. It does not guess and does not discard the entry.

A claim collision does not append a restatement. It strengthens the existing row by incrementing `seen_count`, appending unique evidence, and raising priority when the incoming priority is higher. It records one existing cycle-ledger event with reason `claim_collision` and both hypothesis IDs. Ledger recording is best-effort and cannot break the strategist's durable write path.

This decision is a write-time novelty boundary. It does not rewrite the live file, merge historical rows in place, call an LLM, introduce embeddings, or change lifecycle identity; lifecycle migration and fossils are governed separately by #1346.

## Alternatives Considered

- **Continue title/token dedup:** rejected because the observed 20-row corpus demonstrates that renaming crosses it repeatedly.
- **Generic stemming or token-overlap similarity:** rejected because a threshold that groups the observed restatements can also merge claims whose mechanism differs. The mechanism distinction is the contract, not general lexical similarity.
- **Embeddings or an LLM classifier:** rejected because they add latency, dependencies, cost, and nondeterminism on a constrained live cycle path.
- **Rewrite all 20 live rows during rollout:** rejected because live-state surgery is unsafe and unnecessary; future collisions are closed at the writer, while the fixture proves the identity rule.

## Consequences

### What gets easier

Renamed restatements stop crowding durable capacity and the proposer candidate window. Recurrence makes the retained hypothesis stronger rather than larger in count. Collisions are measurable in the existing ledger.

### What gets harder

The deterministic vocabulary must be extended deliberately when new problem/mechanism families appear. Unknown structured claims fail open to title identity and may remain duplicated rather than risk a false merge.

### What does not change

The 20-entry bound, atomic durable writer, exact-title first pass, generated `hypothesis_id`, strategist LLM contract, and lifecycle behavior remain unchanged.

## Test Contract

| Claim | Test | Status |
|---|---|---|
| Live harness 5→1, host metrics 5→1, dedup 6→2, distinct 4→4 | `tests/test_hypothesis_claim_identity.py::test_live_fixture_claim_groups_are_specific_not_universal` | passing |
| Incomplete structured rows retain title identity | `tests/test_hypothesis_claim_identity.py::test_missing_structured_fields_fall_back_to_title_identity` | passing |
| Collision appends no row, strengthens evidence/count/priority, and records both IDs | `tests/test_hypothesis_claim_identity.py::test_collision_strengthens_existing_record_and_records_both_ids` | passing |

## References

- #903 — verb-invariant title dedup
- #999 — strategist durable hypotheses
- #1345 — governing issue and measured live corpus
