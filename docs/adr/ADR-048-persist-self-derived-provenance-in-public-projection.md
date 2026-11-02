# ADR-048: Persist producer-established provenance on self-derived public rows

**Status**: Proposed
**Date**: 2026-11-02
**Authors**: ozand, delegated design executor
**Supersedes**: None
**Related**: ADR-034 (priority sources), ADR-036 (typed dashboard projection), ADR-023 (provenance authority)

## Context

Issue #2148 follows an already-open draft PR #2054. The PR was created 2026-09-29, with head `727802fac74504e68552220dabc634d996da4f3e` based on `6d476b715aa5989d5036e1385f6262d0f2a70f4b`; issue #2148 was created later, 2026-11-02. The PR's producer field predates the issue and is not evidence of prior ADR approval. Current main at review time is `51653e771fae5cf4476af14da2df8751fd18df0e`.

The proposed change adds `provenance: self-derived` to persisted rows emitted in `state/public/derived_view.json`. That is a public persisted data contract and a trust claim: a consumer may use the tag to decide whether to reveal row label/direction. ADR-036 is still Proposed, ADR-034 is still Proposed, and neither specifies this exact producer-to-public-projection field contract. ADR-023 establishes the general principle that facts shown to the loop require provenance, but does not define this public dashboard field. No accepted ADR explicitly covers this exact field, scope, and trust boundary.

The dashboard #378 is merged (2026-09-29) and its D1.1 typed projection explicitly withholds label/direction absent a validated `self-derived` tag. PR #2054's change is narrowly in `build_derived_view`; its added tests are `tests/test_derived_view_writer.py::test_every_derived_priority_row_carries_self_derived_provenance` and `tests/test_demand.py::TestPublishDerivedView::test_operator_v2_outranks_self_derived_v1_with_provenance_and_direction`. PR body reports CI passing on its old head; that is historical evidence, not validation of the rebased head or a new implementation.

The writer path stated in #2148 and PR #2054 is `goal_review._write_derived_priorities`, registered as sole writer in `state_paths.py`, writing accepted goal-review candidates. This task has not independently inspected private runtime state or host behavior; this design relies on the cited source-code claim, to be rechecked during implementation/review.

## Decision

### What this IS

Persist the fixed tag `provenance: self-derived` on every derived-priority row serialized by `build_derived_view`, because that producer's input rows come from the goal-review-owned derived-priority document. Provenance is producer-established metadata; it is not inferred by the dashboard from row shape or missing fields. Preserve the existing typed dashboard allowlist as a read-only consumer; this change authorizes no consumer-side writes.

### What this IS NOT

This does not add or expose operator priorities, `goal_text`, raw operator wording, private runtime history, or additional projection fields. It does not widen the dashboard allowlist or authorize external repository writes, host access, deployment, gates, task selection, or any new runtime authority. It does not decide or accept ADR-034/036 as a whole.

### Success criteria

- Every producer-emitted derived-priority row has exactly the explicit self-derived provenance value.
- Synthetic regression tests fail if that tag is omitted; other priority-item output remains unchanged.
- No operator-priority text is introduced by this field, and dashboard behavior remains bounded by its existing validated typed allowlist.

## Consequences

### What gets easier

The dashboard can distinguish producer-established self-derived rows from unknown provenance and display only fields admitted by its existing schema.

### What gets harder

Producer and consumer must keep the provenance vocabulary aligned. Any future writer or mixed-origin input cannot inherit this tag without establishing the same trust claim; tests and review must guard that boundary.

### What does not change

The producer remains the sole source of this metadata; the dashboard remains a read-only projection consumer. Operator priority privacy and the existing projection allowlist are unchanged. No host or external-dashboard mutation is implied.

## Alternatives Considered

- **Infer self-derived provenance in the dashboard.** Rejected: an absent tag is ambiguous, and a consumer inference would turn row shape into authority contrary to the explicit-provenance D1.1 contract.
- **Leave provenance absent and withhold label/direction.** Rejected for this narrow producer path: it preserves fail-closed behavior but prevents the typed projection from presenting the writer-established self-derived rows.
- **Do nothing / broaden projection.** Doing nothing leaves the public typed projection unable to show these fields; broadening it or passing through unknown data would weaken the established allowlist boundary and is outside scope.

## Test Contract

| Claim | Test | Currently |
|---|---|---|
| Every derived-priority row carries explicit self-derived provenance | `tests/test_derived_view_writer.py::test_every_derived_priority_row_carries_self_derived_provenance` | Exists on pre-existing PR #2054 head; must be verified on final implementation head |
| Existing priority-item output remains correct when derived provenance is serialized | `tests/test_demand.py::TestPublishDerivedView::test_operator_v2_outranks_self_derived_v1_with_provenance_and_direction` | Exists on pre-existing PR #2054 head; must be verified on final implementation head |
| Charter provenance remains release-root-only and never leaks goal_text | `tests/test_derived_view_writer.py::test_a_release_goals_md_read_is_the_only_release_goals_md_tag`, `test_b_goals_md_absent_with_goal_text_present_is_never_tagged`, `test_c_merged_derived_inputs_without_release_goals_md_are_never_tagged` | Existing tests on pre-existing PR head; regression guards, not newly attributed to this decision |

## Rollback

Remove the added persisted field from the producer and its assertions. The dashboard then resumes its existing fail-closed behavior and withholds label/direction for rows lacking explicit provenance; no host state migration is required by this design.

## References

- #2148 and its research comment `6572249724`.
- #2054 (draft, created before #2148; head/base recorded above); its existing implementation and tests are research evidence, not approval.
- `ozand/eeebot-ops-dashboard#378` (merged D1.1 typed projection contract).
- ADR-023, ADR-034, ADR-036; `nanobot/runtime/demand.py`, `nanobot/runtime/goal_review.py`, `nanobot/runtime/state_paths.py`.
