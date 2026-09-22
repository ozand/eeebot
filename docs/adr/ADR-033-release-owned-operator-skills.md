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
