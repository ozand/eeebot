---
id: PROJECT-0001
title: "A probe that reads the wrong key or the wrong tree disproves a correct hypothesis"
category: diagnostics
severity: high
tags: [measurement, verification, diagnostics]
status: active
created: 2026-09-06
updated: 2026-09-06
error_signatures:
  - "0 of 579"
  - "assert 0 =="
---

# A probe that reads the wrong key or the wrong tree disproves a correct hypothesis

## Symptom

A measurement returns a clean, confident number — very often zero — and it is acted on
immediately because nothing crashed. The number answers a different question than the one
asked.

Seven occurrences in one day, across four agents and the operator:

| Read | Meant | Consequence |
|---|---|---|
| `entries[id].model` | `entries[id].escalated.model` | Correct root-cause hypothesis retracted; an hour spent on a mechanism that does not exist |
| `usage.prompt_tokens` | top-level `total_tokens` | Nearly published "token accounting is not recorded" |
| Catalogue vs the release tree | vs the instance tree | 8 skills / 2,834 chars instead of 30 / 12,320 |
| `AGENTS.md` in a stale worktree | the live instance copy | Reported 24,081 bytes; real files are 9,540 and 8,395 |
| Lifetime blocks per item | blocks *before* its success | Threshold cost overstated threefold |
| Saving estimated from a path prefix | the live prompt-fit row | Predicted 838 chars headroom; actual 513 |
| A terminal pane at the wrong scroll offset | the agent's actual report | Concluded an agent had delivered nothing while it awaited authorisation |

## Root Cause

This project keeps files with identical names in four places at once — the runtime repo, the
instance repo, the deployed release, and several worktrees — and records with nested,
similarly named keys. A probe that names none of these explicitly picks one by accident.

A zero from the wrong key is structurally indistinguishable from a real zero. It reads as
evidence of absence, which is exactly the kind of evidence that ends an investigation rather
than starting one.

## Resolution

State three things before believing any measured number:

1. **The file** — full path, and which tree it lives in.
2. **The key** — the exact path into the structure. Print one whole record and read its keys
   before aggregating over thousands.
3. **The quantity** — "blocks per item" and "blocks before success" are different numbers
   with the same name.

## Prevention

A zero is only readable next to a non-zero neighbour from the same probe. If every counter
in a result reads zero, the probe is the suspect, not the system.

Three of the seven above were caught only because the number contradicted something already
known. Do not rely on that: contradiction is luck, and the other four were published.
