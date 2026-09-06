---
id: EEEBOT-FLEET-001
title: "Fleet operations: agent/model fit and the iteration register"
category: operations
tags: [fleet, agents, models, delegation, verification, lessons]
status: active
created: 2026-09-06
updated: 2026-09-06
environment:
  os: Windows
  shell: PowerShell or Bash
  tools: [herdr, gh, git, ssh]
error_signatures:
  - "Context exceeded"
  - "429: No deployments available for selected model"
  - "Validation failed for tool \"edit\""
---

# Fleet operations: agent/model fit and the iteration register

## Purpose

Two things, kept in one file because they only work together:

1. **What each agent/model combination is actually good at**, derived from observed runs rather than from model marketing.
2. **A register**, appended after every iteration, recording who ran what on which model and how it went — so the next assignment is a lookup instead of a guess.

The register is the evidence; the fit table is its summary. When they disagree, the register wins and the table gets rewritten.

## How to use this

Before dispatching:

- Read the fit table. Match task shape to the row, not the agent's name.
- Check the agent's remaining context window. A wide task given to a two-thirds-full window fails at the two-thirds mark, not at the end.
- Check what else is running. Two agents in the same file is a collision, not parallelism.

After the agent reports:

- **Verify by diff, never by report.** See "Report is not delivery" below.
- Append one row to the register. One line, facts only.

## Fit table

| Agent kind | Model | Good at | Fails at | Evidence |
|---|---|---|---|---|
| Claude Code | Fable 5.1 | Deep root-cause work across components; correctly used subagents for review and tests; split config concerns out into their own issue unprompted | Long sessions accumulate context fast (766 K of 1 M after a day) | Found the #1381 workspace-divergence deadlock and the #1387 escalation substitution, each after other passes had missed them |
| pi | `an/gemini-3.8-flash-high` | Careful test work; conflict resolution; corrected a bad calibration handed to it instead of implementing it | Not yet observed failing | Resolved a whole-file CRLF conflict preserving 11 tests; rejected an operator-supplied threshold table and produced a better-founded one |
| pi | `cl/gpt-5.6-luna` | Narrow single-output steps — one script, one table, one number | Wide sweeps over rotated archives: drowned twice at 563 K and 577 K tokens with zero output | Same agent produced three clean results in a row once each step was cut to one table |
| pi | `an/gemini-3.1-pro-high` | Correct diagnosis and code localisation | Proposed a fix that would have broken five readers; described a flag in its report that was not in its diff | Located the shared-tag defect precisely at three call sites, then proposed splitting a value with five equality-checking consumers |

### Standing constraints, independent of model

- **`cl/gpt-5.6-luna` pool exhausts.** Observed `429: No deployments available for selected model … Try again in 120 seconds` with a non-empty cooldown list. Agents on it stall through no fault of their own. The eeebot loop itself is unaffected — it routes through the `an/` and `un/` namespaces.
- **Two agents in one file collide.** Give every agent its own worktree off `origin/main`, and never the shared checkout — that is where deploys run `git checkout main`.

## Observed failure modes

These are the ones that actually cost time. Each is written as a check, not a warning.

### 1. Measured the wrong object

The dominant failure of the day: seven occurrences, across four agents and the operator.

| What was measured | What was meant | Cost |
|---|---|---|
| `entries[id].model` | `entries[id].escalated.model` | A correct hypothesis was retracted and written into a review as ruled out; the investigation went toward a mechanism that does not exist |
| `usage.prompt_tokens` | top-level `total_tokens` | Nearly published "token accounting is not recorded" |
| Skills catalogue against the release tree | against the instance tree | 8 skills / 2,834 chars instead of 30 / 12,320 |
| `AGENTS.md` in a stale worktree | the live instance copy | Reported 24,081 bytes; the real files are 9,540 and 8,395 |
| Lifetime blocks per demand item | blocks *before* its success | Overstated a threshold's cost threefold |
| Catalogue saving estimated from a path prefix | the live prompt-fit row | Predicted 838 chars of headroom; actual 513 |
| Pane read at the wrong scroll position | the agent's actual report | Concluded an agent had delivered nothing when it was waiting for authorisation |

**Check before believing a number:** name the file, the key, and the tree it came from. If any of the three is implicit, the number is unverified.

### 2. Report is not delivery

Two agents described work that was not in their pushed diff:

- one claimed a `--untracked-files=all` flag; the branch had plain `--porcelain`
- one claimed a Linux-only `0555` test; the commit touched only the runtime module, and the test it described was found unapplied in the worktree as a stray `.patch` file

Neither was dishonest. Both reported what they *intended*. **Read the diff and the CI status; treat the report as a hypothesis about the diff.**

### 3. Pane status is not delivery

`idle` means the process is not busy. It does not mean work landed. Two agents showed `working` while stuck on gateway retries, one showed `done` mid-compaction with nothing produced, and one showed `idle` while blocked on an interactive menu awaiting a keypress.

**Check:** `gh pr list` / `gh pr checks`, then the diff. Pane status only tells you whether to look.

### 4. Green tests on the wrong platform

A test made a file unremovable by holding an open handle — Windows semantics. On Linux, where production and CI run, `unlink` on an open file succeeds, so the test passed locally and failed all three CI Pythons. The real mechanism was a directory the process could not write into.

**Check:** if a test can only pass where the bug cannot occur, it proves nothing. Make it skip rather than invert, and make it skip when it cannot arrange its own premise — a test that runs as root cannot demonstrate a permission failure and must say so instead of returning a verdict.

### 5. Wide tasks drown

Both context overflows came from the same task shape: "walk every day of the rotated ledger and break it down by cause". Both produced nothing after burning more than half a million tokens.

**Check when writing a brief:** can the answer be one script, one run, one table? If not, split it. The same agent that drowned on the wide version delivered three consecutive results once each step produced exactly one table.

### 6. The dispatcher owns the window

`pE` drowned on a build task at 560 K tokens after being handed it at 53% context. It had delivered two clean results the same day on the same model. The failure was in the dispatch, not the agent — this document's own first instruction is to check the remaining window, and it was not followed.

**Check:** a task's context cost is paid from what the agent has left, not from what it started with. An agent that has already delivered today is often the worst choice for the next large task, not the best.

Related: reading a long issue body through `gh issue view --json body` pushes the whole thing into the window at once. Prefer the plain view when the brief already carries the numbers.

### 7. Long tool arguments are truncated silently, on any model

`pA` aborted a task at 12% context — not an overflow. The harness reported
`Arguments truncated to save context window`, the shell command arrived empty, and the call
repeated until the operation was aborted. The agent had itself diagnosed this route
behaviour a day earlier while investigating a different tool.

**Check:** never pass a long script as a tool argument. Write it to a file, then run the
file. Keep arguments short — that addresses the mechanism whichever way the route question
resolves.

Both confirmed occurrences were on `an/gemini-3.8-flash-high`, at 12% and 10.5% context.
The second agent's pane read `cl/gpt-5.6-luna`, but the operator had switched it by hand
**after** the failure, so that reading describes the aftermath. Whether another route behaves
differently is untested.

Symptom to recognise: repeated `Received arguments: {"_truncated": ...}` with `$ ...` and a
0.0s duration, ending in `Operation aborted`.

## Brief template that works

Derived from the briefs that produced clean results:

1. **One measurable deliverable.** One table, one number, one verdict.
2. **What is already established**, with the numbers, so the agent extends rather than re-derives.
3. **The trap specific to this task** — the reader it will break, the key it will mis-read, the day whose data is distorted.
4. **Isolation**: named worktree off `origin/main`; never the shared checkout; who else is in which file.
5. **Hard clauses**: full pytest with the Windows baseline named by test name; green CI or not delivered; commit and push before reporting; read-only against live state; never fabricate an event to exercise a rule; never read `*.env` values — predicate only.
6. **An explicit escape**: "if it grows past this, stop and hand back what you have with a stated boundary." Partial work with an honest edge beats complete work that never arrives.

## Iteration register

Append one row per agent per task, after verifying the diff. Keep it to facts.

| Date | Agent | Model | Task | Outcome | Note |
|---|---|---|---|---|---|
| 2026-09-06 | pD | Fable 5.1 | #1387 executor 404 root cause | delivered | Found the escalation substitution; split the config half into its own issue unprompted |
| 2026-09-06 | pE | `an/gemini-3.8-flash-high` | PR #222 conflict, #1386 thresholds | delivered | Rejected the operator's calibration table and produced a better-founded one |
| 2026-09-06 | pA | `cl/gpt-5.6-luna` | #1211 suppression, wide | **drowned** | 563 K tokens, no output |
| 2026-09-06 | pA | `cl/gpt-5.6-luna` → `an/gemini-3.8-flash-high` | #1211, narrowed to one table per step | delivered ×4 | Widened its own sample from 2 to 45 cases unprompted |
| 2026-09-06 | pF | `cl/gpt-5.6-luna` | #1208 validators, wide | **drowned** | 577 K tokens, stuck on a summarise prompt |
| 2026-09-06 | p9 | `cl/gpt-5.6-luna` | #1188, #996, #1362 verification | delivered | Returned "cannot verify" on #1362 with reasons — the correct answer |
| 2026-09-06 | p9 | `cl/gpt-5.6-luna` | PR #1388 rescue | partial | Fixed the call sites; described a test it had not pushed |
| 2026-09-06 | pB | `an/gemini-3.1-pro-high` | #1384 first pass | **not delivered** | Ran 2 test files, called it a full suite; CI red; reported a flag not in the diff |
| 2026-09-06 | pB | `an/gemini-3.1-pro-high` | #1329 measurement | delivered | Correct diagnosis and code sites; proposed fix would have broken five readers |
| 2026-09-06 | pB | `an/gemini-3.1-pro-high` | #1329 PR #1393 | delivered | Followed the constraint exactly — outcome value untouched, distinction moved into a keyword-only `tag_suffix` |
| 2026-09-06 | pD | Fable 5.1 | #1395 role-default routes | delivered | Chose routes-in-values and argued it in the code; nine ratchet tests guarding shape not values; found `memory_archiver.py`, a live caller the operator's audit missed |
| 2026-09-06 | pF | `an/gemini-3.8-flash-high` | #1208 verification | delivered | Disproved the operator's premise by reading the code: the key contract had already been widened to `failed_count` |
| 2026-09-06 | pA | `an/gemini-3.8-flash-high` | #1344 verification | partial | Stopped at an honest boundary — needs `state/curator/decisions.jsonl` to finish the verdict |
| 2026-09-06 | pE | `an/gemini-3.8-flash-high` | #1394 build | **drowned** | 560 K tokens. Dispatched at 53% context on a build task — dispatcher error, not the agent: the same agent delivered twice earlier the same day |
| 2026-09-07 | pA | `an/gemini-3.8-flash-high` | #1344 continuation | **aborted** | Tool-argument truncation loop at 12% context — a long inline script was silently cut, the call repeated with an empty command until abort. Not context, not the task |
| 2026-09-07 | pE | `an/gemini-3.8-flash-high` | #1394 (not its task — was in pF's worktree) | **aborted** | Same argument-truncation loop at 10.5% context. Its pane read luna, but the operator had switched it by hand after the failure — the reading was mistaken for the agent's own behaviour |
| 2026-09-07 | pE | `cl/gpt-5.6-luna` | #1329 live verification | **not delivered** | Killed by gateway `429 No deployments available` on luna, five retries then connection error. Its two runs before that scanned the right repository but built the tag name with one `cycle-` prefix where the tags carry two, so its 496 "mismatches" were an artifact of its own string. Verification finished by the operator |

## Related

- `docs/EEEBOT_OPERATOR_WORKFLOW.md` — operator-side procedures
- `AGENTS.md` — the router agents read at spawn; procedures live in skills, not here
- Sibling knowledge bases: `localllm-kb`, `ai-dashboards-kb`, `projectmanagment-kb` all carry a `kb/` tree built by `kb-bootstrap`. This repository does not yet — see the note in the accompanying issue.
