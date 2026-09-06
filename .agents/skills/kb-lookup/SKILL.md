---
name: kb-lookup
description: Look up an error fix using deterministic project-local-first precedence and an optional explicitly configured read-only shared store. Use whenever a command fails or behavior is unexpected before retrying.
---

# kb-lookup — search for an error fix

Use this skill immediately after an error and before guessing or retrying. Follow the [lesson ownership and routing policy](https://github.com/ozand/kb-bootstrap/blob/main/docs/LESSON_ROUTING_POLICY.md); that document is the normative ownership and routing policy. This skill provides lookup-specific operational checks and never creates another store.

## Repository governance gate

Lookup is read-only, but any follow-up edit, capture, Issue mutation, commit, or pull request must first:

1. distinguish the consumer repository from an upstream/shared repository;
2. name the target explicitly as `owner/repository`;
3. run `kb-bootstrap doctor --repo <owner/repository>` and fail closed unless it returns `RESULT: OK`;
4. use a separate verified checkout/worktree for upstream changes.

Before claiming a follow-up complete, require `kb-bootstrap check-completion --repo <owner/repository> --commit <commit> [--pr <number>]`. Do not expose private paths, credentials, payloads, or runtime state in the result.

## Store configuration

Read `lesson-stores.json` at the repository root when it exists. Do not discover lesson stores
from absolute paths, sibling directories, Git remotes, or machine-specific state.

If the file is absent, report `RESULT: BLOCKED (no lesson stores configured)` and stop lookup without exposing paths. This generated skill does not imply that a local lesson store exists.

Supported entries are:

```json
{
  "version": 1,
  "capture_store": "local",
  "local": {"path": "kb/lessons"},
  "shared": {"path": "an-explicitly-configured-path", "read_only": true}
}
```

The `shared` entry is optional and must be explicitly configured. It is lookup-only.

## Lookup order

1. Treat `local` as available only when its configured directory, `SCHEMA.md`, and
   `index.yaml` all exist; otherwise report the local source as unavailable without
   printing its path.
2. If local is available, search its `index.yaml` first.
3. If there is no local match and `shared` is explicitly configured as read-only,
   search its `index.yaml` second.
4. If the shared store is missing or unavailable, report that sanitized source as
   unavailable and finish the local result; do not perform hidden discovery or write.
5. If configuration is malformed or ambiguous, stop and report a blocking,
   sanitized configuration error.
6. For a match, read the referenced lesson and apply its `## Resolution` and
   `## Prevention` sections.

Report provenance as `local` or `shared` plus the lesson ID. Do not expose private
absolute paths or lesson payloads in receipts. Lookup never writes, synchronizes,
promotes, or publishes lessons.
