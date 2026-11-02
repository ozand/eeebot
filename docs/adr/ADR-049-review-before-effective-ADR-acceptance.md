---
title: Require exact-revision review before ADR acceptance takes effect
status: proposed
date: 2026-11-02
authors: [delegated design executor]
related: ["#2152", "ADR-048", "AGENT_ROUTING.md"]
tags: [process, review, authority]
---

# ADR-049: Require exact-revision review before ADR acceptance takes effect

**Status**: Proposed
**Date**: 2026-11-02
**Authors**: delegated design executor
**Supersedes**: None
**Related**: #2152, ADR-048 (provenance contract), AGENT_ROUTING.md (delivery gates)

## Context

Issue #2152 identifies a lifecycle ambiguity between two documented rules:
ADR acceptance currently occurs by a status-flipping commit, while the delivery
gate requires an independent PASS on the exact final revision before acceptance
or merge. The status-flipping commit itself creates that candidate revision.
Treating that commit as immediate acceptance makes it impossible to review that
exact SHA before acceptance, and a later review cannot retroactively change
chronology.

The conflict is concretely visible in the open #2148 / PR #2150 ADR-048
candidate. PR #2150 head `afe0aa55e3d014743f03aa175becaf51df31c5fb` changes
ADR-048 to accepted, but its PR body reports the cited audit and CI against
published main `2e176812abad12e3e49f5e9270c5655648429992`; a review finding
says the acceptance revision itself lacks exact-SHA independent review. That
finding is evidence of a process gap, not a judgment about prior authority or
compliance. PR #2142/#2141 and ADR-047 address post-acceptance addenda and
immutability, not this sequencing question. No existing work is reclassified.

## Decision

### What this IS

An intended process rule: an ADR status-flipping commit is a provisional
candidate, not effective acceptance. Effective acceptance requires independent
review PASS and all applicable required checks on that exact candidate SHA,
followed by authorized merge of that unchanged reviewed revision. If its SHA
changes after review, fresh independent review and applicable checks must pass
on the new exact SHA before merge. The accepted record must make the sequence
and reviewed revision auditable without claiming acceptance before merge.

### What this IS NOT

This proposal does not itself change current process, accept ADR-049, authorize
any merge or issue disposition, grant operator authority, or retroactively
validate prior status changes. It does not alter the reserved operator role or
permit self-review, bypasses, blanket approval, or changed scope.

### Success criteria

- Status-flip chronology distinguishes a provisional candidate from effective
  acceptance after authorized merge.
- Independent review PASS and required checks bind to the exact SHA being
  merged; changed SHA invalidates prior evidence.
- Index and acceptance record state the chronology truthfully and do not imply
  acceptance before the merge event.
- Existing authority and issue/PR decisions remain unchanged.

## Consequences

### What gets easier

Reviewers can identify one falsifiable sequence and candidate SHA for the ADR
acceptance gate.

### What gets harder

The status value alone cannot communicate effective acceptance before merge;
contributors must retain exact-SHA review, check, and merge evidence and repeat
gates after any candidate change.

### What does not change

Independent review remains mandatory; operator-reserved authority remains
reserved; existing issues and PRs retain their own scope and chronology.

## Alternatives Considered

- **Keep status flip as immediate acceptance, review afterward.** Rejected:
  violates the exact-final-revision review-before-acceptance requirement and
  cannot be repaired retroactively.
- **Review the pre-flip revision and transfer approval to the flipped SHA.**
  Rejected: the reviewed content is not the exact candidate revision.
- **Do nothing.** Rejected: leaves contradictory documented ordering and makes
  the gate impossible to satisfy literally.

## Test Contract

| Claim | Test | Currently |
|---|---|---|
| Accepted ADRs cannot be treated as effective until required chronology is recorded | `not yet written` | not yet written; policy-only decision has no enforcement test in scope |
| Changed candidate SHA requires fresh exact-revision evidence | `not yet written` | not yet written; policy-only decision has no enforcement test in scope |

## References

- Issue #2152 and research comment https://github.com/ozand/eeebot/issues/2152#issuecomment-6587851209
- #2148 and PR #2150; candidate head `afe0aa55e3d014743f03aa175becaf51df31c5fb`
- PR #2150 review finding: https://github.com/ozand/eeebot/pull/2150#discussion_r4447256041
- #2141 / PR #2142 and ADR-047 (adjacent addenda policy)
