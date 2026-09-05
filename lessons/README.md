# EeeBot Lessons & Error Registry

This directory is the global, agent-native source of truth for reusable lessons and error patterns for the `eeebot` and `nanobot` runtime.

## Markdown catalogue (#1343)

Top-level `lessons/*.md` files are reusable lessons, not just YAML attachments.
`lessons/index.md` is generated with one link/title, prevention summary, and
controlled-tag row per file (excluding README and index). Run
`python -m nanobot.runtime.lesson_index --workspace <instance-repo>`; the daily
knowledge-curator unit also regenerates it. Missing prevention is labelled
`unavailable`, never omitted. Inputs are bounded to 200 files / 128 KiB per
lesson; exceeding the count preserves the previous index and reports unavailable.
The existing word-overlap retriever ranks these rows alongside YAML cards and
returns a path hint, not the Markdown body. Read that path explicitly when useful.

## Policies

- `lessons/errors.yaml` is the structured registry file containing entries of resolved incident failures.
- `lessons/errors/` contains Markdown-formatted cards for each error entry, describing symptoms, root causes, fixes, and prevention mechanisms.
- Both human operators and autonomous subagents must check these records when encountering new failures or troubleshooting stuck pipelines on the host.

## Layout

```text
lessons/
├── README.md
├── errors.yaml              # global structured error-pattern registry
└── errors/                  # human-readable Markdown cards per error
    ├── ERR-2026-06-14-001.md
    └── ...
```

## How to Record a New Error

1. Determine the root cause of the incident.
2. Generate a new ID (format: `ERR-YYYY-MM-DD-XXX`).
3. Add a structured entry to `lessons/errors.yaml`.
4. Create a matching Markdown card under `lessons/errors/<ID>.md`.
5. Reference the error ID in commit messages and system history logs.
