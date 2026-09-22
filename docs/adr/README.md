# Architecture Decision Records

Numbered, immutable-once-accepted records of architecture decisions. Unlike
`docs/changes/` (per-change proposals and designs), an ADR captures a decision
and its rationale at a point in time; superseding requires a new ADR that links
back.

Conventions:
- File name: `ADR-NNN-short-slug.md`, NNN zero-padded, monotonically increasing.
- YAML frontmatter: `title`, `status` (proposed | accepted | superseded),
  `date`, `authors`, `related`, `tags`.
- Sections: Status, Context, Decision, Consequences, Alternatives considered,
  References. Retrospective ("as-built") ADRs are allowed and say so explicitly.

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-agent-architecture.md) | Agent architecture — roles, tools, models, and budgets of the eeebot runtime | accepted |
| [ADR-002](ADR-002-bounded-state-access.md) | Bounded state access | accepted |
| [ADR-003](ADR-003-operator-owned-agents-consolidation.md) | Keep AGENTS.md operator-owned and consolidate only declared-droppable sections | proposed |
| [ADR-004](ADR-004-validator-harness-parse-budget.md) | Validator harness disk-spool parse budget | accepted |
| [ADR-005](ADR-005-terminal-demand-attempt-count.md) | Count terminal demand cycles as futility attempts | proposed |
| [ADR-006](ADR-006-preserve-suppression-reasons.md) | Preserve suppression reasons separately in the scorecard | proposed |
| [ADR-007](ADR-007-deterministic-hypothesis-claim-identity.md) | Deterministic hypothesis claim identity and collision strengthening | proposed |
| [ADR-008](ADR-008-lesson-corpus-selection-keys.md) | The live lesson corpus is a retrieval surface; titles are its selection key and tags are being prepared to become one | accepted |
| [ADR-009](ADR-009-hypothesis-loop-yield-observability.md) | Report the hypothesis loop's verdict yield, and separate unverdictable from undecided | accepted |
| [ADR-010](ADR-010-memory-remainder-is-retrieved-not-resident.md) | The loop retrieves non-resident memory through one bounded FTS5 tool | proposed |
| [ADR-011](ADR-011-charter-voice-after-thresholds.md) | The charter keeps a voice after every threshold is met | proposed |
| [ADR-012](ADR-012-vector-1-scoped-to-the-mutation-surface.md) | Vector 1 names only what the loop is permitted to change | proposed |
| [ADR-013](ADR-013-capability-tiers-probe-and-cost.md) | A capability has a probe, a tier, and a measured cost on this host | proposed |
| [ADR-014](ADR-014-the-avatar-is-an-instrument.md) | A displayed avatar is an instrument, and attention is invited rather than captured | proposed |
| [ADR-015](ADR-015-the-channel-is-an-instrument.md) | The channel is an instrument — the cat may feel anything, and claim nothing the journal does not carry | proposed |
| [ADR-016](ADR-016-work-is-chosen-by-the-journal.md) | Work is chosen by the journal and never by the channel | proposed |
| [ADR-017](ADR-017-long-work-runs-outside-the-cycle.md) | Work too long for a cycle runs in its own unit, checkpointed, and shortens rather than skips | proposed |
| [ADR-018](ADR-018-the-harness-judges-the-instance-draws.md) | The harness judges and the instance draws; the seam is a published file, not an import | proposed |
| [ADR-019](ADR-019-the-baseline-is-the-fastest-thing-this-host-can-do.md) | An optimisation is measured against the fastest implementation this host can run | proposed |
| [ADR-020](ADR-020-direction-comes-from-reflection-over-a-span.md) | Direction comes from reflection over a span, never from an instantaneous error signal | proposed |
| [ADR-021](ADR-021-whoever-sees-the-performance-may-change-the-capability.md) | Whoever sees the performance may change the capability, after measuring whether it was used | proposed |
| [ADR-022](ADR-022-context-ontology.md) | Context ontology — one question, one file, one owner | accepted |
| [ADR-023](ADR-023-a-fact-shown-to-the-loop-is-one-the-loop-cannot-reach.md) | A figure shown to the loop as fact is one the loop cannot reach — provenance, not file ownership | accepted |
| [ADR-024](ADR-024-an-artifact-graph-has-typed-edges.md) | An artifact graph has typed edges, and only production use makes a component | accepted |
| [ADR-025](ADR-025-an-artifact-is-finished-when-something-depends-on-it.md) | An artifact is finished when something depends on its working — readiness by artifact kind, and how it reaches task selection | accepted |
| [ADR-026](ADR-026-the-day-is-a-cycle.md) | The day is a cycle — three clocks, a daily deliverable, and deep sleep as a named boundary | accepted |
| [ADR-027](ADR-027-work-is-ranked-by-rung-gained-per-measured-cost.md) | Work is ranked by rung gained per measured cost, and estimates are audited against outcomes rather than re-scored | accepted |
| [ADR-028](ADR-028-the-day-is-held-by-a-diary-the-loop-never-loads.md) | The day is held by a diary the loop appends to and never loads — second-tier continuity across the ~90 cycles of a day | accepted |
| [ADR-029](ADR-029-one-clock-the-day-is-host-local.md) | One clock — the day is host-local, everywhere | accepted |
| [ADR-030](ADR-030-a-cycle-is-a-hadi-loop.md) | A cycle is a HADI loop, and an unknown is a measurement to schedule | accepted |
| [ADR-031](ADR-031-the-cycle-is-a-box-and-the-increment-is-sized-to-it.md) | The cycle is a box of 80 ticks, the increment is sized to fill it, and a planning session opens it | accepted |
| [ADR-032](ADR-032-the-agent-chooses-its-own-work.md) | The agent chooses its own work; the outer contour holds the goal and judges the day | accepted |
| [ADR-033](ADR-033-release-owned-operator-skills.md) | Operator-owned skills ship in the release tree; loop-owned skills stay in the instance workspace | proposed |

## Acceptance

Each status word has one meaning:

- `proposed` -- guidance for humans and agents. Its rules are not a gate;
  nothing in CI enforces them yet. A proposed record may still be edited.
- `accepted` -- binding, and immutable from here on. Every item of the
  record's `# Test Contract` is enforced by a named test on `main`, or is
  explicitly deferred.
- `superseded` -- replaced. The record names the superseding ADR and is
  otherwise left as it was.
- `deferred (#NNN)` -- a marker on one contract item, not a record status:
  the test is owed and issue #NNN owns it.

A record moves `proposed -> accepted` when, and only when:

1. Every item in its `# Test Contract` names a test -- `tests/<file>.py` or
   `tests/<file>.py::<name>` -- that exists on `main` and cites the record
   (the string `ADR-NNN` appears in that test file), or carries the marker
   `deferred (#NNN)`. An item with neither is the reason the record is not
   accepted. Amending a proposed record's contract items to name their tests
   is part of accepting it.
2. The operator accepts, by a commit that flips frontmatter `status:` to
   `accepted`. Not the agent that wrote the record, and not the loop.
3. The record carries its own acceptance: date and accepting change (PR
   number or commit) under `# Status`.

`tests/test_adr_acceptance.py` enforces rule 1 on every `accepted` record,
checks this index against each record's frontmatter, and prints contract
coverage for `proposed` records. A record without a `# Test Contract` section
is exempt from rule 1 (ADR-001). ADR-002, 004, 008 and 009 were accepted
before this procedure existed; their unmapped items are reported as expected
failures, not enforced, until the operator maps or defers them.
