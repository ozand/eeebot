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
