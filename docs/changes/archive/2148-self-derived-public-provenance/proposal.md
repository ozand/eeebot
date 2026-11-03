# Change: Publish producer-established self-derived priority provenance

- **change-id:** `2148-self-derived-public-provenance`
- **issue:** #2148
- **capability:** `docs/specs/subagent-bridge/spec.md`
- **role / workstream:** runtime producer / public state projection

## Problem

The typed dashboard projection withholds derived-priority label and direction unless the producer provides explicit `self-derived` provenance. The producer rows in PR #2054 predate issue #2148 and did not include that field, leaving the projection unable to distinguish those rows from unknown provenance.

## Intended change

Add `provenance: self-derived` to every derived-priority row serialized by `build_derived_view`, based on the goal-review-owned derived-priority source. Preserve the existing projection allowlist and keep operator priority wording out of the public view.

## Acceptance

- Every derived-priority projection row carries explicit self-derived provenance.
- Existing focused tests prove row provenance and retain operator-priority privacy.
- Focused producer tests pass on the final PR revision.

## Out of scope

No dashboard changes, operator-priority text exposure, host operations, deployment, or external-repository writes. PR #2054 predates #2148; this change record documents the follow-up contract, not authorship chronology.
