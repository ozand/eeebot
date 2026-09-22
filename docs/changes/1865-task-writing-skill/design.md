# Design

`roles/planner.md` uses a release-root substitution for the absolute task-writing skill path. The planner gets `release_root` so its existing `ReadFileTool` callback recognizes the release file.

The callback only appends an in-memory marker. After the planner integrity bracket closes, bridge checks that marker. A missing marker produces a `planning_session` refusal; a successful plan persists `task_writing_read: true` in its existing ledger event. This avoids writing `skill_fitness` during the spawn bracket.

The full contract remains in `nanobot/skills/task-writing/SKILL.md`; the prompt carries only its path. Rollback is a normal revert: no instance files or state schema migration is required because the added ledger field is optional.
