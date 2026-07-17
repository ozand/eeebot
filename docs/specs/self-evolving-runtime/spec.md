# Self-Evolving Runtime — spec

_Status: current. Last updated: 2026-07-17 (#782: added R36 — the RSI
maturity ladder; current level L0, L1 criteria informational in the metrics
report). Previous entry: 2026-07-17 (#780: R26 extended — the
held-out verification pack joins the fitness function outside the mutable
workspace; the instance cannot read, run, or optimize against its checks)._

## Purpose

The self-evolving runtime is the bounded autonomous engineering operator at the
core of the eeebot **product**. Each cycle it observes durable state, selects one
bounded task, runs an experiment, and writes durable proof before any change is
promoted. It behaves like a bounded engineering operator — orchestrator, planner,
evaluator, bounded-executor manager, evidence-producing control plane — not like
an open-ended chat session. The learning signal is the HADI arc
(Hypothesis→Action→Data→Insight), where each Insight shapes the next Hypothesis.

> This is **product** behavior. How *we* develop this product is in `AGENTS.md` /
> `CONSTITUTION.md`. Explanatory detail is in `docs/ARCHITECTURE.md`,
> `docs/SYSTEM_OPERATION_REFERENCE.md`, and `docs/OBSERVABILITY.md`.

## Requirements

### Cycle contract
- R1. Each cycle SHALL run the model `Observe → Reframe → Specify → Execute →
  Evaluate → Persist`.
- R2. Each cycle SHALL run under a bounded budget (`max_requests`, `max_tool_calls`,
  `max_subagents`, `max_timeout_seconds`, `mutation_scope`) and SHALL NOT silently
  widen scope mid-cycle.
- R3. Every cycle SHALL write durable evidence under the state root:
  `state/reports/evolution-<ts>-<cycle_id>.json` (top-level `result_status` ∈
  `PASS|BLOCK|CRASH`; `experiment.outcome` ∈ `keep|discard|blocked|crash`;
  `changed_files`; `promotion.readiness`), `state/goals/current.json`, and a
  `state/promotions/` candidate when a change is ready to graduate.
- R4. Every experiment SHALL end with exactly one outcome (`keep|discard|blocked|
  crash`), evaluated against a baseline, a current value, and a frontier/best-so-far.
  `blocked`, `crash`, and `discard` SHALL be treated as distinct, never conflated.

### Learning (HADI Insight → next Hypothesis)
- R5. When the active backlog is empty, the next hypothesis SHALL be derived from
  accumulated insights/lessons or a metric delta — not from a hardcoded template.
- R6. "Backlog empty" SHALL NOT be a terminal stall state while actionable insights
  or metric deltas exist.
- R26 (issue #690). When the explicit, finite `state/goals/goal_text.json` priority
  list is exhausted (every priority already done in recent git log), candidate
  generation SHALL fall through to an always-on, open-ended generator —
  `_synthesize_hypothesis_from_state` (`nanobot/runtime/cycle_planning.py`) —
  before any legacy/no-op fallback, so the loop never idles for lack of work.
  The generator composes one candidate per cycle from the two GOAL VECTORS
  parsed out of goal_text.json's free-text mission statement, paired with the
  single most salient concrete STATE signal available on disk (in priority
  order: fresh failure learning, an actionable LessonsDB insight, a
  state/reports PASS/FAIL streak, a state/host_metrics sample). It is
  deterministic and makes no LLM call — the seed is grounded and open-ended
  ("propose and implement one concrete bounded improvement toward this
  vector..."); the actual invention is delegated to the downstream subagent.
  A composed candidate that `_title_already_done_in_git_log` matches is
  rejected and the next (signal, vector) combination is tried; the generator
  returns no candidate only when every combination is already done or no
  state signal exists at all.
- R27 (issue #690). Research-feed candidate selection
  (`_pick_candidate_from_research_feed`) SHALL drop self-referential entries —
  ones whose title or `selection_source` indicate the pipeline's own internal
  review/materialize template (e.g. "Synthesize one new bounded improvement
  candidate...", or a `generated_from_*`/`feedback_*`/`retire_*` source) —
  so the loop never feeds its own meta-task description back to itself as
  "new" content.
- R28 (issue #695, superseded in shape by R30/R32 — see below). Once a
  generation's whole synthesize→materialize→verify chain is complete and no
  verify request is still in flight, the planner SHALL reopen the chain
  (`start_next_improvement_generation`) in the SAME cycle that lands on
  `record-reward` — it SHALL NOT require a second consecutive `record-reward`
  cycle to "confirm" reward accounting first. R30 restates this invariant on
  top of a live-file classification instead of the persisted-status check
  R28 originally specified (see R30's rationale for why that check itself
  became a live gap).
- R30 (issue #697). The generation-restart decision (R28) SHALL be derived
  from **live subagent request/result files only, computed fresh every
  cycle** — never from a persisted task-status field. `cycle_feedback.
  _generation_phase` is the single classifier (`NONE | SYNTH_PENDING |
  MATERIALIZE_PENDING | VERIFY_LIVE | VERIFY_PENDING | GENERATION_DONE`) that
  replaced three independent ad hoc "is the chain complete" checks
  (formerly at `cycle_feedback.py`'s `start_next_improvement_generation`
  branch, its SYNTH-stage fast-path, and its final CORE-lane fallback).
  Rationale: a persisted verify-task status field is written in exactly one
  place, gated on that task being `current_task_id` at the moment it
  completes; if the materialize→verify handoff is ever bypassed (as the now-
  removed `repeated_synthesized_materialization_completion` shortcut in
  `cycle_planning.py` did), the field never transitions out of `"pending"`
  and the restart can never fire again — the exact live gap this issue
  fixed. `_generation_phase` closes it structurally: `MATERIALIZE_PENDING`
  (a materialized artifact exists but no verify request has been written
  yet) now **unconditionally** hands off to
  `subagent-verify-materialized-improvement` — there is no path from a
  completed materialization to `record-reward` that skips writing a verify
  request first. When `_generation_phase` resolves to `VERIFY_PENDING`
  (verify's live result is terminal but not yet reward-accounted), the
  planner accounts reward and reopens the chain in the **same call** —
  folding what R28 called a same-task confirmation round-trip into one
  same-cycle transition, so there is no cross-cycle "confirm" memory left
  for R11 to see and no `record-reward → record-reward` decision is ever
  emitted.
- R31 (issue #697). R11's stall-switch (`_switch_off_stalled_lane`,
  `nanobot/runtime/cycle_persist.py`) SHALL NOT override a feedback decision
  produced by the generation-phase driver (R30) or the idle backstop (R32) —
  any such decision is tagged `lane_category: "generation"` and R11 returns
  it unchanged regardless of the stall signal. R11 retains authority only
  over CORE bookkeeping lanes (approval-gate refresh/verify, repeat-block
  force-remediation) — the only lanes where "stuck on the same task" is
  still a meaningful stall signal rather than a live-recomputed decision
  that happens to repeat.
- R32 (issue #697, ordering fixed by #700). The planner SHALL track
  `cycles_since_productive_spawn`, an integer persisted in the existing
  `state/goals/current.json` snapshot (no new state file), incremented once
  per cycle by default and reset to 0 only when that cycle's live subagent
  request files include a genuinely new, non-`already_done`
  `subagent-verify-materialized-improvement` or
  `materialize-pass-streak-improvement` request that was not already known
  the previous cycle. If this counter exceeds `IDLE_BACKSTOP_CYCLE_LIMIT`
  (6, `nanobot/runtime/cycle_observe.py`), the planner SHALL force a fresh
  synthesize-generation restart on the next cycle regardless of
  `_generation_phase`'s own conclusion — a liveness net that bounds the
  worst-case stall duration even if a future change reintroduces a sink
  `_generation_phase` does not yet cover. Issue #700 fix: this check MUST be
  evaluated in `cycle_feedback._derive_feedback_decision` before the
  retire/restart-mode replay short-circuit (the block that returns a
  recorded decision unchanged when `current_task_id` hasn't moved) — placed
  after, the short-circuit returned the same decision every cycle once the
  planner landed in one of those modes, and the backstop counter could climb
  past the limit without the force-restart ever firing (the live host symptom:
  "stuck on record-reward/retire modes with 0 spawns").
- R33 (issue #700). Independent of `_derive_feedback_decision`'s mode/lane
  state, the coordinator's per-cycle path (`run_self_evolving_cycle`,
  `nanobot/runtime/coordinator.py`, right after the normal feedback-decision
  handoff calls `_write_subagent_request_artifact`) SHALL run a decouple
  guard, `cycle_planning._ensure_verify_request_for_fresh_materialization`,
  every cycle: it finds the newest `state/improvements/materialized-*.json`
  artifact, skips it if its hypothesis title is already done in the selfevo
  git log (`_title_already_done_in_git_log`/`_recent_git_log`), and otherwise
  writes a verify request for it via the same
  `_write_subagent_request_artifact` helper the normal handoff uses (so
  schema/fields stay identical) — reliably making "generate → execute"
  true regardless of the feedback-decision tangle. Both call sites share one
  liveness guard, `_live_verify_request_for_artifact`: a request already
  written for the same `source_artifact` blocks a duplicate UNLESS its
  correlated result has already resolved to a terminal status
  (`_TERMINAL_SUBAGENT_RESULT_STATUSES`: already_done/completed/no_commit/
  blocked) — a stale queued request that already resolved does not count as
  live and never blocks a fresh write for the next generation. This also
  fixes the root cause of the accumulated stale-request pile: the normal
  handoff previously wrote a brand-new request file every cycle it re-landed
  on the verify task, even while an unresolved one was still in flight.
- R34 (issue #739). Behind the `SELFEVO_DETERMINISTIC_PLANNER_ENABLED`
  kill-switch (default `"1"`, i.e. unchanged behavior — any value other than
  the literal `"0"` preserves current behavior), both request-minting call
  sites named in R33 (`_write_subagent_request_artifact` and
  `_ensure_verify_request_for_fresh_materialization`) SHALL return `None`
  without writing a request file when the flag is `"0"`. This leaves the
  subagent bridge's LLM proposer (`docs/specs/subagent-bridge/spec.md` R28)
  as the sole request source; no other coordinator behavior (goals, reports,
  learning, HADI bookkeeping) changes, and the planner's lane code itself is
  not deleted or restructured by this flag — see
  `docs/changes/739-planner-minting-kill-switch/proposal.md`.
- R35 (issue #750). The deterministic planner's candidates (R26/R29) and the
  LLM proposer's proposals alike ultimately pass through the subagent
  bridge's pre-spawn dedup sequence, which SHALL now include a semantic
  near-duplicate check — a local FTS5 existence index over script
  filenames/docstrings, past attempt titles, and hypothesis titles
  (`nanobot/runtime/existence_index.py`) — alongside the pre-existing exact/
  keyword title checks, so a candidate whose wording differs from but whose
  intent duplicates an already-shipped script (e.g. "monitor RAM and memory
  usage" vs. an existing `track_memory.py`) is skipped before spawn instead
  of shipping as a second "success". Full contract (corpus, matching rule,
  kill-switch, ledger bookkeeping) lives in
  `docs/specs/subagent-bridge/spec.md` R35 — this entry only notes that the
  planner/proposer candidates feeding the bridge are now subject to it.
- R29 (issue #695). When `_synthesize_hypothesis_from_state` (R26) is
  exhausted — goal vectors and a state signal both exist, but every (signal,
  vector) combination it can compose is already done in git log — candidate
  generation SHALL fall through to `_open_ended_novelty_directive`
  (`nanobot/runtime/cycle_planning.py`) before the legacy todo.md/research-feed
  fallbacks. Unlike R26's generator, this directive's title is fixed and
  deliberately generic (never names a concrete gap), so it can never itself
  become "already done" and never gets skipped by the bridge's
  `_task_already_done` keyword-overlap check
  (`nanobot/runtime/bridge.py`). Its instructions hand the subagent (LLM) the
  goal vectors plus a list of recently-done commit subjects and ask it to
  invent AND implement one genuinely new bounded improvement itself — novelty
  is delegated entirely to the subagent's judgment rather than to a
  deterministic template, so it cannot collapse into a repeating bounded set.

### Evidence / observability
- R7. From durable state alone, the runtime SHALL be able to answer: active goal,
  current blocker, backlog hypotheses, why the current task was selected, what
  subagents are doing, the measurable result, and the outcome. (See
  `docs/specs/observability/spec.md` once written; `docs/OBSERVABILITY.md` today.)
- R8. The runtime SHALL NOT report narrative progress as material progress; a cycle
  with no file change SHALL NOT be presented as a kept improvement.
- R25. Evidence checks that gate reward or promotion (e.g. "does a concrete code
  change exist") SHALL fail CLOSED: a git-probe error, a non-git workspace, or any
  other inability to verify SHALL be treated as "no evidence", never as "evidence
  present". A materialize-lane reward bonus SHALL require a verified commit
  timestamped at or after the cycle start; a promotion candidate SHALL NOT be
  minted with both `base_commit` and `candidate_patch_hash` null for a
  materialize-lane origin that has no verified diff.
- R26 (issue #765; extended by #780). The instance's fitness function — the
  deterministic scorecard, its metric targets, their sidecars
  (subagent-bridge spec R41), and the held-out verification pack's checkers
  and runtime-generated fixtures (subagent-bridge spec R42) — SHALL live in
  the product runtime (`nanobot/`) and the harness-owned state dir, never
  in the instance's mutable workspace: the instance SHALL NOT be able to
  redefine how its own value is measured, nor read, run, or optimize
  against the held-out checks (the #603 fixed-harness invariant; AIDE²'s
  public/private evaluation split), only move the metrics by doing real
  work.
- R36 (issue #782). The runtime's self-improvement maturity SHALL be
  assessed against the 4-level RSI ladder defined in `CONSTITUTION.md`
  ("RSI maturity ladder"): L0 Delegation (current, honest state) → L1 Net
  Positive → L2 Ignition → L3 Inflection. The L1 criteria (7-day
  confirmed-integration streak from non-`priority` demand kinds, zero
  operator interventions, a declared daily token budget, `heldout_gap`
  ≤ 0.2) are rendered informationally by `scripts/loop_metrics_report.py`
  from existing durable state only — the RSI line SHALL NOT introduce a
  new runtime module or change loop behavior, and per the CONSTITUTION's
  standing invariant, L2+ is out of scope: the outer improver remains the
  dev loop and the gate/harness/fitness stay outside the instance's
  mutable surface (#603, R26).

#### LLM call telemetry (issue #675)

Every LLM call in nanobot goes through the single choke point
`nanobot.providers.base.LLMProvider.chat_with_retry`. That call hooks
`nanobot.observability.llm_telemetry.record_llm_call`, which appends one
best-effort JSON line per call to `<dir>/YYYY-MM-DD.jsonl` (daily rotation),
where `<dir>` resolves from `LLM_CALLS_DIR`, else `<STATE_DIR>/llm_calls`,
else `~/.nanobot/llm_calls`. A telemetry failure never breaks the LLM call —
the whole write path is wrapped and swallows any exception.

Each line has: `ts` (UTC ISO-8601), `model`, `duration_ms`, `prompt_tokens`,
`completion_tokens`, `total_tokens` (0 when the provider's `usage` dict omits
a field), `finish_reason`, `retries` (transient-error retry attempts before
this call returned), `cycle_id`, `component`.

`cycle_id`/`component` are attributed via a `contextvars.ContextVar` that
entry points set for the duration of their work:
- `nanobot.runtime.bridge.main()` — `component=bridge`.
- `nanobot.runtime.tool_harness.run_tool_harness_request()` —
  `component=tool_harness` (propagated across the harness's dedicated thread
  via `contextvars.copy_context()`, since a new thread does not inherit the
  calling thread's context by default).
- `nanobot.runtime.coordinator.run_self_evolving_cycle()` — `component=
  coordinator`, wrapping the `execute_turn()` call where the cycle's own LLM
  work happens.

This JSONL complements (does not replace) the LiteLLM proxy's own spend/
latency logs: the proxy has authoritative per-model cost; this file adds the
cycle_id/component attribution the proxy lacks, so `scripts/llm_calls_report.py`
can show per-model latency/token stats and per-cycle LLM wall-time (a
utilization proxy for how much of a ~10-minute cycle is spent waiting on the
LLM) — `--json` for machine consumption, human table by default.

#### LLM prompt/response recording (issue #693)

The counts-only telemetry above answers "how much/how long" but not "what's
actually in the prompt" — of the subagent's ~23k prompt tokens, ~14k was
unaccounted for. `nanobot.observability.llm_telemetry.record_llm_prompt` is
hooked into the same `chat_with_retry` choke point (right beside
`record_llm_call`, on every return path) and persists the full assembled
`messages` array plus the response `content`/`reasoning_content`/
`finish_reason` for each call.

- **Storage**: `<dir>/prompts/YYYY-MM-DD.jsonl` (one call per line), where
  `<dir>` resolves the same way as the counts-only telemetry
  (`LLM_CALLS_DIR`, else `<STATE_DIR>/llm_calls`, else `~/.nanobot/llm_calls`).
  Each line: `ts`, `model`, `cycle_id`, `component`, `seq` (a per-cycle,
  per-process monotonic counter), `prompt_tokens`, `completion_tokens`,
  `finish_reason`, `messages`, `content`, `reasoning_content`.
- **Rotation + retention (bounded disk on the constrained host)**: on every
  write, any previous-day plain `prompts/*.jsonl` file is gzipped to
  `.jsonl.gz` and the plain file removed; `.jsonl.gz` files older than
  `LLM_PROMPTS_RETENTION_DAYS` (default 14) are pruned. Today's file always
  stays plain/appendable. Both steps are best-effort — a single file's
  gzip/prune failure is swallowed and never blocks the write or the LLM call.
- **Secret scrub**: the serialized record is passed through a small regex
  scrub (`sk-...`, `gh[oprsu]_...`, `Bearer ...` tokens redacted) before
  being written — defensive, since messages already went to the provider.
- **Toggle**: `LLM_CAPTURE_PROMPTS` — default ON; set to `0`/`false`/empty to
  disable (privacy/perf-sensitive runs).
- **Reader**: `scripts/llm_prompt_inspect.py --dir PATH --cycle ID --date
  YYYY-MM-DD --call SEQ [--json]` reads plain and `.gz` daily files and, for a
  selected call, prints each message's role/byte-size/`len//4` token estimate
  and a truncated preview plus totals — evidence for trimming the subagent's
  context.

### Roles
- R9. The coordinator SHALL maintain goal alignment, backlog/prioritization,
  experiment contracts, subagent launches, evaluation, and durable state — and SHALL
  run with `ALLOW_CODE_EDITS=false` (it does bookkeeping, not code edits).
- R10. Subagents SHALL execute bounded tasks only, remain correlated to the parent
  goal/cycle/task, and return concrete artifacts — not invent broader mission scope.

### Termination and progress
- R11. The runtime SHALL track consecutive stalled cycles and, on reaching a
  bounded threshold (default 2), SHALL stop the active goal/lane and record
  `stop_reason="no_progress"` — it SHALL NOT continue iterating. A cycle is
  **stalled** when at least one observable signal holds: the same blocker
  repeats, the cycle produced no `changed_files`, or the verifier/evaluation
  result is unchanged with no frontier movement. Its authority to switch the
  selected lane is scoped down by R31 (issue #697): it may only override CORE
  bookkeeping decisions, never a generation-phase or idle-backstop decision.
- R12. A failed gate (e.g. the smoke gate) SHALL be revised at most a bounded
  number of times (default 3) before the experiment ends with
  `experiment.outcome="blocked"`; the revision count SHALL be recorded and
  revisions SHALL NOT be unbounded.
- R13. A cycle/lane SHALL terminate on an explicit, enumerated stop condition
  and SHALL record which one in `stop_reason`: `gate_clean` (experiment reached
  `keep`/`discard`), `max_iterations`, `no_progress` (R11), or `budget_<name>`
  (any R2 cap exceeded). Termination SHALL NOT rely on budget exhaustion alone.

## Autonomous subagent operating directive

This is the directive given to subagents spawned by the coordinator on the host
(relocated here from `AGENTS.md`, where it did not belong — it is product runtime
behavior, not our dev process).

A spawned subagent is there to **implement, not just review**. If the artifact it
is asked to verify is metadata-only (no file change, no measurable improvement), it
SHALL make the improvement itself:

1. Pick a complete logical task/capability from the runtime backlog (not a micro-step)
   and implement it end-to-end.
2. Write or edit the file(s).
3. Run a quick smoke check on the changed files (import/syntax; full pytest is not
   required for the gate).
4. Commit on the cycle branch (`selfevo/cycle-<id>`), not directly on `main`.
5. The bridge integrates the cycle branch to `main` only after the smoke gate passes.
6. Append a one-line entry to `memory/HISTORY.md`; update `memory/MEMORY.md` if a
   durable lesson was learned.

A session that ends with no edit when a concrete bounded task existed is a FAILURE,
not a success.

## Scenarios

### Scenario: bounded cycle produces durable proof
- Given an active goal and a non-empty bounded budget
- When a cycle runs
- Then `state/reports/evolution-*.json` is written with a single `result_status` and
  `experiment.outcome`, and `state/goals/current.json` reflects the active goal/task.

### Scenario: empty backlog does not stall
- Given the active backlog is empty and ≥1 fresh actionable insight exists
- When the coordinator forms the next hypothesis
- Then the hypothesis is derived from that insight (its title/acceptance reflect the
  insight content), not from a generic template.

### Scenario: goal_text priorities exhausted, loop still never idles (#690)
- Given every priority in `state/goals/goal_text.json`'s "Current priority
  targets" list already matches a recent commit (git-log dedup), and at least
  one concrete state signal exists (e.g. a host_metrics sample or a fresh
  failure-learning record)
- When the coordinator materializes the next improvement artifact
- Then `_synthesize_hypothesis_from_state` produces a new, non-circular
  candidate grounded in a GOAL VECTOR x that state signal, and the
  materialized artifact's `next_bounded_candidate` is that candidate — not the
  self-referential "Priority 99: Synthesize one new bounded improvement
  candidate..." research-feed meta-task.

### Scenario: subagent that finds metadata-only work still produces a change
- Given a subagent is dispatched to verify a materialized improvement that has no
  file change
- When the subagent runs under an open approval gate
- Then it implements a concrete bounded improvement and commits it on the cycle
  branch, rather than reporting "nothing to do".

### Scenario: stalled loop stops instead of spinning
- Given two consecutive cycles whose deliveries have no `changed_files` and the
  same blocker
- When the next cycle would start
- Then the runtime stops the lane and writes `stop_reason="no_progress"` with
  `stall.consecutive >= 2`, rather than running another bookkeeping cycle.

### Scenario: a failing gate is not retried forever
- Given a subagent change that fails the smoke gate
- When it has been revised the bounded number of times (default 3) without
  passing
- Then the experiment ends with `outcome="blocked"` and `experiment.revisions`
  records the attempts.

### Scenario: every stop has a recorded reason
- Given any terminating cycle/lane
- When the cycle report is written
- Then `stop_reason` is set to exactly one enumerated value, so R7 can answer
  "why did the loop stop" from durable state alone.

### Scenario: an unverifiable evidence check never fakes a passing result
- Given a materialize-lane cycle where the git-probe used to detect a concrete
  change errors, or the workspace is not a git repository
- When the coordinator evaluates whether to award the reward bonus or mint a
  promotion candidate
- Then the check returns "no evidence" (fails closed), the 1.2 reward bonus is
  NOT applied, and no promotion candidate is created with null `base_commit`/
  `candidate_patch_hash` — matching issue #565.

## References

- External reference (design input, not a dependency):
  [`ksimback/looper`](https://github.com/ksimback/looper) `loop.yaml`
  (`loop_control.no_progress`, `gates.*.max_revisions`, `stop_conditions`).
- Reference docs: `docs/ARCHITECTURE.md`, `docs/SYSTEM_OPERATION_REFERENCE.md`,
  `docs/OBSERVABILITY.md`, `docs/EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md`.
- Code: `nanobot/runtime/coordinator.py` (`run_self_evolving_cycle`),
  `nanobot/runtime/subagent_materializer.py`, `nanobot/runtime/state*.py`,
  `nanobot/runtime/bridge.py`.
- Related specs: `subagent-bridge`, `promotion-and-release`, `observability`,
  `model-routing`.
