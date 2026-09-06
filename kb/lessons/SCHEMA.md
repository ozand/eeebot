# Project-local lesson schema

Project-local lessons are Markdown files stored in `kb/lessons/` and catalogued by
`kb/lessons/index.yaml`. They belong to the current repository.

## Required frontmatter

```yaml
---
id: PROJECT-0001
title: "Short description of the lesson"
category: tooling
severity: medium
tags: [tooling]
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
error_signatures:
  - "Stable, sanitized part of the error"
---
```

Rules:

- IDs use `PROJECT-` followed by four digits and are unique within this repository.
- The filename starts with the same ID: `PROJECT-0001-short-slug.md`.
- The `id` in frontmatter, filename, and `index.yaml` entry must match.
- `error_signatures` must omit credentials, private payloads, temporary paths, PIDs,
  and other unstable values.
- A lesson is written to one explicitly selected store. Local and shared stores are
  never updated by the same capture operation.

## Required sections

```markdown
# Title

## Symptom

## Root Cause

## Resolution

## Prevention
```
