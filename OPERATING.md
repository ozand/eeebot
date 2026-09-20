# Operating rules

How a cycle runs. Not identity, not values, not the charter — those live in `IDENTITY.md`, `SOUL.md`, and `goals.md`. This file is release-owned; the loop reads it and never commits to it.

## Cycle contract

Work only on the cycle branch the harness already created off `origin/main`. Implement and commit there. Never run `git checkout`, `git switch`, or `git branch`, and never run `git push` — the bridge integrates your commit(s) into `main` after the gate passes. A stray push cannot reach `main` from this branch, but it still wastes a turn. A cycle that does not integrate is a normal outcome, not an error to work around. Work you do not commit is discarded when the turn ends.

## Mutation surface

<!-- rendered-from: mutation_policy -->
Allowed targets: surfaces/, scripts/, memory/, lessons/, docs/, tests/, skills/, diary/, AGENTS.md
Creating or improving skills for repeated patterns is valuable work.
Do NOT modify: state/, ops/, goals.md, IDENTITY.md, SOUL.md, USER.md, OPERATING.md, secrets, or systemd units.

## Before editing: skip check

Before any edit, check the Recent activity list and the codebase for whether this task is already done or not applicable. If it is, stop immediately: report `outcome: "skipped"`, make no bookkeeping commit, and spend no further tool calls. A skip is a valid, complete outcome, not a failure to work around.

Then check the skills catalogue: if a skill describes this work, `read_file` its `SKILL.md` before starting, not after a failed attempt.

## Execution

Name the kind of work before starting; each ends differently.

- **Connect** — an artifact exists, is tested, and nothing runs it. Give it an invoker: a caller, a unit, a skill, a documented command. The cheapest way to finish something.
- **Extend** — improve what other work already depends on, so the improvement propagates.
- **Reach the screen** — move the day's output one stage on (`goals.md` Vector 3).
- **Repair** — reproduce or diagnose, then make the fixing edit in the same session; never end a cycle on diagnosis alone.
- **Retire** — remove what no longer earns its keep, with evidence nothing depends on it.
- **Create** — a new artifact only when you can name what will depend on it; name that in `findings`.

Scoped does not mean trivial: when the task deserves it, make the ambitious change rather than the smallest safe edit. Only when the full change will not fit the remaining budget, commit the smallest verifiable step — a failing test, or a scoped partial fix — so the next cycle starts from an artifact and not a re-diagnosis. Never a bookkeeping-only commit.

## Verification

The test runner is **pytest**. Verify with `exec("python3 -m pytest <affected test file>")` — run a scoped test before a full suite. A verification failure outside your target path is out of scope: do not fix it. Note the failing path and the exact error, complete and commit your in-scope change, and hand the defect off through `concrete_next_action` instead. pytest also runs stdlib `TestCase` suites — an existing one needs no rewrite.

## Termination

Once verification passes and the commit is made, the cycle is over: emit the final response immediately. Do not issue trailing placeholder calls (`echo done`, a repeat `git status`, a no-op read) after the commit — they burn iterations and can truncate the final response. The commit plus the final JSON response is the complete, terminal action of the cycle.

## Handoff

`concrete_next_action` must name what the next subagent should do, precisely enough to act on without re-diagnosing:

- **Out-of-scope defect:** the failing path, the exact error, and why it is pre-existing (fails on `origin/main` too, or lives outside your target).
- **Completed or skipped task:** the next concrete step. A vague "fix the tests" is not a handoff.

## Iteration budget

Your budget of tool iterations this cycle is on the line "Iteration budget this cycle" in the task section. Read and edit early, verify once you have a candidate fix, and keep enough in reserve for the commit and the final response — do not circle before committing.

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

- **`read_file`** — read before you edit; also how you read a skill (below) or a file named in a handoff.
- **`list_dir`** — find a path you do not know, instead of guessing at `read_file`.
- **`edit_file`** — change an existing file. Prefer it to `write_file`, which drops what you did not rewrite.
- **`write_file`** — create a new file, or replace one you have read in full.
- **`exec`** — run commands, pytest included (see Verification). 60-second default timeout, 10,000-character output cap; bound a long or noisy command (`head`, `tail`, `wc -l`) before running it, not after it truncates.
- **`search_memory`** — the memory block here is an index, not the store behind it. When a task touches something done before and the index line is not enough, retrieve the fact instead of re-deriving it.
- **Skills** — the catalogue lists one line each. When a task matches a skill's description, `read_file` its `SKILL.md` before starting the work, not after a failed attempt.
- **Read-only for every tool** — everything under Mutation surface's "Do NOT modify". Reading them for context is fine; writing, staging or committing to them is not.
