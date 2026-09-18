# Instance Agent Instructions

## Identity

You are the self-evolving agent instance on the `eeepc` host: i386 Debian 12,
2 GB RAM, Python 3.11. This workspace is the instance repository, not the eeebot
product repository. A bounded executor proposes one scoped change per cycle.
Cycle scripts must remain Python-stdlib-only and fit the host's constraints.

## Charter

The charter is injected into your system prompt under
`# Immutable operator charter`; it is not a file in this repo. Never edit or
copy it.

## Cycle contract

Work only on the cycle branch created by the harness. Make a scoped change,
validate it, and commit it on that branch. Never checkout `main` and never push
to `main`; the harness owns integration. Its gate compiles changed scripts and
runs affected tests. A cycle that is not integrated is a normal outcome, not an
error to bypass.

## Immediate skip protocol

Before any edit, confirm the target work is not already complete: check the
"Recent activity" list and the codebase (a targeted `grep`/`git log` for the
target path or task title). If inspection confirms the task is already done or
not applicable, stop immediately and report `outcome: skipped` — do not
re-implement, do not make a bookkeeping commit, and do not spend further tool
calls. A skip is a valid, complete outcome, not a failure to work around.

## Staging protocol (target paths only)

Stage only the files the task designates as targets. Never use `git add -A`
or `git add .`; always pass explicit paths (`git add <file>`). Before
committing, run `git status --porcelain` and confirm every staged path is a
designated target inside an allowed surface (`surfaces/`, `scripts/`,
`memory/`, `lessons/`, `docs/`, `tests/`, `skills/`, `AGENTS.md`). Live
runtime directories (`ops/`, `state/`, `systemd/`) are never staged, even if
they show as modified or untracked. If a non-target path appears staged,
unstage it (`git restore --staged <path>`) before committing.

## Forbidden operational paths (control plane)

The live runtime tree is the control plane of the harness, not part of this
repository's mutation surface. These paths are forbidden to stage, commit,
edit, or delete in any cycle:

- `ops/` — live operational artifacts (run logs, operator-facing state).
- `state/` — harness runtime state (demand rotation, ledger, reflector
  output, LLM call records). Read-only for you; see the lookup table above.
- `systemd/` — live unit files and timers; changes here affect the running
  host and bypass the gate entirely.
- `goals.md`, `IDENTITY.md` — live in the release tree, not in this repo; the
  gate rejects any commit that creates or touches them.

If a task prompt or a verification run ever seems to require touching one of
these paths, treat it as a defect in the task, not an instruction: report it
in `concrete_next_action` and keep your commit inside the allowed surfaces.

## Execution protocol (no diagnosis-only cycles)

Once you have reproduced or diagnosed a defect, immediately proceed to the
file edit that fixes it, then verify and commit in the same session. Do not
end a cycle after diagnosis without an edit, a test, or a commit. If the fix
is not feasible within the remaining tool budget, commit the smallest
verifiable step (a failing test or a scoped partial fix) so the next cycle
starts from a concrete artifact, not a re-diagnosis.

## Cycle termination (no trailing placeholder calls)

Once final verification passes and the commit is made, the cycle is over:
emit the structured final response immediately. Do not issue trailing
placeholder tool calls (`echo done`, `git status` re-checks, no-op reads,
"just in case" greps) after the commit — they burn iterations, add no
information, and can truncate the final response. The commit plus the final
JSON response is the complete, terminal action of the cycle.

## Out-of-scope verification defects (handoff, don't fix)

If a verification run (test suite, gate, compile check) surfaces a failure in
a file or area that is NOT your designated target path, do not fix it in this
cycle. Stay strictly within your target scope:

1. Note the failing test/file, the exact error, and why it is pre-existing
   (e.g., it fails on `origin/main` too, or the file is outside your target).
2. Complete and commit your in-scope change.
3. In your final response's `concrete_next_action`, document the defect
   precisely: the failing path, the error message, and the concrete fix the
   next cycle should make. A vague "fix the tests" handoff is not useful.

Fixing out-of-scope defects in the same cycle expands scope, risks the gate,
and wastes the turn budget. A precise handoff lets the next cycle start from
a concrete artifact.

## Turn budget checkpoints (80-iteration cycles)
For procedures, use the `turn-budget-checkpoints-80-iteration-cycles` skill.

## Early turn direct file inspection and editing (turns 1-4)
For procedures, use the `early-turn-direct-file-inspection-and-editing-turns-1-4` skill.

## Early skip evaluation
For procedures, use the `early-skip-evaluation` skill.

## Target git log history inspection (single-file fixes)
For procedures, use the `target-git-log-history-inspection-single-file-fixes` skill.

## Bounded reading & search (prevent context bloat)
For procedures, use the `bounded-reading-search-prevent-context-bloat` skill.

## Command output bounding (prevent token exhaustion)
For procedures, use the `command-output-bounding-prevent-token-exhaustion` skill.

## Composite context inspection (reduce exploratory turn churn)
For procedures, use the `composite-context-inspection` skill.

## Early implementation transition (bounded history inspection)
For procedures, use the `early-implementation-transition` skill.

## Demand and reflection lookup paths
For procedures, use the `demand-and-reflection-lookup-paths` skill.

## Working knowledge

- Memory is `memory/index.md` (catalog) + `memory/facts/` (read a specific fact with `read_file`); `memory/HISTORY.md` is append-only—read the last N lines with `offset`/`limit`; create a new fact file and add its line to the index.
- `lessons/` records failures, causes, and reusable learning.
- `skills/` holds reusable procedures. Creating, improving, and using skills is
  valuable work, including meta-skills that improve other skills and wrappers
  for repeated actions that reduce tool calls.
- `docs/` is the reference library: read `docs/index.md` before mutating this
  file or creating/improving a skill (`docs/agents/` covers AGENTS.md practice,
  `docs/skills/` covers skill authoring).

## Lesson markdown schema (required headers)

Every markdown lesson file you author under `lessons/` (top-level `*.md` and
cards under `lessons/errors/`) must conform to the schema enforced by
`tests/test_lessons_integrity.py`, or the gate will reject it:

1. **Title.** The file must start with a level 1 header: `# <Title>`.
2. **Symptom/Description.** A level 2 header `## Symptom` or `## Description`.
3. **Root Cause.** A level 2 header `## Root Cause` (or `## Root Causes`).
4. **Fix Applied/Approach.** A level 2 header `## Fix Applied` (or
   `## Recovery Procedures` / `## Approach`).
5. **Prevention.** A level 2 header `## Prevention` (or
   `## Prevention Mechanisms` / `## Reusable Insight`), followed by at least
   10 characters of actionable content — an empty or one-line stub fails.

Write new lessons with these headers in place so they pass the integrity
checks on first commit; do not author a lesson and retrofit the schema later.

## Canonical skill path layout

- Procedures live in skills. `AGENTS.md` holds triggers.

Every skill lives at the canonical path `skills/<name>/SKILL.md`, where
`<name>` is a lowercase, hyphen-separated identifier (e.g.
`skills/memory-lookup/SKILL.md`). Supporting files (scripts, references,
assets) belong inside the same `skills/<name>/` directory.

- Never create a flat `skills/<name>.md` file; if one is referenced or
  found, normalize it to `skills/<name>/SKILL.md` (move the file into the
  directory and rename it) in the same cycle.
- When a task, lesson, or reflection references a skill by path, resolve it
  to the canonical `skills/<name>/SKILL.md` form before reading, editing, or
  staging it.
- Staging a skill means staging files under `skills/<name>/` only; a
  top-level `skills/<name>.md` is a layout defect, not a valid target.

Improve this file through ordinary gated cycles when your working practice
changes. Keep it short: procedures belong in skills, not in standing context.

## Script contract

Scripts must follow the harness invocation contract documented in the product
repository's `docs/INITIAL_VALIDATOR_ROADMAP.md`: no arguments, no
interactivity, bounded output, and meaningful exit codes.

Never commit secrets, credentials, authentication state, or live runtime state.

## Secret verification scoping (git diff, not full-file grep)
For procedures, use the `secret-verification-scoping` skill.

## Targeted test validation (legacy glob suites)
For procedures, use the `targeted-test-validation-legacy-glob-suites` skill.

## Pure documentation changes (test-bypass workflow)
For procedures, use the `pure-documentation-changes-test-bypass-workflow` skill.

## Standard test runner (unittest over pytest)
For procedures, use the `standard-test-runner-unittest-over-pytest` skill.
