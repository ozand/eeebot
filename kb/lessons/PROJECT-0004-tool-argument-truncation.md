---
id: PROJECT-0004
title: "Long tool arguments are truncated silently and the call loops until abort"
category: tooling
severity: medium
tags: [agents, tooling, harness]
status: active
created: 2026-09-07
updated: 2026-09-07
error_signatures:
  - "Arguments truncated to save context window"
  - "Operation aborted"
---

# Long tool arguments are truncated silently and the call loops until abort

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

Observed twice within an hour, at 12% and 10.5% context used. Neither is an overflow, and
distinguishing the two matters: overflow is fixed by narrowing the task, this is not.

## Root Cause

The harness truncates large tool-call arguments before dispatch and surfaces the truncation
only as `_truncated`, not as an error. The tool then receives an empty or malformed
argument, fails, and the agent retries the same oversized call.

The trigger in every observed case was a long inline script passed as a single argument —
the natural shape when a brief asks for "one script, one run, one table".

## What is and is not established about the model

Both confirmed occurrences were on `an/gemini-3.8-flash-high`. The second agent's pane read
`cl/gpt-5.6-luna` when inspected, but the operator had switched that agent from flash to
luna **by hand after the failure**, so the reading postdates the event and says nothing
about which route the truncation happened on.

That correction matters twice over. The first version of this lesson called the truncation
gemini-specific on one occurrence. The second version called that disproven, on a reading
that turned out to describe the state *after* a manual intervention — the operator's action
was invisible in the pane and was assumed to be the agent's own. Both versions asserted more
than the evidence carried, in opposite directions.

What stands: two occurrences, both on flash, both with a long inline script, both far from
any context limit. Whether another route behaves differently is **unknown and untested**.

## Resolution

Write the script to a file, then run the file. Keep tool arguments short. This addresses the
mechanism regardless of how the route question resolves, which is why it is the recommended
action and switching model is not.

## Prevention

- Briefs that ask for a single aggregate run must say **write the script to a file first**,
  not merely "one run". The phrasing "one script, one run, one table" invites the failure.
- When an agent stalls, read its remaining context before assuming overflow. At 10–12% used
  the cause is elsewhere.
- A pane shows current state, not history. Before drawing a conclusion from an agent's model,
  ask whether anyone changed it — an operator's manual switch leaves no trace in the pane and
  is easily read as the agent's own behaviour.
- Prefer the fix that works under both competing explanations over the one that requires
  picking between them. Here, shortening the argument does; changing model only helps if the
  route hypothesis is true, and it is not established.
