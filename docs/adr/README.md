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
