---
id: PROJECT-0006
title: Verify the tool control plane before orchestration recovery
category: agent-orchestration
severity: high
tags: [herdr, shell, verification, orchestration]
status: active
created: 2026-10-07
updated: 2026-10-07
error_signatures:
  - "Herdr agent wait rejects a documented option"
  - "An agent appears stalled after a pane compaction warning"
  - "A repository lookup returns 404 or resolves the wrong repository"
---

# Verify the tool control plane before orchestration recovery

## Symptom

A recovery action based on stale command syntax, a misleading pane warning, an
expired wait budget, a shell mismatch, an ambiguous cancellation result, or an
assumed repository identity can diagnose the wrong failure or act on the wrong
project.

## Root Cause

Operator-facing skills and remembered session state are not authoritative proof
of the installed CLI behavior, current pane state, process exit, active shell,
or repository identity. Similar names and cross-repository work make assumptions
especially risky. A cancellation request is not confirmation that a child
process stopped; a 404 or permission response is evidence to reconcile against
the explicit repository and cwd, not proof that the target does not exist.

## Resolution

Use the installed command's bounded help output to establish current syntax
before waiting. The verified Herdr CLI uses `herdr agent wait <PANE_ID>
--until STATUS --timeout MILLISECONDS`; do not copy stale `--state` or
`--timeout-ms` forms from instructions without checking. When compaction or a
stalled prompt is suspected, inspect the specific pane's current status/output
before concluding it is stalled. Bound waits to the remaining task deadline and
reserve time for result inspection. Execute PowerShell commands in PowerShell,
not through Bash with PowerShell-only syntax. Treat cancellation as a request;
confirm terminal status separately. Before GitHub operations, verify the exact
`owner/repository`, git origin, and cwd; reconcile 404/wrong-repository evidence
before retrying.

## Prevention

- Check the installed CLI's `--help` when documented flags are rejected; update
  only the authoritative instruction owner after confirming a real mismatch.
- Distinguish compaction warnings from evidence of a stalled prompt by checking
  the named pane; do not infer state from a warning alone.
- Calculate each wait timeout from the remaining deadline, not the original
  budget; keep verification time available.
- Keep shell boundaries explicit and use native PowerShell for PowerShell
  commands.
- Record cancellation requested, process terminal state, and exit code as
  separate facts.
- Verify repository identity and cwd before issue/PR operations. A 404 or
  access error requires checking the explicit target and identity; do not
  silently switch repositories.
- Report only verified facts. Do not infer an EPERM root cause from the error
  alone or retry/delete runtime state without evidence and authorization.
