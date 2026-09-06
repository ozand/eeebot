---
id: PROJECT-0003
title: "An agent's report describes what it intended, not what it pushed"
category: process
severity: medium
tags: [delegation, verification, agents, context]
status: active
created: 2026-09-06
updated: 2026-09-06
error_signatures:
  - "Context exceeded"
  - "assert True is not True"
  - "No deployments available for selected model"
---

# An agent's report describes what it intended, not what it pushed

## Symptom

An agent reports a change that is not in its branch, and the report is detailed enough to be
believed. Two occurrences in one day: one claimed a `--untracked-files=all` flag where the
diff had plain `--porcelain`; one claimed a permission test whose commit touched only the
runtime module — the described test was found unapplied in the worktree as a stray `.patch`
file, written against a design the agent had since abandoned.

Adjacent shapes seen the same day: `idle` panes that had delivered nothing, `working` panes
stalled on gateway retries, `done` panes mid-compaction, and a pane blocked on an
interactive menu awaiting a keypress.

## Root Cause

Agents report intent. Nothing in the loop from intent to report forces a read-back of the
pushed state, so an abandoned draft, an unapplied patch or an uncommitted edit is described
in the past tense with full confidence.

Two further mechanisms produce silent non-delivery:

- **Context overflow.** Three agents drowned at 560–577 K tokens with zero output. Two were
  on "walk every day of the rotated archive and break it down by cause"; the third was a
  build task handed to an agent already at 53% context — a dispatcher error, since the same
  agent had delivered twice that day.
- **Pool exhaustion.** `429: No deployments available for selected model … Try again in 120
  seconds`. The agent stalls through no fault of its own; the fix is to move it to another
  model, not to retry.

Note the error runs both ways: the same day, one agent corrected the operator's threshold
calibration with a better-founded measurement, and another disproved an operator premise by
reading the code. This is an argument for verifying the artifact, not for distrusting
agents.

## Resolution

Verify by diff and CI status before accepting any report, in both directions. A green CI on
the branch is the delivery; the report is a hypothesis about it.

For a stalled agent, read the pane content rather than its status — and read enough of it,
since a pane read at the wrong scroll offset looks identical to an empty one.

## Prevention

- Check the agent's *remaining* context window before dispatching, not its past
  performance. An agent that already delivered today is often the worst choice for the next
  large task. The dispatcher owns this.
- One measurable deliverable per brief — one script, one run, one table — plus an explicit
  escape: "if this grows past here, stop and hand back what you have with a stated
  boundary." Partial work with an honest edge beats complete work that never arrives.
- Avoid `gh issue view --json body` for long issues; it pushes the whole body into the
  window at once.
- A test that cannot arrange its own premise must skip, not return a verdict. A test that
  passes only where the bug cannot occur — Windows file-locking semantics standing in for a
  POSIX permission failure — proves nothing about the bug.
