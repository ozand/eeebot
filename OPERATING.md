# Operating rules

How a cycle runs. Not identity, not values, not the charter — those live in `IDENTITY.md`, `SOUL.md`, and `goals.md`. This file is release-owned; the loop reads it and never commits to it.

## Cycle contract

Work only on the cycle branch the harness already created off `origin/main`. Implement and commit there. Never run `git checkout`, `git switch`, or `git branch`, and never run `git push` — the bridge integrates your commit(s) into `main` after the gate passes. A stray push cannot reach `main` from this branch, but it still wastes a turn. A cycle that does not integrate is a normal outcome, not an error to work around. Work you do not commit is discarded when the turn ends.

## Mutation surface

<!-- rendered-from: mutation_policy -->
Allowed targets: surfaces/, scripts/, memory/, lessons/, docs/, tests/, skills/, AGENTS.md
Creating or improving skills for repeated patterns is valuable work.
Do NOT modify: state/, ops/, goals.md, IDENTITY.md, SOUL.md, USER.md, OPERATING.md, secrets, or systemd units.

## Before editing: skip check

Before any edit, check the Recent activity list and the codebase for whether this task is already done or not applicable. If it is, stop immediately: report `outcome: "skipped"`, make no bookkeeping commit, and spend no further tool calls. A skip is a valid, complete outcome, not a failure to work around.

## Execution

Once you have reproduced or diagnosed the defect, proceed to the edit that fixes it in the same session — do not end a cycle after diagnosis alone. If a full fix does not fit the remaining iteration budget, commit the smallest verifiable step (a failing test, or a scoped partial fix) so the next cycle starts from a concrete artifact, not a re-diagnosis. Do not create bookkeeping-only commits.

## Verification

The test runner is **pytest**. Verify with `exec("python3 -m pytest <affected test file>")` — run a scoped test before a full suite. A verification failure outside your target path is out of scope: do not fix it. Note the failing path and the exact error, complete and commit your in-scope change, and hand the defect off through `concrete_next_action` instead.

## Termination

Once verification passes and the commit is made, the cycle is over: emit the final response immediately. Do not issue trailing placeholder calls (`echo done`, a repeat `git status`, a no-op read) after the commit — they burn iterations and can truncate the final response. The commit plus the final JSON response is the complete, terminal action of the cycle.

## Handoff

`concrete_next_action` must name what the next subagent should do, precisely enough to act on without re-diagnosing:

- **Out-of-scope defect:** the failing path, the exact error, and why it is pre-existing (fails on `origin/main` too, or lives outside your target).
- **Completed or skipped task:** the next concrete step. A vague "fix the tests" is not a handoff.

## Iteration budget

The number of tool iterations available this cycle is stated in the task section of this prompt, on the line "Iteration budget this cycle". Pace deliberately against it: read and edit early, verify once you have a candidate fix, and keep enough budget in reserve for the commit and the final response — do not spend the whole budget circling before committing.

## Final response

Your final response MUST be this JSON, and nothing else (no markdown wrapping):

```
{
  "action_taken": "<one sentence: what you actually did>",
  "files_changed": ["<path1>", "<path2>"],
  "outcome": "completed" | "skipped" | "blocked",
  "concrete_next_action": "<what the next subagent should do>",
  "findings": ["<observation1>", "<observation2>"]
}
```

## Tools

- **pytest** is the test runner (see Verification); it is installed in this environment.
- **exec** has a 60-second default timeout and a 10,000-character output cap; long-running or high-output commands must be scoped or piped through a bound (`head`, `tail`, `wc -l`) before you run them, not after they truncate.
- **search_memory** reports `complete`, `partial`, or `unavailable` — never a bare empty list standing in for "nothing found."
- **Read-only for every tool:** everything named under Mutation surface's "Do NOT modify" list above. Reading those paths to understand context is fine; writing, staging, or committing to them is not.
