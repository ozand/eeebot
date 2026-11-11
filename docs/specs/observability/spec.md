# Observability — spec

_Status: current. Last updated: 2026-11-01._

## Purpose

Observability is the contract that every claim about what the runtime is doing
reduces to a durable artifact that can be read, or to one operator command that
answers the question. The project principle is simplicity and transparency: if a
behavior cannot be observed after the fact from durable state, it does not count
as having happened. This spec is the normative contract; `docs/OBSERVABILITY.md`
remains the explanatory reference (signal map, anti-patterns, walkthroughs).

## Requirements

### Per-task H5 warning (#2143, ADR-039 Proposed)

The existing cycle-health command exposes a read-only, aggregate-only task-attempt
warning derived from append-only cycle-ledger evidence.

- A `started` row may persist the bridge's already-computed exact `retry_key`;
  rows without a key remain legacy/incomplete evidence. A cycle later recorded as
  `skipped-duplicate` is excluded from the started-attempt count only when the
  outcome joins an exact cycle/key start unambiguously and any supplied retry key
  matches. Same-key keep/resume attempts may legitimately repeat a cycle ID and
  each count as a start; a later duplicate skip for that reused ID is ambiguous
  and yields UNKNOWN whether or not it carries the matching key. Conflicting-key
  reuse, mismatched, unjoined, or
  repeated skip outcomes also yield UNKNOWN.
- Count exact-key starts since an exact-key successful terminal outcome. Edited
  self-directed title text may form a new identity; late-push outcomes without
  exact key lineage are not inferred by time, title, or cycle proximity.
- Report a warning at 12 evidenced unsuccessful starts. Report clear/zero only
  when at least one exact-key success has anchored task history and complete
  start/reset coverage is available. Empty/no-task windows, missing keys,
  malformed or partial reads, expired horizon, unrepresentably large horizon,
  future-dated or timezone-naive rows, missing active ledger with archive-only
  evidence, invalid UTF-8 ledger bytes, unknown resets, or absent global-history
  evidence yield UNKNOWN and no count. Strict H5 ledger reads reject invalid
  UTF-8; a valid literal U+FFFD character remains ordinary Unicode data. The
  existing ledger has no inception/completeness watermark, so
  an apparently complete retained slice cannot justify below-threshold aggregate
  clear when older task keys may have been pruned. Valid unrelated phases remain
  ignorable, but success-shaped rows with
  missing/non-string phase, malformed outcome/key/cycle ID/timestamp, or empty
  identity is UNKNOWN reset coverage and cannot crash health output. Twelve exact
  current starts after an uncertain reset may prove a warning only when the suffix
  since that reset is demonstrably complete; a missing active file, capped read,
  or other coverage gap yields UNKNOWN. Task count remains unknown without global
  completeness evidence.
- Use the cycle ledger's `CYCLE_LEDGER_RETENTION_DAYS` effective horizon (90-day
  default), reading retained archives and active data in timestamp order; this
  does not promise physical age-based pruning of the active file. A byte-capped
  prefix is UNKNOWN because a later reset may be unread.
- Health JSON and text expose only warning/UNKNOWN status and coverage; known
  clear coverage is rendered as clear, and raw retry keys are never shown. The
  warning does not block or alter planner, retry, handled-marker, wake,
  prioritization, or rollback behavior; #2050's global tripwire remains separate.

### Runtime dashboard skill-read scope (#2170)

The canonical runtime dashboard labels the existing skill-read count as retained recorded reads from delivered cycles, not all reads or a time-window count. It explains that zero does not prove a skill was never read outside delivered cycles. This is a presentation clarification; collection, count, schema and missing-data behavior are unchanged.

### Runtime dashboard recovery visibility (#2118)

The existing runtime dashboard presents a bounded, read-only projection of persisted planner recovery state.

- The existing metrics, health, and rendered HTML surfaces SHALL label availability and expose only validated recovery flags and consecutive counts; missing, malformed (including invalid UTF-8), or unreadable state is not healthy zero and SHALL NOT break GET rendering.
- Health status and the HTML recovery badge SHALL reflect stopped/degraded severity separately from source availability: stopped is critical, degraded is caution, and valid normal state is nominal.
- The projection SHALL omit deferred verdict identifiers and arbitrary state payload fields and SHALL NOT trigger recovery writes, resume, cycle execution, or publisher actions.

### Seven operator questions

- R1. From durable state alone (no access to the live process), the runtime SHALL
  be able to answer all seven operator questions: (1) the active goal, (2) the
  current blocker, (3) the backlog hypotheses, (4) why the current task was
  selected, (5) what subagents are doing, (6) the measurable result, and (7) the
  outcome (`keep|discard|blocked|crash`).
- R2. Each of the seven answers SHALL map to a named durable artifact under the
  state root (`state/goals/*`, latest `state/reports/evolution-*.json`,
  `state/current.json`, `state/subagents/*`). An unanswerable question SHALL be
  treated as the runtime working incorrectly, not as a missing-data condition.

### `eeebot cycle-health`

- R3. `eeebot cycle-health` SHALL be a read-only command (it SHALL NOT mutate
  state) usable for operator triage and dashboard ingestion (`--json`).
- R4. `eeebot cycle-health` SHALL report, in a single invocation: the latest cycle
  id, the report path, subagent telemetry (id/path), bridge service status, the
  failed-units count, promotion readiness, and the next recommended action.

### Material progress

- R5. Material progress SHALL be measured by movement of `origin/main` —
  integrated commits reaching the working repo's `origin/main` — and SHALL NOT
  be measured by cycle count.
- R6. A cycle that produces no integrated change SHALL NOT be reported as material
  progress; narrative/bookkeeping activity SHALL NOT be presented as a kept
  improvement. A stalled `origin/main` while cycles run SHALL be observable as a
  stagnation signal, not masked as progress.

### Durable trace for new behavior

- R7. Every new runtime behavior SHALL leave a durable, post-hoc-readable trace:
  a record in `state/reports/`, a field in a durable artifact, or a journal line.
  A change that cannot be observed after the fact from state SHALL NOT ship until
  it has that observability.
- R8. Every armed bridge invocation SHALL persist one `state/bridge/runs.jsonl`
  `phase: run_end` record with `run_id`, `started_at`, `finished_at`, and
  non-negative `duration_s`. The record SHALL carry an explicit run-end
  classification when known (including `loop_breaker_abort`, `wall_clock_abort`,
  `unit_timeout`, or `completion`) and SHALL remain separate from cycle outcome
  rows because a run can end without a cycle.
- R9. Run records SHALL rotate daily and use the bounded cycle-ledger retention
  horizon. Readers SHALL use `state_access` and expose `partial`, `unavailable`,
  and `beyond_retention` conditions rather than converting missing history into
  a zero-valued duration distribution.

### Reflector response diagnostics (#1291)

Reflector error rows in `state/reflector/reflections.jsonl` carry a structured
`parse_reason`, raw `response_chars`, and bounded `response_head` (200 characters)
and `response_tail` (80 characters). Transport failures have `not_attempted` and
empty response diagnostics, never a previous attempt's values.

Malformed text distinguishes `fenced_unclosed`, `fenced_trailing_text`,
`prose_then_fence`, `json_truncated`, and `not_json`. Complete plain/JSON fences
are repaired; successes retain `fenced_json`, while invalid enclosed JSON gets
`fenced_not_json`. `json_truncated` is a text-shape heuristic, not proof of a token
limit. The telemetry recorder retains the authoritative `finish_reason`; the
LLM content-return contract is unchanged. Schema diagnostics distinguish missing
cycle IDs, mismatches, required fields, invalid kinds, and empty details.

The reflector unit permits writes to the state tree, matching the strategist,
so `state/llm_calls` recording is possible while systemd hardening remains intact.

### Named system-prompt provenance (#2179, ADR-057)

Selected production prompt builders preserve their existing emitted bytes and
section order while retaining a transient ordered section representation.
Optional `prompt_provenance` on existing LLM call, prompt, and bounded failure
records correlates a logical provider invocation with an opaque `call_id`, a
bounded in-memory snapshot/delta baseline, producer/schema, and predecessor
call ID. The assembly metadata remains available for repeated dispatches within
the same explicitly identified owner (including proposer fallback), while
assemblies from another owner cannot be reused solely because system bytes
match. Provider calls without an explicit owner use an isolated identity rather
than a shared `unknown` baseline. A reflector job uses one owner window across
its selected cycle prompts while preserving each cycle ID in ordinary telemetry;
a later job invocation starts a fresh baseline. Curator fact-retry batches share
the owner within one job, while later curator runs, strategist invocations, and
narrator invocations get fresh owner windows. Only explicitly allowlisted fixed
role-file bodies may contribute UTF-8 SHA-256 and character length; identity,
soul, goals, workspace, memory, task, runtime, and unknown sections disclose
no fingerprints or structure.
Missing baselines and unavailable calls are not inferred as empty prompts.
This is process-local comparison evidence, not authentication or proof of
provider receipt. It adds no public/dashboard projection, cycle-ledger row, or
prompt capture; current prompt capture behavior remains unchanged.

## Scenarios

### Scenario: operator answers all seven questions from state

- Given a runtime that has run at least one cycle and written durable state
- When an operator inspects state without access to the live process
- Then the active goal, current blocker, backlog hypotheses, task-selection
  reason, subagent activity, measurable result, and outcome are each readable
  from a named artifact under the state root.

### Scenario: single-command triage

- Given a deployed runtime on the host
- When the operator runs `eeebot cycle-health --runtime-state-root <root>
  --runtime-state-source host_control_plane [--json]`
- Then the latest cycle id, report path, subagent telemetry, bridge status,
  failed-units count, promotion readiness, and next recommended action are
  reported, and no state is mutated.

### Scenario: cycles run but origin/main does not move

- Given cycles are executing but `origin/main` in the working repo has not moved
- When material progress is assessed
- Then the run is reported as no material progress (a stagnation signal), not as
  kept improvement measured by cycle count.

### Scenario: new behavior without a durable trace

- Given a proposed runtime behavior that leaves no `state/reports/` entry,
  artifact field, or journal line
- When the change is evaluated for shipping
- Then it is rejected for violating the transparency principle until observability
  is added.

## References

- Reference doc (stays as explanation, not a contract): `docs/OBSERVABILITY.md`
  (seven-questions table, signal map, observability anti-patterns).
- Operator runbook: `EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md` ("Operator health
  check") was removed 2026-07-05 (#613; recoverable from git history).
- Code: `nanobot/cli/commands.py` (`cycle-health` command),
  `nanobot/runtime/health.py` (`build_cycle_health_summary`,
  `format_cycle_health_summary`, `dumps_cycle_health_summary`),
  `nanobot/runtime/state*.py` (durable-state aggregation).
- Related specs: `self-evolving-runtime` (R7/R8 evidence requirements),
  `subagent-bridge`, `promotion-and-release`.
