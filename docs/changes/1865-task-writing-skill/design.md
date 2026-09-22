# Design

`roles/planner.md` uses a release-root substitution for the absolute task-writing skill path. The planner gets `release_root` so its existing `ReadFileTool` callback recognizes the release file.

Superseded by #1891 after production showed the model could ignore the read pointer until timeout. Bridge now pre-reads the immutable release carrier before planner spawn, injects it only into that run's planner task, and records source/size/digest in the existing planning-session event. The callback remains ordinary read observability, not the authority for contract delivery. This still avoids writing `skill_fitness` during the spawn bracket.

The full contract remains authored only in `nanobot/skills/task-writing/SKILL.md`; bridge supplies its bounded contents only to the planner run that applies it. It never enters executor or resident ontology prompts. Rollback is a normal revert: no instance files or state schema migration is required because the added ledger fields are optional.
