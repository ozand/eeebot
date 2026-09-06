---
id: PROJECT-0004
title: "Long tool arguments are truncated silently on the gemini route, and the call loops"
category: tooling
severity: medium
tags: [agents, tooling, models, routing]
status: active
created: 2026-09-07
updated: 2026-09-07
error_signatures:
  - "Arguments truncated to save context window"
  - "Operation aborted"
---

# Long tool arguments are truncated silently on the gemini route, and the call loops

## Symptom

An agent stops producing output and eventually aborts, at a context level nowhere near its
limit. The pane shows a repeating block:

```
$ ...
Received arguments:
{
  "_truncated": "Arguments truncated to save context window."
}
Took 0.0s
```

ending in `Operation aborted`. The shell command is empty — `$ ...` — and each attempt costs
no time, so the loop is fast and produces nothing.

Observed on `an/gemini-3.8-flash-high` at **12% context used**. It is not an overflow, and
distinguishing the two matters: overflow is fixed by narrowing the task, this is not.

## Root Cause

The route truncates large tool-call arguments before dispatch and does not surface the
truncation as an error. The tool then receives an empty or malformed argument, fails
validation, and the agent retries the same oversized call.

The trigger in every observed case was a long inline script passed as a single argument —
the natural shape when a brief asks for "one script, one run, one table". The agent that hit
this had itself diagnosed the same route behaviour a day earlier while investigating a
different tool, which is how it was recognised quickly.

## Resolution

Write the script to a file, then run the file. Keep tool arguments short.

Reassigning the same brief to another agent on a different route completed it without
change, which is the evidence that the task was not at fault.

## Prevention

- Briefs that ask for a single aggregate run should say **write the script to a file first**,
  not just "one run".
- When an agent stalls, read its remaining context before assuming overflow. At 12% used the
  answer is elsewhere.
- Route-specific behaviour belongs in the fit table, not in a per-task workaround: an agent
  on this route is a poor choice for anything that assembles large arguments, regardless of
  how capable it is otherwise.
