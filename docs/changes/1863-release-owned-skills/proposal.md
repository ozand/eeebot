# Release-owned operator skills

## Why

Issue #1863 found that operator and loop skills share `skills/`, a loop-writable instance surface. `SKILL.md` cannot be protected through basename-only immutable rules.

## Scope

Move only `eeebot-agent-work-review`, `memory-lookup`, and `run-tests` into the release package. The loader and generated index expose both ownership classes, with release skills taking precedence on collisions.

## Non-goals

Do not move the thirteen ambiguous skills, alter their content, add `ops/skills/`, or change the loop's existing `skills/` commit surface.

## Verification

Prove loader precedence and visibility, source-labelled index output, gate refusal for release-tree paths, and removal of inert `author` metadata from the three copied skills.
