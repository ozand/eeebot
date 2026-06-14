# EeeBot Lessons & Error Registry

This directory is the global, agent-native source of truth for reusable lessons and error patterns for the `eeebot` and `nanobot` runtime.

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
