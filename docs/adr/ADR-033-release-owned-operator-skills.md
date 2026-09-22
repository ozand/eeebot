---
title: Operator-owned skills ship in the release tree; loop-owned skills stay in the instance workspace
status: proposed
date: 2026-09-22
authors: [ozand, eeebot maintainers]
related: ["#1863", "#1865", "ADR-022"]
tags: [skills, ownership, mutation-policy, runtime]
---

# Status

Proposed 2026-09-22 for #1863. No implementation has started. Operator approval is required before this record can become accepted or its implementation can begin.

# Context

#1863 establishes that operator and loop skills currently share the instance `skills/` tree. `MutationPolicy` permits `skills/` commits, while its basename-only immutable list cannot protect an individual `SKILL.md`. ADR-022 assigns release files to the operator and instance skills to the loop; this decision extends that existing ownership model to selected skills.

# Decision

The three unambiguous operator skills — `eeebot-agent-work-review`, `memory-lookup`, and `run-tests` — shall ship as release-owned package skills under `nanobot/skills/<name>/SKILL.md`. Loop-created skills remain only under the instance workspace `skills/<name>/SKILL.md`.

`SkillsLoader` shall expose the union of both roots. Release-owned skills win an identical-name collision and are labelled `release`; instance skills retain `workspace`. The derived `skills/index.md` shall use the existing loader and visibly label both sources. This is a read expansion, not a new hand-maintained catalogue.

The release root, outside the instance repository and its permitted commit prefixes, is the enforcement boundary. The gate must reject any attempted loop mutation whose resolved target is a release-owned skill. The instance-repository `ops/skills/` alternative is not chosen: it would make operator instructions deploy independently of the release tree and contradict #1863's stated release-tree boundary.

Only the three named skills move in this increment. The thirteen ambiguous skills stay in the instance tree until #1863 records an individual decision and reason. The inert `author` frontmatter is removed from the moved skills rather than treated as ownership authority. No skill content is otherwise rewritten.

## Consequences

### What gets easier

The loop can still discover and read both ownership classes, while release provenance makes the operator boundary visible and reviewable. Existing package-data rules already ship `nanobot/skills/**/*.md`.

### What gets harder

A release-owned skill changes through a product PR and deployment rather than a loop cycle. Loader precedence and the generated index need regression coverage. A migration needs a release deployment before the instance copies are retired.

### What does not change

The loop retains its current `skills/` commit surface for loop-owned skills. `skills/index.md` remains harness-generated after integration. #1865 stays blocked until this decision is accepted and implemented.

## Alternatives considered

- **`owner:` frontmatter:** rejected; it is editable by the loop in the current instance tree and cannot enforce ownership.
- **Add `SKILL.md` to immutable basenames:** rejected; it freezes every skill, including loop-owned ones.
- **Instance `ops/skills/`:** rejected; despite falling outside current prefixes, it is not release-owned and creates an independent deployment lifecycle.
- **Move all candidate operator skills now:** rejected; #1863 requires recorded decisions for the thirteen ambiguous skills before moving them.

## Test Contract

| Decision claim | Test | Currently |
|---|---|---|
| Release and workspace roots are both discoverable; release wins collisions | `tests/test_skills_release_ownership.py::test_loader_lists_both_roots_and_release_wins_name_collision` | not yet written |
| The gate rejects a loop attempt to mutate a release-owned skill | `tests/test_skills_release_ownership.py::test_gate_rejects_release_owned_skill_mutation` | not yet written |
| The derived index labels both sources and reuses loader output | `tests/test_skills_release_ownership.py::test_skills_index_labels_release_and_workspace_sources` | not yet written |
| The three selected skills remain readable and the `author` field is absent from their release copies | `tests/test_skills_release_ownership.py::test_selected_operator_skills_are_release_owned_and_readable` | not yet written |
| The compact index cost is reported before and after migration | `tests/test_skills_release_ownership.py::test_compact_index_cost_is_reported_before_and_after_migration` | not yet written |

# References

#1863; #1865; ADR-022; `nanobot/agent/skills.py`; `nanobot/runtime/skills_index.py`; `nanobot/runtime/mutation_policy.py`.
