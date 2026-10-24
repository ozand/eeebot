---
title: Accept bounded delivery deviations only with independent evidence and truthful acceptance
status: proposed
date: 2026-09-22
authors: [eeebot maintainers]
related: ["#2116", "ADR-008", "ADR-035", "ADR-037", "ADR-041"]
tags: [process, authority, delivery]
---

# ADR-042: Accept bounded process deviations only with truthful evidence

**Status**: Proposed
**Date**: 2026-09-22
**Authors**: eeebot maintainers
**Supersedes**: None
**Related**: ADR-008 (delegated technical authority), ADR-035 (planning and closure), ADR-037 (handled disposition), ADR-041 (web fetch safety)

## Context

Issue #2116 has owner approval for the current bounded technical delivery plan to proceed end-to-end without repeated GO prompts. Existing project routing separates authority from capability, requires exact-revision independent review, and reserves new scope, protected access, dangerous operations, and other explicit decisions. Existing ADR-008 already delegates broad technical execution; this record does not duplicate or amend its global scope.

A missing project-level rule is how to disposition a process-only deviation while preserving evidence and the original outcome criteria. Without an explicit bound, a limited deviation could either be incorrectly treated as satisfying every criterion or block otherwise authorized work as if every procedural departure were a substantive failure. This decision is limited to technical policy delivery and does not amend existing ADR statuses or acceptance history.

## Decision

An explicitly approved technical plan may proceed through its agreed technical delivery lifecycle without repeated approval prompts. For a necessary technical ADR within that approved outcome, the authorized delivery executor may accept the ADR only after applicable checks and independent review PASS the exact final revision, recording the delegated authority and reviewed revision. This is technical acceptance under delegation, not a claim that a human reviewer or operator personally accepted it. A process-only deviation may be technically accepted only when an independent reviewer assesses the exact final revision and retained evidence shows there is no substantive outcome failure, ongoing exposure, guard/access bypass, or unresolved reserved decision. Record every original acceptance criterion as met, partial, or unmet against its own evidence. A deviation never turns partial or unmet into met, rewrites history, waives substantive outcomes, or bypasses required checks and review. Unresolved substantive criteria or reserved authority keep the task open. Escalate only new scope or product outcome, protected access, dangerous irreversible action, unapproved production operation, or an explicit reserved decision.

### What this is not

This is not blanket authority for new scope, changed product outcomes, protected access, host/production action, guard bypass, or closure with unresolved substantive acceptance. It does not change ADR-035, ADR-037, ADR-041, or any existing ADR status.

## Consequences

### What gets easier

A narrowly bounded process deviation can be assessed without a redundant approval cycle while preserving a checkable record of outcome status and evidence.

### What gets harder

Reviewers must distinguish process-only deviation from substantive failure and retain criterion-specific evidence; ambiguous cases remain open rather than being rounded up.

### What does not change

Exact-final-revision independent review, applicable checks, explicit scope, protected-operation gates, and truthful acceptance evidence remain mandatory. Technical ADR acceptance may be delegated only within an explicitly owner-approved outcome and never represents personal human review or operator acceptance.

## Alternatives Considered

- **Treat every original criterion as fully met on deviation acceptance.** Rejected because it falsifies outcome evidence and erases partial/unmet status.
- **Require a new GO for every delivery step or any process deviation.** Rejected for the already-approved technical plan because it adds a repeated gate without changing reserved decisions or the evidence standard.
- **Do nothing.** Rejected because the gap permits inconsistent acceptance of the same bounded process-only deviation.

## Test Contract

| Claim in Decision | Test | Currently |
|---|---|---|
| Approved ordinary delivery needs no repeated GO, while reserved decisions still escalate | Manual scenario in #2116 review receipt | not yet written |
| A necessary technical ADR may be accepted by the delegated executor only after checks and independent PASS on its exact final revision, without claiming operator or human-reviewer acceptance | Manual scenario in #2116 review receipt | not yet written |
| A limited deviation preserves original per-criterion met/partial/unmet evidence and cannot bypass review or guards | Manual scenario in #2116 review receipt | not yet written |
| Unresolved substantive criteria or reserved authority keep the task open | Manual scenario in #2116 review receipt | not yet written |

## References

- Issue #2116 and its research record: https://github.com/ozand/eeebot/issues/2116
- ADR-008 — accepted global technical delegation, reused without changing its status or scope.
