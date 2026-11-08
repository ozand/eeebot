---
title: Assemble system prompts from named hashed sections and record hash deltas
status: proposed
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2179", "#2177", "#2058", "#1930", "#2021", "ADR-022", "ADR-046", "ADR-055"]
tags: [runtime, context, prompts, observability]
---

# Status

Proposed; not yet effective or implemented. Governing design proposal for `#2179`.

# Context

`ContextBuilder` and role prompt builders (`proposer`, `planner`, `executor`, `reflector`) assemble system prompts from release ontology and dynamic workspace/priority blocks. `ADR-046` records resolved prompt budgets but not per-section change provenance. Named sections could help identify changes and may enable provider prefix caching, but cache benefits and current byte/order behavior must be measured rather than assumed. #2058 separately owns a reserved trust-boundary decision for selected-ref exporter execution and approved provenance; this proposal does not authorize, implement, or supersede that trust work.

# Decision

1. Represent role/loop prompt assembly as an ordered sequence/map of named sections while preserving the existing emitted prompt bytes and ordering in the first increment. Record the actual order; do not impose a static-first reorder as an incidental prerequisite. If later work reorders sections to stabilize a static prefix, it requires a separate scoped change, byte/behavior verification, and provider-specific measurement before claiming a cache benefit. This preserves the original desired investigation of section boundaries and potential prefix stability without assuming the benefit.
2. Compute deterministic UTF-8 `sha256` digests and character lengths only for sections explicitly classified as safe for that evidence surface. Do not publish digests, lengths, deltas, or content-derived fingerprints for private/operator-priority or otherwise sensitive sections: hashes of guessable text can disclose content by dictionary testing and equality correlation. Public provenance should omit these values without a per-section presence, name, order or length signal. Serialize an order value as the filtered sequence of non-sensitive section names only; do not expose original positions, omitted-section counts, or other signals of sensitive section presence/order. No raw prompt or private input is added to public or ledger provenance.
3. Bind an initial section snapshot and subsequent changed/removed (`null`) deltas to an actual model-call operation, explicit baseline ID, schema version, observed section order and release revision. Missing baseline is unavailable evidence, never an empty prompt. A digest is comparison metadata, not authentication, authority, or a reconstruction source; reconstruction may use only separately authorized retained input. This proposal does not resolve #2058's selected-ref exporter trust boundary: it neither chooses a trust anchor nor makes a hash authenticate exporter code or metadata.
4. Preserve `ADR-022`'s 15,500-character pool cap and `ADR-046` budget fields; no mid-conversation provider wire-format dependency on unsupported gateways. This ADR does not alter #2058's reserved exporter/trust-anchor scope or requirement for explicit operator decisions. It also does not absorb #1930/#2021 compaction corrections or the `#1989` recovery regression.

# Consequences

### Easier

For non-sensitive sections, operators can compare bounded per-section changes bound to actual calls. This does not identify sensitive section content or prove provider cache behavior. Gateway-specific cache benefit remains a hypothesis requiring measurement; hashes cannot reconstruct unavailable historical text.

### Harder

All in-scope role prompt builders must route named section assembly and provenance through a shared helper without changing current emitted bytes in the first increment. Sensitive-section omission, call/baseline binding, schema compatibility, and any separately authorized reorder add implementation and verification cost.

### What does not change

`ADR-022` file ownership, the 15,500-char release pool cap, `ADR-046` resolved budget fields, and operator-priority privacy rules remain unchanged.

# Alternatives considered

- **Store full rendered system prompts on every ledger row:** rejected due to storage bloat on `eeepc` and private operator-priority exposure risk.
- **Hash only the concatenated prompt string:** rejected because a single digest hides which input section drifted.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| First increment preserves existing assembled bytes/order and `ADR-022` pool invariants while representing named sections | `tests/test_prompt_ontology.py::test_named_sections_preserve_existing_prompt_bytes_and_pool` | deferred (#2179); byte-preserving contract, not an unapproved reorder |
| Deltas bind to the actual model call and baseline; changed/removed non-sensitive sections are represented, sensitive section names/fingerprints/lengths/presence/order do not leak into public provenance, and missing baselines remain unavailable | `tests/test_role_prompt_builder.py::test_prompt_section_hash_deltas_and_privacy` | deferred (#2179) |
| Any separately authorized static-prefix reorder is behaviorally verified and its cache claim is measured for the named provider rather than assumed | Named test to be selected in the separate reorder change | deferred; not required to stage the byte-preserving provenance increment |

# References

- Issues `#2179`, `#2177`, `#2058`, `#1930`, `#2021`, `#1989`; `ADR-022`, `ADR-046`, `ADR-055`. These linked issues retain their distinct ownership and criteria.
- `@earendil-works/pi-durable` (`packages/durable/docs/spec.md §2`, `SystemMessage.sections`).
