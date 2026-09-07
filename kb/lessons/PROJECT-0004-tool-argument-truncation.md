---
id: PROJECT-0004
title: "Long tool arguments are truncated silently and the call loops until abort"
category: tooling
severity: medium
tags: [agents, tooling, harness]
status: active
created: 2026-09-07
updated: 2026-09-07
occurrences: 4
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

**Not route-specific.** Settled on the fourth occurrence, 2026-09-07: the harness printed
`Model: cl/gpt-5.6-luna` in the transcript at the abort itself, not in a status line that a
later manual switch could have changed. Earlier occurrences were on
`an/gemini-3.8-flash-high`. Two different providers, same failure.

The earlier versions of this lesson called it gemini-specific on one occurrence, then called
that disproven on a pane reading that turned out to describe the state *after* an operator's
manual model change. Both asserted more than the evidence carried, in opposite directions.
The evidence that finally settled it was a model name the harness printed for the aborted
run, not one read off a status bar.

What stands: four occurrences across two providers, every one with a large single payload,
every one far from any context limit.

## Resolution

Keep **every** tool argument short. The cap is on the serialized arguments of any single
tool call, so it is not specific to shell commands.

The obvious remedy — "write the script to a file, then run the file" — is not sufficient on
its own, and a third occurrence on 2026-09-07 proved it. The truncation hit the `write` call
itself:

```
write ...
Validation failed for tool "write":
  - path: must have required properties path, content
Received arguments:
{ "_truncated": "Arguments truncated to save context window." }
```

The file content *is* the oversized argument. `path` and `content` both vanish, the tool
rejects the call for missing required properties, and the agent retries the same oversized
write until it aborts. A brief that says "write the script to a file" without saying how big
that write may be has moved the failure, not removed it.

What actually works: build the file in several small appends rather than one large write, or
keep the analysis to short steps whose output feeds the next. Either way the rule is the same
— no single tool call carries a large payload.

## The mitigation that actually works

Telling the agent to keep its payloads small does not reliably prevent this — the fourth
occurrence happened to an agent whose brief said exactly that, in those words.

What works is removing the payload from the agent entirely: **the dispatcher writes the
script, stages it (locally and on the host), and hands the agent a path to run.** The agent
then spends its calls on `ssh ... python3 /tmp/<script>.py` and on interpreting the output,
neither of which is large. The #996 verification was finished this way after two aborts on
the same task.

This also puts the analysis code under review before it runs against live state, which is
worth having for its own sake.

## Prevention

- Briefs that ask for a single aggregate run must bound the *write* as well as the run.
  "Write the script to a file first" is not enough on its own — the write is a tool call with
  the same cap. Say: build it in small appends, no single call carrying the whole script.
  The phrasing "one script, one run, one table" invites the failure.
- When an agent stalls, read its remaining context before assuming overflow. At 10–12% used
  the cause is elsewhere.
- A pane shows current state, not history. Before drawing a conclusion from an agent's model,
  ask whether anyone changed it — an operator's manual switch leaves no trace in the pane and
  is easily read as the agent's own behaviour.
- Prefer the fix that works under both competing explanations over the one that requires
  picking between them. Here, shortening the argument does; changing model only helps if the
  route hypothesis is true, and it is not established.
