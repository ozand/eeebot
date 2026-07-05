# 643 — design: minimal Python tool-harness for bounded subagents

## Tool set (minimal)

The subagent's actual job (per `docs/specs/subagent-bridge/spec.md` and the
autonomous subagent operating directive in
`docs/specs/self-evolving-runtime/spec.md`) is: implement or verify one
bounded improvement, produce a diff, and report evidence. That bounds the
tool set tightly — this is not a general coding-agent tool belt.

| Tool | Phase | Justification |
|---|---|---|
| `read` | 1 | Needed to inspect the file(s) named in the source artifact before editing or reporting findings. Lowest risk: no mutation. |
| `grep` | 1 | Needed to locate the change site when the source artifact names a symbol/behavior rather than a line number. Read-only. |
| `ls` | 1 | Needed to confirm workspace layout before editing (avoid guessing paths). Read-only. |
| `edit` | 2 | The actual implementation step — string-replace against a known-good original, not a truncated diff format the model has to invent. |
| `write` | 2 | Needed only for new files (rare — most bounded tasks touch existing modules). Gated with `edit` since both mutate the confined workspace under the same policy seam. |
| `bash` (command-runner) | 3 | Needed to run the import-smoke check or a narrow test the subagent itself, rather than the bridge, drives — but command execution is the hardest surface to bound (arbitrary subprocess) and stays last. |

Phase 1 alone is already useful today: it lets the bounded "verify" role
(`profile` ∈ `research_only`/`review_only`/`bounded_review`, see
`nanobot/runtime/subagent_materializer.py::_run_local_executor`) read the
actual changed files instead of reasoning only from the ~4000-char inlined
`source_artifact` (`docs/specs/subagent-bridge/spec.md` R6), without any
autonomy-surface expansion (no mutation, no execution).

## Contracts (ported from pi, adapted to Python)

### Edit tool

Request shape: `{path, edits: [{oldText, newText}]}` (pi ref:
`packages/coding-agent/src/core/tools/edit.ts`). Each `edits[i].oldText` must
be an exact, unique, non-overlapping substring of the *current* file content
at apply time — validated against the file as read, not a cached copy, so a
concurrent modification is caught rather than silently overwritten. Multiple
edits in one call are validated as a batch against the original text (not
progressively against intermediate states) so overlapping ranges are rejected
up front, matching pi's `edit-diff.ts` validation order.

Validation failures (oldText not found, found more than once, edits overlap)
are **not exceptions** — they come back to the model as normal tool-result
text (e.g. `"oldText appears 3 times in file; must be unique"`), so the model
gets a turn to retry with a larger, disambiguating context window. This is
the same shape pi uses in `agent-loop.ts` (see below) and is deliberately
loop-friendly: a bounded subagent with a small context budget cannot afford a
crashed turn.

A unified diff is produced as an audit byproduct on successful apply — not
returned to the model as the primary result (the model only needs
success/failure + the new relevant excerpt), but written alongside the tool
call's journal entry (see Safety model) so a human reviewing a promoted
change can read a real patch, not just "edit applied."

### Pluggable Operations interface

Each tool's logic (validate edits, run a command, list a directory) is
written against a small `Operations` interface (pi ref: `BashOperations.exec`,
`EditOperations.readFile/writeFile` in `tools/bash.ts` / `tools/edit.ts`), not
directly against `open()`/`subprocess.run()`. The concrete implementation used
in eeebot resolves and enforces the workspace boundary (see Safety model)
before ever touching the filesystem or spawning a process. This keeps "what a
tool does" separate from "what a tool is allowed to touch" — the same
decoupling pi uses to run tools against local/SSH backends, repurposed here
so the *only* backend eeebot ships is the confined-workspace one.

### Shared truncation module

One deterministic truncation function, applied uniformly by `read`, `grep`,
and `bash` output — 2000 lines / 50KB head-tail with `truncated` and
`totalLines` (or `total_bytes`) metadata told to the model explicitly (pi
ref: `tools/truncate.ts`). This is not a nice-to-have here: the eeepc host is
resource-constrained and the executor model
(`un/qwen3.6-27b-mtp`) has a bounded context; an unbounded `read` or `bash`
result is a real failure mode (context blowout, OOM-adjacent behavior on a
weak host), not just an inconvenience.

### Errors as tool-result content

No tool call raises an exception that reaches the turn loop. Every failure —
bad path, validation failure, timeout, veto (below) — becomes a tool result
the model sees as a normal turn, exactly as `agent-loop.ts`'s
`prepareToolCall`/`executePreparedToolCall` split does (prepare validates and
can already produce an error result; execute only runs if prepare succeeded).
This matters more for eeebot than for pi: pi's loop is human-supervised and
can afford a rare TUI-visible crash; ours runs unattended, and an unhandled
exception in a bounded subagent turn should never propagate up into the
bridge process (`nanobot/runtime/bridge.py`) or the coordinator.

### Single veto hook as the only policy seam

Exactly one `before_tool_call(call) -> allow | veto(reason)` hook sits
between "model asked for a tool call" and "tool call executes" (pi ref: the
same `agent-loop.ts` prepare/execute split). Tools themselves stay
policy-free — they do not know about stop-guards, promotion gating, or
allowlists. All of eeebot's policy plugs in at this one seam:

- stop-guard budget checks (max tool calls/iterations for the run, R2/R13);
- path-confinement checks (is the resolved path inside the workspace root);
- command allowlist/deny-by-default checks (phase 3 only);
- a veto is itself a tool-result, not a crash — the model sees "this call was
  not permitted: <reason>" and can adjust, same as a validation failure.

## What we deliberately do NOT port

- **pi's "no sandbox by design" posture.** pi assumes an attended CLI session
  where a human can Ctrl-C a runaway command; eeebot runs unattended on a
  host with no one watching. Every tool in our port assumes the opposite:
  confinement is mandatory, not optional configuration.
- **No loop-level turn/token budgets.** pi's agent loop runs until the model
  stops on its own. Our loop enforces `max_iterations` and a token budget
  *inside* the loop as a hard stop, independent of what the model wants — R2
  and R11-R13 in `docs/specs/self-evolving-runtime/spec.md` remain the
  authoritative budget/stop-reason contract; the harness adds tool calls to
  what a stop-guard counts, it does not add a second budget system.
- **The ~3,175-LOC `agent-session.ts` framework layer** (session persistence,
  resumption, multi-turn conversation management beyond one bounded run) —
  each bounded subagent run is already a single isolated invocation
  (`docs/specs/subagent-bridge/spec.md` cycle-branch isolation), so there is
  no cross-run session state to manage.
- **The Node/TUI extension system and per-tool rendering** — there is no
  interactive terminal on the other end; tool results are consumed by the
  next model turn and by the result-artifact writer, not displayed live.

## Safety model (eeebot-specific)

This is the part with no pi precedent — pi does not need it because it is
supervised.

- **Workspace confinement.** Every tool that takes a path resolves it
  (`Path.resolve()`) and verifies the resolved path is a descendant of the
  subagent's workspace root (the cycle's isolated worktree/branch checkout,
  `docs/specs/subagent-bridge/spec.md` R8) before any I/O. A path that
  escapes the root (symlink, `..`, absolute path outside root) is a veto, not
  a warning.
- **Command execution policy (phase 3).** Deny-by-default: a command is only
  runnable if its argv head matches a small allowlist (the same shape
  `NANOBOT_SUBAGENT_EXECUTOR_COMMAND` already uses for the executor argv
  itself, `nanobot/runtime/subagent_materializer.py::_executor_argv`).
  Every invocation has a mandatory timeout with a conservative default (no
  unbounded run, unlike pi which has no default timeout) — mirroring the
  existing `executor_timeout_seconds` parameter on
  `materialize_subagent_requests`.
- **Journaling.** Every tool call (request, veto/allow decision, result,
  truncation metadata) is appended to the same per-cycle evidence trail the
  bridge already writes to `state/subagents/results/` — extending, not
  replacing, the `bridge_llm_execution` result shape
  (`docs/specs/subagent-bridge/spec.md` R16) with a `tool_calls` list.
- **Stop-guards as the loop budget authority.** The harness loop does not
  invent its own budget model; it decrements the same `max_tool_calls`/
  `max_iterations` counters the coordinator's stop-guard tracking uses (R2,
  R11-R13), so a harness run and a plain LLM-only run are comparable under
  one accounting scheme.
- **Promotion gating.** Harness-produced edits land only inside the existing
  cycle-isolation branch (`selfevo/cycle-<id>`,
  `docs/specs/subagent-bridge/spec.md` R8-R9) and are promoted to `main` only
  through the existing smoke gate and integration path (R10-R15) — the
  harness never merges or pushes on its own. A subagent using the harness is
  not a new promotion authority; it is a richer producer of the same diff the
  bridge already gates.

## Loop shape

A small turn loop, not a session framework:

```
loop:
    if stop_guard.should_stop(): break with recorded stop_reason
    response = litellm_client.call(model, messages, tools=tool_schemas)
    if response has no tool calls: break with stop_reason="gate_clean"
    for call in response.tool_calls:
        result = before_tool_call(call)          # veto hook
        if not result.allowed:
            tool_result = error_result(result.reason)
        else:
            tool_result = execute(call)          # never raises
        messages.append(tool_result)
    stop_guard.record_iteration()
```

`max_iterations` and a token budget are enforced *in* this loop (stricter
than pi, which has neither as a hard stop) — consistent with R13's
requirement that termination never rely on budget exhaustion alone: the loop
records one of the enumerated stop reasons (`gate_clean`, `max_iterations`,
`no_progress`, `budget_<name>`) into the result artifact exactly like the
bridge already does for cycle-level termination.

## Integration point

The harness is a new **executor profile**, not a replacement of any existing
path. Today `nanobot/runtime/subagent_materializer.py::materialize_subagent_requests`
picks one of: no configured executor (blocked stub), a configured
`NANOBOT_SUBAGENT_EXECUTOR_COMMAND` (arbitrary external argv,
`_run_local_executor`), or the bridge's own direct LiteLLM call
(`nanobot/runtime/bridge.py::build_task` + `main`). The harness would be a
new in-process option selectable the same way the bridge model is configured
(`config.tools.subagent`, `docs/specs/subagent-bridge/spec.md` R4) — e.g. a
`tool_harness` profile value — so:

- the no-tools direct LiteLLM call remains the default, unaffected;
- opting a request into the harness is explicit per-request (`profile` field
  on the request artifact), not a global runtime-wide switch;
- the bridge's branch isolation, smoke gate, and integration steps
  (`_setup_cycle_branch`, `_run_smoke_tests`, `_integrate_cycle_to_main` in
  `nanobot/runtime/bridge.py`) are unchanged — the harness only changes what
  happens *inside* one subagent invocation, between branch setup and commit.

Config surface stays minimal: model/timeout/max_iterations reuse existing
`SubagentToolConfig` fields where possible; only genuinely new knobs (tool
allowlist for phase 3, workspace-root override for tests) get new fields.

## Phasing

### Phase 1 — read-only tools (`read`, `grep`, `ls`)
Lowest risk: no mutation, no command execution, path-confinement is the only
new policy surface. Immediately useful for richer verification in the
`research_only`/`review_only`/`bounded_review` profiles.

Acceptance: tool calls are confined to the workspace root; every call is
journaled; truncation module bounds all output; a veto (path escape) never
crashes the loop; stop-guard counters include harness tool calls.

### Phase 2 — edit + write in confined workspace
Adds mutation. Requires the edit contract's uniqueness/overlap validation to
be verified against adversarial inputs (empty oldText, non-existent path,
concurrent external modification) before sign-off.

Acceptance: all phase 1 criteria; edits land only on the cycle's isolation
branch; a rejected edit call returns a validation-failure tool result (never
an exception); an audit unified-diff is produced for every successful edit
and journaled; the bridge's existing smoke gate and integration path require
no changes to accept harness-produced commits.

### Phase 3 — command execution (bash/command-runner)
Hardest to bound — explicit sign-off required (per the autonomy-surface
precondition in the (superseded) idea-backlog entry and #641's non-goals).

Acceptance: all phase 1-2 criteria; command execution deny-by-default against
an explicit allowlist; every invocation has a mandatory timeout with a
conservative default; command stdout/stderr pass through the shared
truncation module; a denied command returns a veto tool result, never a
crash or silent skip.

## Open questions (context — see resolutions below)

- Should the harness share one LiteLLM client instance with the bridge's
  direct-call path, or run in a fully separate process the bridge shells out
  to (closer isolation, but reintroduces a subprocess boundary the #641
  removal just simplified away)?
- Where exactly does the tool-call journal live — a new
  `state/subagents/tool_calls/<request_id>.jsonl` file, or inlined into the
  existing result JSON under `executor_result`? Inlining is simpler but risks
  growing the result artifact unboundedly on a long tool-call run.
- Does phase 3's command allowlist need to be per-request (declared in the
  source artifact) or a fixed runtime-wide list? Per-request is more bounded
  but adds a new field to the request schema.
- Should `write` (new files) really wait for phase 2, or is it low-risk
  enough (still confined, still no execution) to ship alongside phase 1's
  read-only tools? Left as phase 2 here per the prompt's grouping, but worth
  revisiting once phase 1 ships and we see real usage.
- Token-budget accounting: does the harness loop need its own token counter,
  or can it reuse whatever the coordinator already tracks for R2's
  `max_tool_calls`/budget caps? Assumed reusable above; needs confirmation
  against the actual budget-tracking code once implementation starts.

## Resolved questions (2026-07-05)

Decided by the operator in the #643 issue thread immediately before phase 1
implementation started; binding for phase 1 and the default assumption for
phases 2-3 unless a later task overturns them.

1. **LLM client**: in-process, reusing the bridge's existing LiteLLM call
   path — concretely, `nanobot.providers.base.LLMProvider.chat_with_retry`
   via the same `_make_provider(config)` bridge.py already uses. A separate
   subprocess would reintroduce the boundary #641 just removed. Implemented
   as `nanobot.runtime.tool_harness.run_tool_harness_request`, which builds a
   provider the same way `bridge.py::main` does (`config.tools.subagent.model`
   / `SUBAGENT_BRIDGE_MODEL`, `_make_provider`) unless a provider is injected
   (tests inject a fake `LLMProvider`).
2. **Tool-call journal**: sidecar `state/subagents/tool_calls/<request_id>.jsonl`
   (append-only, one JSON line per tool call), and the result JSON carries
   only `{tool_calls_count, tool_call_journal, stop_reason}` — the result
   artifact stays bounded regardless of how many tool calls a run makes.
3. **Phase-3 command allowlist**: runtime-wide fixed list in config (no new
   request-schema field). Still a design note only — phase 3 remains gated
   and unimplemented.
4. **`write` placement**: stays in phase 2 — phase 1 remains strictly
   zero-mutation (cleanest autonomy story; no promotion-gating review needed
   for phase 1 sign-off).
5. **Token budget — implementer verification finding**: confirmed against
   `nanobot/runtime/stop_guards.py`. The harness does **not** invent a second
   budget system: `derive_stop_reason()` / the `budget_<name>` stop-reason
   vocabulary (R13) are reused verbatim for the harness's own stop reason
   string. The one deviation from stop_guards' existing
   `budget_exceeded()` helper: that helper compares strictly `used > cap`
   (an after-the-fact "did we exceed" check, appropriate for cycle-level
   accounting sampled between cycles). The harness instead vetoes a tool
   call the moment `tool_calls_used >= max_tool_calls` — a hard ceiling
   enforced *before* the call executes, not after — because the loop can
   observe its own counter mid-turn and a bounded subagent should never be
   allowed to run one call over budget just because the check is
   after-the-fact. The stop-reason *name* (`"tool_calls"` →
   `stop_reason = "budget_tool_calls"`) is unchanged, so harness runs remain
   comparable to any other stop-guard-tracked run. New config fields:
   `SubagentToolConfig.harness_max_iterations` (default 8) and
   `harness_max_tool_calls` (default 24) — both genuinely new knobs, not
   duplicates of an existing field.

Integration point (resolved by implementation, not a standing open
question): the harness is wired into
`nanobot/runtime/subagent_materializer.py::materialize_subagent_requests`,
not into `nanobot/runtime/bridge.py`. The bridge's own `SubagentManager` path
already runs a full nanobot agent tool-loop (`read_file`/`write_file`/
`edit_file`/`list_dir`/`exec`, see `bridge.py::build_task`'s
"Use your tools" instructions) — a materially richer surface than this
phase-1 harness, and out of scope to touch. The materializer, by contrast,
had no tool-execution path at all for `research_only`/`review_only`/
`bounded_review`-style requests without a configured
`NANOBOT_SUBAGENT_EXECUTOR_COMMAND` — those degraded straight to a blocked
stub. `tool_harness` is a new, explicit, per-request `profile` value
alongside those; every other profile's behavior in
`materialize_subagent_requests` is unchanged byte-for-byte (see
`tests/test_tool_harness.py::test_materialize_research_only_profile_is_byte_identical_to_before`).
`_setup_cycle_branch`/the smoke gate/`_integrate_cycle_to_main` in
`bridge.py` are untouched — this is a separate execution surface, not a
change to promotion.
