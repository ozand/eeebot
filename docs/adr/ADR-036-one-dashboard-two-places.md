---
title: One dashboard, two places — publish allowlisted agent-owned context after secret filtering
status: proposed
date: 2026-09-25
authors: [ozand, architect]
related: ["ADR-013", "ADR-018", "ADR-034", "#1899", "#1903", "#1917", "#2185", "ozand/eeebot-ops-dashboard#457", "ozand/eeebot-ops-dashboard#311", "ozand/eeebot-ops-dashboard#313"]
tags: [dashboard, observability, privacy, deploy, decommission]
---

# Status

Proposed 2026-09-25 on the operator's decision that day. The 2026-11-10
owner-authorized revision below replaces the original content-type publication
boundary before dependent policy implementation. Historical wording is retained
with an explicit current decision to preserve chronology. This ADR remains
Proposed: no policy implementation, independent acceptance, merge, or deployment
is claimed. The amendment requires independent design review before dependent
projection/render/scanner implementation. The original #1903 sequencing
condition applies to the implementation work it covered and is not expanded by
this policy clarification.

# Context

Two dashboards have drifted into separate products. The published generator
serves GitHub Pages; the existing host dashboard has separate operational
readers and is not retired by this policy decision. Prior records describe the
cycle/attempt/session data sources and bounded rendering architecture; this
proposal does not authorize reading new host data or add a collector.

The original 2026-09-25/26 decision designated requests, answers, reasoning,
operator priorities and other agent-produced text as host-only because content
type was treated as an adequate privacy boundary. On 2026-11-10 the owner
explicitly reversed that policy for this autonomous agent's own prompts,
requests, responses, recorded sessions (including history), goals, skills and
related context: they are open for public publication after actual secret
filtering. Text or age alone does not make this agent's own context private.
This is an owner-scoped publication decision, not a finding that every record
is free of credentials, unrelated personal data, or content requiring bounds.

A bounded projection and explicit path allowlist remain necessary because the
publisher must preserve sink integrity and reject unapproved artifacts. Secret
scanning and field-aware redaction must distinguish actual secret indicators
from ordinary content such as project paths, identifiers, and context prose.
Disabling the scanner, publishing unallowlisted paths, or treating every
content fingerprint as a secret would fail the owner's requested outcome.

The linked dashboard implementation task is #457. Its execution non-goal is no
bulk export of unprocessed historical artifacts during that increment; this is
not a content-type or age-based ban on eventual publication of this agent's own
recorded content. The precise owner clarification is recorded on #2185 and #457.
No actual raw payload is included in this proposal or its tests; later
verification uses synthetic examples.

# Decision

## 1. One public policy source

ADR-036 is the canonical policy for this dashboard's public/host sinks. The
implementation issue and dashboard tests refer to this decision; they do not
create a competing definition of private content. Owner-authorized content is
only this autonomous agent's own prompts, requests, responses, sessions and
history, goals, skills, and related context. Other projects' data and unrelated
third-party/personal data are not made public by this decision.

## 2. Content type is not a publication ban

The agent's own content listed above is eligible for publication through
explicitly allowlisted fields and paths after actual secret filtering. No rule
may withhold it solely because it is text, a prompt, a request/response, a
session/history artifact, or old. Bounded output and accurate provenance remain
required; eligibility does not mean unbounded copying or bulk archival export.

The implementation covered by #457 uses synthetic fixtures and does not bulk
export unprocessed historical artifacts. This increment-level operational
non-goal does not change the eligibility decision for historical own-session
content when it is later read through an authorized, bounded pipeline.

## 3. Security and data boundaries

- Preserve and test actual secret scanning/redaction for credentials, keys,
  tokens, passwords, authorization material, environment/auth-file contents,
  and equivalent secret patterns. A detected secret is redacted or makes the
  relevant publication fail closed according to the field contract.
- Preserve explicit published path and field allowlists, size/count bounds,
  output encoding, and scan-failure behavior. A permitted value does not grant
  a new source reader or access to a host/private source.
- **Bound the input sources, not a semantic privacy detector.** The eligible
  input universe for #457 is this agent's own project-authored goals, skills and
  context, plus its own recorded prompts, requests, responses and session/tool
  history. Do not feed known excluded sources—environment/auth/credential
  stores, other agents/users/projects, unrelated chats or connectors—into public
  fields. The same exclusion applies to content in this agent's own session
  record when the content is explicitly attributed to a known excluded source.
- If an adapter receives a mixed record with an authorized field and separately
  identified excluded-source content, omit/redact only that identified content
  when its provenance boundary permits separation; otherwise fail that record
  closed. Enforce this at existing producer/adapter input selection. Do not
  invent origin metadata, a new collector, universal DLP, consent registry,
  semantic detector, or per-record manual approval. If current source selection
  cannot distinguish an excluded input, do not feed that source to the public
  adapter; never infer provenance from text appearance.
- Open web/public third-party quotation is not automatically private. Ordinary
  names, email-like text, and project paths are not privacy heuristics. This
  rule does not claim to detect unknown personal data semantically, and secret
  regexes are not presented as a solution to that separate limitation.
- Separate secret matching from content-type fingerprints or structural
  markers. Expected agent-owned text is not a secret merely because it matches
  a marker for a request/session object; actual secret patterns still block or
  redact it. Do not disable unrelated scanner rules or weaken scanner integrity.
- Never execute, replay, or interpret published text as instructions during
  projection or publication.
- No host access, new collection, deployment, publication action, or change to
  the host/LAN access model is authorized by this ADR.

## 4. Prior records and chronology

The original ADR-036 rules below are historical context only where they conflict
with the above decision: rule 3's categorical exclusion of request/answer text,
its separate public input that never holds that text, the use of call-content
fingerprints as a reason to refuse publication, rule 5's general host-only
classification of the operator's own priorities, and the rejected alternative
against public call detail. Their recorded 2026-09-25/26 rationale is not
rewritten or erased; this Proposed revision records the owner's later reversal.

ADR-034 remains Proposed and retains its runtime/document ownership and reader
semantics. This decision does not change its rule that dashboard status surfaces
do not render operator priority text, nor does it change the meaning of goals
inside runtime decision-making. It changes only public publication eligibility
for the owner's own goal/priority text through #457's bounded public projection.
The separate status/log/error readers governed by ADR-034 remain unchanged.

## 5. Test contract for the dependent implementation

| Decision claim | Required synthetic test | Current |
|---|---|---|
| Owner-authorized synthetic prompt, request, response, session/history, goal, skill and context content passes the real public projection, renderer, scanner and publish dry-run when its path/field is allowlisted | `tests/test_two_sinks.py::test_public_agent_owned_content_survives_projection_render_and_publish_dry_run` | not yet written |
| Synthetic actual-secret canaries remain redacted or reject publication through the same path | `tests/test_two_sinks.py::test_public_content_secret_and_unrelated_data_boundaries` | not yet written |
| The real input adapter excludes known disallowed source classes; a synthetic mixed record drops only separately identified excluded content, while inseparable known-excluded content fails that record closed; ordinary names, open/public quotation and project paths are not heuristically stripped | `tests/test_two_sinks.py::test_public_content_source_boundary_and_mixed_record_handling` | not yet written |
| Synthetic secret canaries are redacted/rejected independently of source eligibility | `tests/test_two_sinks.py::test_public_content_secret_and_unrelated_data_boundaries` | not yet written |
| Unallowlisted paths and scanner errors remain fail-closed | existing `tests/test_two_sinks.py::test_publish_allowlist_refuses_unlisted_pages` and scanner-failure tests, with unchanged assertions | existing tests to verify on implementation candidate |
| No source/pipeline reads or executes actual session payloads during these synthetic tests | tests use synthetic fixture data only; no host/runtime input | required test constraint |

# Consequences

### What gets easier

The public dashboard may explain this agent's own bounded prompts, sessions,
goals, skills and related context instead of stripping them solely due to text
type or age. Explicit secret and path controls remain independently testable.

### What gets harder

Projection and renderer schemas must admit only named fields, while secret
scanning must remain effective without treating all expected content as secret.
Reviewers must verify both content survival and actual-secret exclusion through
the complete synthetic pipeline. Public publication is irreversible in practice,
so source field selection and bounds remain consequential.

### What does not change

No new host/private source access, data collection, deployment, host/LAN access,
credential handling, or third-party data authorization is introduced. Existing
operational issue scopes and #443's original acceptance criteria are unchanged.
ADR-034 remains Proposed; accepted ADRs are not edited. No actual artifact is
published by this design phase.

# Alternatives Considered

- **Keep all agent text host-only.** Rejected by the owner's explicit reversal;
  it repeats the type-based ban that this decision is meant to remove.
- **Disable the publication scanner or allow every output file.** Rejected:
  this would also remove actual-secret detection and path control, neither of
  which the owner waived.
- **Retain fingerprint-of-call-content rejection as a universal public ban.**
  Rejected: it conflates authorized own-content with secret material. Separate
  that detection from concrete secret patterns and continue scanning for actual
  secrets and unrelated private data.
- **Bulk export unprocessed historical files in this increment.** Explicitly
  out of scope as an operation; this does not disqualify bounded publication of
  this agent's own recorded history by age or artifact type after filtering.
- **Change ADR-034's status or rewrite its history.** Rejected: it is Proposed
  and this decision only adjusts the dashboard's public publication boundary;
  runtime reader semantics and status/log surfaces remain unchanged.

# References

- #2185, owner authorization and 2026-11-10 clarification.
- `ozand/eeebot-ops-dashboard#457`, dependent synthetic pipeline implementation.
- ADR-034, operator document/priority semantics (unchanged outside public
  publication eligibility stated here).
- ADR-036 original 2026-09-25/26 decision, retained as historical chronology.
