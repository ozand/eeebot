---
title: Keep AGENTS.md operator-owned and consolidate only declared-droppable sections
status: proposed
date: 2026-09-05
authors: [eeebot maintainers]
related: ["#1188", "#1193", "#1300", "#1313"]
tags: [runtime, prompt, operations]
---

# Status

Proposed — implemented in the change governed by #1313, pending merge and rollout.

# Context

#1188 measured 20 autonomous integrations in five days whose only substantive change was appending to the instance `AGENTS.md`, against six deleted lines. #1193 then made the file operator-owned. #1300/#1302 replaced positional truncation with declared-droppable sections and a loud overflow. On 2026-09-05, #1313 measured only 732 prompt characters of slack plus 2,948 characters of still-droppable reserve.

The remaining decision is who may remove accumulated guidance. Giving the autonomous loop deletion authority over the file would also let it delete charter, staging, and forbidden-path instructions that the prose-blind gate cannot evaluate.

# Decision

`AGENTS.md` remains operator-owned. Autonomous proposals and diffs targeting it remain rejected as `operator_owned_path`.

Removal is an explicit operator action through `scripts/agents_md_consolidate.py`. The operator names each `## ` section. The tool removes a section only when there is exactly one matching heading and that section carries the exact `<!-- prompt-fit: droppable -->` declaration. It is dry-run by default, requires `--apply`, and replaces the file atomically. Missing, duplicate, or unmarked headings reject the whole operation without a partial write.

The loop never imports or invokes this tool, and `agents_md_consolidate.py` is itself in the runtime's `_BLOCKED_EXACT_PATHS`: both proposal sizing and the integration gate reject an autonomous edit to the operator's removal mechanism. Raising the system-prompt cap is not part of the decision.

The product gate's `if f == 'AGENTS.md'` check governs only a repository-root path. The growth measured by #1188 occurred in the separate instance repository; that instance `AGENTS.md` is governed by the instance-side path rules delivered with #1193, not by treating every nested `AGENTS.md` in this product repository as operator-owned.

# Consequences

## What gets easier

The prompt ledger reports the remaining droppable reserve directly, and an operator can convert declared reserve into permanent file reduction without hand-editing section boundaries.

## What gets harder

Consolidation requires an operator to choose sections deliberately. The tool cannot decide semantic redundancy and will not remove a critical section even when the operator believes it is obsolete; the marker must first be reviewed and added in the instance repository.

## What does not change

Critical sections are never dropped by prompt fitting or removed by the consolidation tool. The autonomous loop keeps learning through skills, lessons, and memory, but cannot mutate `AGENTS.md`.

# Alternatives considered

- **Autonomous age- or size-based deletion:** rejected because age and size do not encode importance and the gate cannot distinguish redundant prose from a security invariant.
- **Automatic deletion of every section dropped from a prompt:** rejected because prompt-fit choice is a runtime budget decision, not authorization to mutate the source file.
- **Raise `NANOBOT_SYSTEM_PROMPT_MAX_CHARS`:** rejected because it moves the failure date while leaving append-only growth and invisible reserve consumption intact.
- **Do nothing:** rejected because the measured reserve has a finite exhaustion date.

# Test Contract

| Claim | Test | Status |
|---|---|---|
| Remaining declared-droppable characters are recorded after no, partial, and complete exhaustion | `tests/test_context_prompt_fit.py` reserve tests | passing |
| Both successful and overflow ledger rows carry `droppable_reserve_chars` | `tests/test_bridge_system_prompt_overflow.py` | passing |
| Every `system_prompt` row carries `sections` (one entry per assembled section, `0` when empty) and `sum(sections) + separators` reconciles to `chars` / `cap + over_by` (#1379) | `tests/test_system_prompt_sections.py` | passing |
| Dry-run never writes and `--apply` removes only explicitly named marked sections | `tests/test_agents_md_consolidate.py` | passing |
| Missing, duplicate, or unmarked headings reject without partial mutation | `tests/test_agents_md_consolidate.py` refusal tests | passing |
| Autonomous root `AGENTS.md` mutation remains rejected | `tests/test_mutation_surfaces.py`, `tests/test_llm_proposer.py` | passing |
| Autonomous edits to `agents_md_consolidate.py` are rejected by proposal sizing and integration policy | `tests/test_mutation_surfaces.py::test_agents_md_consolidate_script_is_immutable`, `tests/test_llm_proposer.py::TestValidateSizing::test_rejects_agents_md_consolidate_script`, `tests/test_runtime_slice.py::test_bridge_policy_mirrors_stay_synced_with_gate` | passing |

# Rollback

Revert the product commit to remove the metric and operator tool. An applied instance consolidation is separately reversible through the instance repository's git history.

# References

#1188, #1193, #1300, #1313; `docs/specs/subagent-bridge/spec.md`.
