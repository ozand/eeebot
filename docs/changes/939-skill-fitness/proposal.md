# #939 — Skill Fitness Sidecar + Surface/Path Widening

## Summary

Implements four of the five parts of #939 (B/instance-migration excluded):

- **A** — Open `skills/` prefix and root `AGENTS.md` in bridge classifier, legacy
  validator, and `llm_proposer`; `goals.md` remains explicitly denied.
- **C** — Standalone stdlib `nanobot/runtime/skill_fitness.py` (≤300 LOC); runtime
  deny-set entry; `FITNESS_SIDECARS` sidecar in scorecard; `ReadFileTool` optional
  callback; `SubagentManager` skill-fitness context; bridge collects reads only
  after successful integration with birth-use guard.
- **D** — SKILL template documentation for stdlib/no-uv PEP 723/noninteractive
  scripts.
- **E** — Loop subagent excludes weather/tmux/clawhub from skills summary without
  changing ContextBuilder defaults; workspace/instance skills never auto-load;
  prompt ordering fix (skills before memory).

## Changed files

- `nanobot/runtime/bridge.py` — `_ALLOWED_PATH_PREFIXES` + `_ALLOWED_EXACT_PATHS`
  + `_classify_mutation_surface` exact-path bypass + `SubagentManager(...)` skill-
  fitness + excluded-skills args + `collect_skill_reads` post-integration.
- `nanobot/runtime/llm_proposer.py` — mirror of path prefix/exact-path changes +
  prompt string updates + `validate_proposal` exact-path bypass.
- `nanobot/runtime/scorecard.py` — `skill_fitness/reads.json` added to
  `FITNESS_SIDECARS`.
- `nanobot/runtime/runtime_deny.py` — `nanobot/runtime/skill_fitness.py` added to
  `_RUNTIME_DENY_ALWAYS_FILES`.
- `nanobot/runtime/skill_fitness.py` — new stdlib module (233 LOC).
- `nanobot/agent/subagent.py` — skill-fitness context params + `collect_skill_reads`
  method + `excluded_skill_names` param + `_build_subagent_prompt` exclusion wiring.
- `nanobot/agent/tools/filesystem.py` — `ReadFileTool` `on_skill_read` optional
  callback.
- `nanobot/agent/skills.py` — `build_skills_summary(excluded_names=)` param +
  `get_always_skills()` workspace guard + `source` attribute in XML output.
- `nanobot/agent/context.py` — `build_system_prompt(excluded_skill_names=)` param +
  prompt ordering fix (skills before memory).
- `docs/changes/939-skill-fitness/proposal.md` (this file).
- `docs/changes/939-skill-fitness/skill_stdlib_template.md` — Part D SKILL template.

## Design decisions

### Birth-use guard (Part C)

A subagent that writes a SKILL.md and immediately reads it in the same cycle earns
no fitness credit (`confirmed=False`).  The guard is implemented via a git ancestry
check: if `skill_commit` (last commit touching the SKILL.md) is NOT an ancestor of
`cycle_base_sha` (the sha the cycle branched from), the skill was created/modified
in THIS cycle → `confirmed=False`.

Persistence happens AFTER the integration outcome is known (post `_integrated`
check in bridge.py), so a non-integrated cycle's reads are discarded entirely.
The bridge's spawn-boundary integrity hash window covers the pre-spawn→pre-gate
period; the `collect_skill_reads` write lands outside this window and is never
flagged as a tamper incident.

### Prompt ordering (Part E)

Previous ordering: identity → bootstrap → memory → active-skills → skills-summary.
The 24 KB cap could push the skills catalogue off the end when memory was large.
Fixed ordering: identity → bootstrap → active-skills → skills-summary → memory.
Memory is still included; it just yields space to the skills catalogue.

### Workspace/instance always guard (Part E)

`SkillsLoader.get_always_skills()` now skips skills with `source='workspace'`.
Only builtin/operator-installed skills may carry `always=true`.  This prevents an
instance from injecting arbitrary always-loaded content into every future subagent
context by writing a SKILL.md with `always=true`.
