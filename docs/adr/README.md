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
