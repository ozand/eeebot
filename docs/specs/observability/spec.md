# Observability — spec

_Status: current. Last updated: 2026-06-25._

## Purpose

Observability is the contract that every claim about what the runtime is doing
reduces to a durable artifact that can be read, or to one operator command that
answers the question. The project principle is simplicity and transparency: if a
behavior cannot be observed after the fact from durable state, it does not count
as having happened. This spec is the normative contract; `docs/OBSERVABILITY.md`
remains the explanatory reference (signal map, anti-patterns, walkthroughs).

## Requirements

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
  integrated commits reaching the working repo's `origin/main` — and SHALL NOT be
  measured by cycle count.
- R6. A cycle that produces no integrated change SHALL NOT be reported as material
  progress; narrative/bookkeeping activity SHALL NOT be presented as a kept
  improvement. A stalled `origin/main` while cycles run SHALL be observable as a
  stagnation signal, not masked as progress.

### Durable trace for new behavior

- R7. Every new runtime behavior SHALL leave a durable, post-hoc-readable trace:
  a record in `state/reports/`, a field in a durable artifact, or a journal line.
  A change that cannot be observed after the fact from state SHALL NOT ship until
  it has that observability.

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
