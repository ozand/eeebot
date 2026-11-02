# Design: Review-before-effective ADR acceptance

## Approach

1. Keep the change as a Proposed ADR until an independent exact-revision design
   review accepts the design for policy implementation. Proposed status records
   intent only; it does not make policy binding or enable implementation.
2. On an authorized policy implementation, revise `docs/adr/README.md` and
   `AGENT_ROUTING.md` together. A status-flipped candidate is explicitly
   provisional. Required checks and independent review PASS bind to its exact
   SHA. The acceptance record identifies that SHA and the review/check evidence.
3. Effective acceptance occurs only when the unchanged reviewed candidate is
   merged with authorization. If content changes, derive a new SHA and repeat
   review/check gates before merge. Do not rewrite historical chronology.
4. Preserve operator-reserved authority. This proposal does not amend or
   retroactively reinterpret #2148/#2150, #2141/#2142, #2139, or prior ADR
   status changes.

## Affected components

- `docs/adr/README.md` — future implementation of candidate and effective
  acceptance wording.
- `AGENT_ROUTING.md` — future alignment of exact-SHA review/authority wording.
- `docs/adr/ADR-049-review-before-effective-ADR-acceptance.md` — Proposed
  decision record for independent review.

## Trade-offs / alternatives considered

Immediate status flip is simple but contradicts review-before-acceptance and
makes ordering unverifiable. Pre-flip review cannot cover the final flipped SHA.
The provisional-candidate state adds a lifecycle distinction and evidence
requirements, but keeps both review-before-acceptance and exact-SHA integrity.

## Verification

Before policy implementation, independent review should test the proposed
sequence against: candidate status flip; successful exact-SHA review/checks;
any changed SHA; authorized merge of the unchanged reviewed SHA; and the
non-retroactive treatment of prior chronology. Policy implementation later
requires `lat check`, targeted documentation validation, and independent review
of the exact committed implementation SHA.

## Spec delta

No product capability spec changes. This is development process policy.
