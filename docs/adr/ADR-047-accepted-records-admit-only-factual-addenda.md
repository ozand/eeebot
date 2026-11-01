---
title: Accepted ADRs admit only append-only factual addenda
status: proposed
date: 2026-11-01
authors: [eeebot maintainers]
related: ["#2141", "ADR-030"]
tags: [process, governance, adr]
---

# Status

**Proposed.** This draft records a repository-wide policy clarification and is
not accepted or binding until the authorized decision owner accepts it through
the normal ADR process. Issue #2141 delegates implementation and drafting, not
operator acceptance.

## Context

`docs/adr/README.md` describes accepted records as immutable, while the installed
ADR lifecycle permits a dated appended clarification that does not alter the
decision. ADR-030 contains a dated addendum precedent. The policy needs to make
that narrow exception auditable without making accepted decisions or their
history editable. Reversing this repository-wide governance rule later would
require knowing which class of edits was permitted and how it was reviewed, so
the ADR reversal test is met.

## Decision

Keep the accepted ADR's original decision, status, and existing historical text
immutable. Permit only a dated, factual, non-decision addendum appended after
all existing content. An addendum must not add or imply a decision, acceptance,
status change, or authority; change, delete, or reorder existing content; or
make unsupported factual claims. A named independent reviewer checks the
complete `git diff <base>...<head>` against those conditions and verifies each
factual claim against its cited evidence. Reject any diff that violates them.
This is a human review guard, not an automated enforcement claim.

This decision defines documentation governance only. It does not itself accept
this ADR, authorize an addendum to any particular ADR, change any prior
acceptance criteria, or grant merge/closure authority.

## Consequences

### What gets easier

Authors and reviewers have a precise test for a permissible post-acceptance
clarification, consistent with ADR-030 and the installed lifecycle guidance.

### What gets harder

Every proposed factual addendum needs an independent full-diff review and
traceable support; append-only placement alone is insufficient.

### What does not change

Accepted decisions and their prior history remain immutable. Normal ADR
acceptance authority and all issue/PR review gates remain in force. No automated
policy enforcement is introduced.

## Alternatives Considered

- Forbid all additions after acceptance: preserves the README's strictest
  reading but unnecessarily excludes the existing ADR-030 precedent and the
  installed lifecycle's non-decision clarification.
- Allow general amendments in place: rejected because it would blur the
  decision's original status and history, contrary to the accepted-record
  immutability rule.

## Test Contract

| Claim in Decision | Test | Currently |
|---|---|---|
| A permitted factual append can be distinguished from a rewrite or decision-changing append | Independent reviewer inspects `git diff <base>...<head>` using the human review guard in `docs/adr/README.md`, validates a positive factual append and rejects negative mutation examples | Review procedure documented; independent review of this proposed policy is pending |

## References

- Issue #2141, including the research record at https://github.com/ozand/eeebot/issues/2141#issuecomment-6557053677
- ADR-030, dated addendum precedent
- Installed ADR lifecycle, `references/lifecycle.md`
