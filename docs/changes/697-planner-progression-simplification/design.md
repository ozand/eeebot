# 697 — Design: simplify the planner's task-progression state machine

This is the design pass for #697 only. No code changes ship with this PR.
Citations are against `main` at the time of writing (commit `5903625`,
"#695 loop never stalls on already-done hypotheses").

## 1. The current machine, mapped

### 1.1 Task-id vocabulary

| constant | file:line | contents |
|---|---|---|
| `CORE_TASK_IDS` | `cycle_observe.py:55-60` | `{refresh-approval-gate, verify-approval-gate, run-bounded-turn, record-reward}` — pure bookkeeping |
| `SYNTHESIZE_NEXT_IMPROVEMENT_CANDIDATE_ID` | `cycle_observe.py:61` | `"synthesize-next-improvement-candidate"` |
| `MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID` | `cycle_observe.py:62` | `"materialize-synthesized-improvement"` |
| `_BACKLOG_PROGRESSION_IDS` | `cycle_observe.py:66-71` | `{synthesize-next-improvement-candidate, materialize-pass-streak-improvement, materialize-synthesized-improvement, subagent-verify-materialized-improvement}` — preferred over CORE on lane-switch (#568) |
| `COMPLETED_TASK_STATUSES` | `cycle_observe.py:73-86` | `{blocked, canceled, cancelled, closed, done, failed, terminal, terminal_blocked, terminal_closed, terminal_failed, terminal_merged, terminal_noop}` |
| `_TERMINAL_SUBAGENT_RESULT_STATUSES` | `cycle_observe.py:93` | `{already_done, completed, no_commit, blocked}` — terminal *result_status* values (distinct from task status) |
| `KNOWN_TASK_IDS` | `cycle_observe.py:117-126` | closed set; anything else is an orphan (#580) and gets retired, never re-created |

Selection predicates: `_task_status` (`:180-183`, defaults `"pending"`),
`_task_is_selectable` (`:186-194`, false if status ∈
`COMPLETED_TASK_STATUSES` or task_id unknown), `_retire_orphaned_task_ids`
(`:197-217`), `_pick_task_for_classes` (`:266-282`, class-preference
fallback — returns `None` if the pool is exhausted or every candidate is
`current_task_id`, see §2.5).

### 1.2 Per-task_id lifecycle

| task_id | defined | entry | exit / next |
|---|---|---|---|
| `refresh-approval-gate` | `cycle_planning.py:1204,1209,1242` | BLOCK or normal path | `verify-approval-gate` or `run-bounded-turn` |
| `verify-approval-gate` | `cycle_planning.py:1205` | after refresh, BLOCK only | pending → next cycle |
| `run-bounded-turn` | `cycle_planning.py:1210,1243` | normal path | `record-reward` |
| `record-reward` | `cycle_planning.py:1211,1234-1239,1244` | default "active" fallback | **see §2 — under investigation** |
| `analyze-last-failed-candidate` | `cycle_planning.py:168-177` | fresh `_latest_failure_learning` | `record-reward` via `retire_terminal_selfevo_lane` |
| `inspect-pass-streak` | `cycle_planning.py:230-239` | `pass_streak >= 3` | `materialize-pass-streak-improvement` via `promote_review_followup` |
| `materialize-pass-streak-improvement` | `cycle_planning.py:178-187` | current == `inspect-pass-streak` | `subagent-verify-materialized-improvement` |
| `synthesize-next-improvement-candidate` (SYNTH) | `cycle_feedback.py:163-197` | restart/fallback branches (§1.3) | `materialize-synthesized-improvement` once `pass_streak>=3`, or discard-pressure fast-path |
| `materialize-synthesized-improvement` (MATERIALIZE) | `cycle_feedback.py:200-264` | selected from SYNTH | `subagent-verify-materialized-improvement` once artifact written, or straight to `record-reward` if no next candidate |
| `subagent-verify-materialized-improvement` | `cycle_planning.py:190-199`, `cycle_feedback.py:1696-1710` | parent = materialize-pass-streak or MATERIALIZE | `record-reward` via `should_retire_subagent_lane` when lane_health is `stale`/`completed`/`terminal_noop` |
| `execute-queued-revert` | `cycle_feedback.py:849-869` | `latest_experiment_revert_queued` | falls back to next selectable |
| `diagnose-blocker` | `cycle_feedback.py:1130-1145` | repeated BLOCK ≥ `REPEATED_BLOCK_LIMIT` (2) | verification/remediation/diagnostic class |

### 1.3 `_derive_feedback_decision` — every mode, in evaluation order

| # | mode | file:line | condition (essence) | next |
|---|---|---|---|---|
| 1 | replay prior decision | `:603-610` | prior recorded mode ∈ retire/restart set, still matches `current_task_id`, selected ≠ current | whatever it already selected |
| 2 | `switch_stalled_lane` (orphan) | `:513-569`, `:649-661` | `current_task_id ∉ KNOWN_TASK_IDS` (#580) | first selectable, backlog-progression preferred |
| 3 | `escalate_underutilized_ambition` (HADI) | `:700-745` | record-reward + MATERIALIZE completed + **confirmed** (see below) + discard/underuse reasons | re-materialize |
| 4 | `synthesize_next_candidate` | `:746-776` | same as #3 minus the ambition-reason requirement | new SYNTH |
| 5 | `start_next_improvement_generation` (#695) | `:777-828` | record-reward + MATERIALIZE completed + `_chain_complete_for_reward_check` (verify **persisted status** done) + `not _has_live_verify_request_queue` | new SYNTH — **the restart** |
| 5b | `record_reward_after_synthesized_materialization` (fallback) | `:829-847` | same outer guard as #5, inner chain-check false | **same task** — see §2 |
| 6 | `execute_queued_revert` | `:849-869` | revert queued | next non-current task |
| 7 | `escalate_underutilized_ambition` (generic) | `:870-909` | underutilization reasons non-empty | MATERIALIZE |
| 8 | `promote_review_followup` | `:910-930` | current == `inspect-pass-streak`, followup selectable | that materialize task |
| 9 | `retire_goal_artifact_pair` | `:941-957` | streak≥limit, no followup | class-preference pick |
| 10 | `continue_active_lane` (×3 near-identical) | `:959-975, 982-998, 1090-1106, 1113-1129` | active task still selectable | itself, no-op |
| 11 | `materialize_synthesized_improvement` (fast-path) | `:999-1038` | current==SYNTH, MATERIALIZE+verify done | new MATERIALIZE |
| 12 | `materialize_synthesized_improvement` (discard-pressure) | `:1039-1070` | current==SYNTH, streak+discard | MATERIALIZE |
| 13 | `retire_goal_artifact_pair` (SYNTH variant) | `:1071-1089` | current==SYNTH, streak≥limit, no discard yet | stays on SYNTH |
| 14 | `force_remediation` | `:1130-1145` | repeat_block ≥ limit | verification/remediation class |
| 15 | `switch_task_class` | `:1146-1152` | `reward_value < LOW_REWARD_THRESHOLD` | execution/verification class |
| 16 | streak-retirement cascade | `:1153-1213` | `strong_pass_count >= GOAL_ROTATION_STREAK_LIMIT`, generic current task | cascades materialize→SYNTH→any selectable→record-reward |
| 17 | `start_next_improvement_generation` (#656/#664 fallback) | `:1215-1244` | `mode=="stable"`, nothing else matched, full chain complete, no live verify queue | new SYNTH |
| 18 | `stable` → `None` | `:1273-1275` | nothing matched | caller replays stale `current.json` |

Branches #3/#4 gate on `post_materialization_reward_already_confirmed`
(`:689-699`) — a **same-task two-cycle memory**: `current_task_id ==
"record-reward"`, the *prior* recorded decision's mode was
`record_reward_after_synthesized_materialization` with
`selected_task_id == "record-reward"`, and the artifact path still
matches. Branch #16 independently re-derives an equivalent check at
`:1188-1200` without the `artifact_path` match clause — **two different
definitions of "confirmed" in the same file**, a duplication risk in
itself.

### 1.4 `cycle_planning.py` — writer side

- `_build_task_plan_snapshot` (`cycle_planning.py:1175-1948`) is the
  writer: it consumes last cycle's persisted `current.json` plus the
  `feedback_decision` computed above, and produces *next* cycle's
  `recorded_task_plan`.
- Two independent retire/complete blocks live **inside** this function,
  separate from `_derive_feedback_decision`:
  - terminal-selfevo retirement (`:1332-1466`, 3 near-duplicate guards)
  - materialize-completion handoff (`:1672-1822`), which sets MATERIALIZE
    `done`, generates the verify task if absent (`:1696-1710`), **and**
    independently computes `repeated_synthesized_materialization_completion`
    (`:1675-1683`) — a **third** implementation of the same
    "record-reward-after-materialization" mode emitted by
    `cycle_feedback.py:829-847`.
- `should_retire_subagent_lane` (`:1825-1893`) is the **only** writer of
  the verify task's persisted status (`:1848-1850`). Its outer guard,
  `current_task_id == "subagent-verify-materialized-improvement"`
  (`:1826`), is load-bearing for §2.
- `_synthesize_hypothesis_from_state` (`:601-709`, #690) only supplies
  *content* for a materialize artifact once a materialize task is already
  selected — it does not decide *whether* to materialize.
- `_write_materialized_improvement_artifact` (`:795-955`) fallback chain:
  MEMORY.md → `goal_text.json` → `_synthesize_hypothesis_from_state` (#690)
  → `_open_ended_novelty_directive` (#695) → legacy `todo.md` → research
  feed.
- `_has_live_verify_request_queue` (`cycle_feedback.py:80-106`) is used
  only inside `cycle_feedback.py` (`:799`, `:1231`); `cycle_planning.py`
  has its own, separate live prober, `_subagent_lane_health`
  (`:964-1017`), consumed by `should_retire_subagent_lane`.

### 1.5 `cycle_persist.py` — R11 stall-switch

`_switch_off_stalled_lane` (`cycle_persist.py:635-695`): if
`should_switch_lane(previous_experiment)` is true (a durable `stall`
counter tripped R11's no-progress guard), and the incoming
`feedback_decision.selected_task_id` **equals** the stalled
`current_task_id` (i.e. it looks like a no-op), it overwrites
`selected_task_id` via `pick_alternative_task` — filtered to selectable
tasks, backlog-progression preferred (#568) — and stamps
`mode="switch_stalled_lane"`. If `pick_alternative_task` finds no
distinct alternative, the decision is returned **unchanged** (the stall
is recorded but not resolved — §2.6).

### 1.6 State diagram (textual)

```
refresh-approval-gate ─▶ verify-approval-gate ─▶ run-bounded-turn ─▶ record-reward
                                                                        │
                          ┌─────────────────────────────────────────────┤
                          │ (default fallback, nothing else active)     │
                          ▼                                             │
                 SYNTH (synthesize-next-improvement-candidate)          │
                          │ pass_streak>=3 / fast-path / discard        │
                          ▼                                             │
                 MATERIALIZE (materialize-synthesized-improvement)      │
                          │ artifact written                            │
                          ▼                                             │
                 subagent-verify-materialized-improvement                │
                          │ should_retire_subagent_lane                 │
                          ▼                                             │
                 record-reward ◀──────────────────────────────────────┘
                          │
                          │  chain complete? (persisted status check)
                          ├─ yes, no live queue ──▶ restart: new SYNTH (#5 / #17)
                          └─ no / ambiguous ──────▶ record-reward (same task, #5b)
                                                       │
                                     R11 stall-switch sees same-task decision,
                                     may overwrite selected_task_id — erasing
                                     the two-cycle "confirmed" memory (#3/#4)
```

## 2. Root-fragility diagnosis

### 2.1 The current live gap, precisely

Confirmed against `cycle_feedback.py:777-847`:

```python
if (
    current_task_id == "record-reward"
    and isinstance(materialized_artifact_payload, dict)
    and materialized_artifact_payload.get("task_id") == MATERIALIZE_SYNTHESIZED_IMPROVEMENT_ID
    and _materialize_task_completed
):
    _verify_task_for_reward_check = _task_by_id.get("subagent-verify-materialized-improvement")
    _chain_complete_for_reward_check = (
        _verify_task_for_reward_check is not None
        and _task_status(_verify_task_for_reward_check) in COMPLETED_TASK_STATUSES
    )
    if _chain_complete_for_reward_check and not _has_live_verify_request_queue(state_root):
        ...  # mode = start_next_improvement_generation  <- the restart
    selected_task = _task_by_id.get("record-reward") or {...}
    return {"mode": "record_reward_after_synthesized_materialization", ...
            "selected_task_id": "record-reward", ...}  # <- same-task fallback
```

The outer guard (record-reward + materialize-completed) is true. The
inner guard is false, and it fails on `_chain_complete_for_reward_check`,
**not** on the live-queue check — `_has_live_verify_request_queue` is
short-circuited and never even evaluated. `_chain_complete_for_reward_check`
reads `_task_status(_task_by_id.get("subagent-verify-materialized-improvement"))`
— the **persisted status field on the verify task record** carried over
from last cycle's `current.json`, not any live subagent-request/result
file. That field is written in exactly one place:
`should_retire_subagent_lane` (`cycle_planning.py:1825-1893`), gated on
`current_task_id == "subagent-verify-materialized-improvement"`
(`:1826`).

If the materialize→verify handoff was ever bypassed — concretely, the
`repeated_synthesized_materialization_completion` shortcut
(`cycle_planning.py:1675-1730`) routes a completed materialization
straight to `record-reward` without ever making the verify task
`current_task_id` for even one cycle — the verify task's status field
never transitions out of its `"pending"` default. `should_retire_subagent_lane`
never runs (its own guard requires verify to be current), so the field is
now **permanently** stuck below `COMPLETED_TASK_STATUSES`.
`_chain_complete_for_reward_check` is therefore permanently `False`, branch
#5 can never fire, and every cycle re-enters the `829-847` fallback,
returning the same-task decision `selected_task_id == current_task_id ==
"record-reward"`.

This is structurally identical to the failure #695 itself fixed one layer
down (the same-task confirmation `post_materialization_reward_already_confirmed`,
`:689-699`, erased by R11 before the second cycle lands, §1.5) — #695
closed it for the case where the *materialize* task's completion is what's
being confirmed; the `829-847` fallback reproduces the identical shape one
level up, keyed on the *verify* task's persisted-status synchronization
instead. **Fixing the specific guard again would only move the sink to
whatever the next-most-derived "is it really done" check turns out to be** —
this is the pattern #697 asks us to stop patching.

### 2.2 Terminal sinks / erasable-confirmation deadlocks (full enumeration)

| sink | file:line | trap | status |
|---|---|---|---|
| CORE round-robin with nothing to restart the chain | pre-#656/#664 shape, guard now at `:1215-1244` | `mode=="stable"`, nothing matched, `_derive_feedback_decision` → `None`, caller replays stale `current_task_id` forever | fixed by #656/#664 |
| verify lane never retires on genuine clean completion | `cycle_planning.py` `_subagent_lane_health` pre-#656 | result-file correlation counted any result ever written, not the one for this cycle's request; `already_done` wasn't terminal | fixed by #656 |
| two-cycle `post_materialization_reward_already_confirmed` erased by R11 before landing | `cycle_feedback.py:689-699` def; `cycle_persist.py:635-695` erasure | same-task decision is exactly what R11 treats as still-stalled; overwritten before confirmation reads back | fixed by #695 (branch #5 reopens same-cycle instead of waiting) |
| **current: #695's own fallback reproduces the shape** | `cycle_feedback.py:829-847`, gated by `cycle_planning.py:1825-1833` | `_chain_complete_for_reward_check` reads persisted verify status, permanently stuck if materialize→verify handoff was bypassed (`:1675-1730`) | **not fixed — the live incident driving #697** |
| duplicated "confirmed" definitions can disagree | `:689-699` vs `:1188-1200` | branch #16's local copy omits the `artifact_path` match; can diverge from #3/#4's | not yet a live incident, flagged as structural risk |
| orphaned `current_task_id` with no selectable alternative | `_orphaned_current_task_switch`, `:550-551` | returns `None`, falls through to generic branches that also find nothing selectable; quiet dead end if backlog is fully exhausted of orphans and chain isn't complete enough for #17 | not yet fixed, same shape |
| `_pick_task_for_classes` exhaustion | `cycle_observe.py:266-282` (def), consumers `:870-909, 1130-1152` | returns `None` when task pool is empty or all-current; caller mode label changes but `selected_task_id` stays unset, `_derive_bounded_tasks_from_plan` falls back to `recorded_current_task_id` — a same-task repeat under a "force_remediation" label | not yet fixed, low likelihood |
| `pick_alternative_task` exhaustion in R11 | `stop_guards.py:209-227` | returns `None` if every selectable task shares `current_task_id`; `_switch_off_stalled_lane` then returns the decision **unchanged** — the stall counter climbs but nothing resolves | not yet fixed |

**The recurring anatomy**, present in every row above:

1. A "chain complete" check keyed on **persisted, single-writer task
   status** rather than live subagent state — three near-duplicate
   implementations exist (`cycle_feedback.py:794-798`, `:1007-1011`,
   `:1226-1230`).
2. A **same-task decision used as cross-cycle memory** (confirm-next-cycle
   patterns), which R11's stall-switch cannot distinguish from a genuine
   stall and may erase mid-flight.
3. **Multiple independent code paths** (inline blocks in
   `cycle_planning.py`'s `_build_task_plan_snapshot`, plus scattered
   branches in `cycle_feedback.py`'s `_derive_feedback_decision`) that can
   each emit the same mode string from slightly different guard
   conditions — impossible to reason about exhaustively, easy to add a new
   sink by accident.

## 3. The simplified model

### 3.1 The invariant

> **The loop always advances toward generating or executing new productive
> work. No state is a permanent sink. No transition depends on multi-cycle
> memory a stall-switch can erase.**

Concretely: every decision is a **pure function of live, currently-true
facts** (what subagent requests/results exist on disk right now, what the
current generation's artifacts say, cycle count) — never of "what did the
decision function return last cycle," because that is exactly the state
R11 can silently rewrite.

### 3.2 One driver, four lanes

Collapse the ~18 branches of `_derive_feedback_decision` and the inline
retire/complete blocks of `_build_task_plan_snapshot` into **one function**,
`decide_next_lane(state) -> Lane`, evaluated top-to-bottom with the first
match winning — no other function may independently emit a lane decision.
`state` is assembled once per cycle from **live** sources only:

```
state = {
  approval_gate: BLOCK | clear,           # live: approval gate file
  repeat_block_count: int,                # live: recent bounded-turn history
  generation_phase: NONE | SYNTH_PENDING | MATERIALIZE_PENDING
                     | VERIFY_PENDING | VERIFY_LIVE | GENERATION_DONE,
  cycles_since_productive_spawn: int,     # see §4
}
```

`generation_phase` replaces every persisted-status "chain complete" check
with **one** live computation: walk `state/subagents/requests` +
`state/subagents/results` for the current generation's artifact, exactly
as `_has_live_verify_request_queue` already does (`cycle_feedback.py:80-106`)
— reused, not reinvented — and classify:

- no materialize artifact for this generation yet → `NONE`
- materialize artifact exists, no verify request written → `MATERIALIZE_PENDING`
- verify request written, no terminal result → `VERIFY_LIVE`
- verify request has a terminal result (`_TERMINAL_SUBAGENT_RESULT_STATUSES`,
  `cycle_observe.py:93`) not yet reward-accounted → `VERIFY_PENDING`
- reward already accounted for this generation's artifact path
  (a single boolean set the *same cycle* reward accounting runs, never
  read back across a cycle boundary as a truth condition) → `GENERATION_DONE`

`decide_next_lane` becomes:

```
1. approval_gate == BLOCK           -> refresh/verify-approval-gate  (unchanged, R-gate safety)
2. repeat_block_count >= limit      -> force_remediation             (unchanged, R-safety)
3. cycles_since_productive_spawn > N-> FORCE new SYNTH generation     (the backstop, §4 — always wins ties below)
4. generation_phase == NONE
   or GENERATION_DONE               -> new SYNTH generation
5. generation_phase == SYNTH_PENDING-> MATERIALIZE
6. generation_phase == MATERIALIZE_PENDING -> write verify request
7. generation_phase == VERIFY_LIVE  -> run-bounded-turn / record-reward bookkeeping (wait)
8. generation_phase == VERIFY_PENDING -> account reward, mark GENERATION_DONE, loop to step 4 in the SAME cycle
```

Step 8 is the key structural change: reward-accounting and the decision to
start the next generation happen **in the same cycle, as one state
transition**, never split across a cycle boundary via a same-task
"confirm" record. There is no `record-reward → record-reward` decision
left to erase, because reward accounting is no longer a *lane you sit on
waiting for next cycle to notice* — it's a side effect applied inline
before falling through to step 4. `record-reward` and `run-bounded-turn`
remain as CORE bookkeeping labels (still recorded for observability/specs
compatibility) but they are no longer **decision states** with their own
branch logic — they're outputs of steps 3/7, not standalone nodes with
independent exit conditions.

### 3.3 What collapses / what's removed

- **Remove**: `post_materialization_reward_already_confirmed` and its
  duplicate at `:1188-1200` (no two-cycle memory needed — step 8 is
  same-cycle).
- **Remove**: the three separate "chain complete" implementations
  (`:794-798`, `:1007-1011`, `:1226-1230`) → one `generation_phase`
  computation, reusing `_has_live_verify_request_queue`'s file-walk
  (`cycle_feedback.py:80-106`) as the live-state primitive.
- **Remove**: `repeated_synthesized_materialization_completion`
  (`cycle_planning.py:1675-1683`) — its job (detect "materialize done,
  handle the handoff") is subsumed by `generation_phase ==
  MATERIALIZE_PENDING` in step 6, which unconditionally writes the verify
  request — there is no path that skips straight to record-reward without
  writing it, closing the exact bypass diagnosed in §2.1.
- **Keep, reuse as-is**: `_synthesize_hypothesis_from_state` (#690, still
  supplies *content* once step 4 decides to synthesize),
  `_open_ended_novelty_directive` (#695, same), `should_retire_subagent_lane`'s
  *file-correlation logic* (repurposed as the live result lookup for
  `generation_phase`, not as a persisted-status writer), R5/R6 approval-gate
  logic, R11's `should_switch_lane`/`pick_alternative_task` (retained as a
  **diagnostic signal and CORE-lane escape hatch only** — see §3.4 — never
  as an override of `decide_next_lane`'s generation-phase steps 4-8).
- **Keep, unchanged**: `_BACKLOG_PROGRESSION_IDS` preference ordering (#568),
  orphan retirement (#580), `KNOWN_TASK_IDS` closed-set validation.

### 3.4 R11's new, narrower role

R11 (`_switch_off_stalled_lane`) currently can override *any* decision,
including ones from generation-phase steps 4-8 — this is exactly what
erased the #695 confirmation. Under the new model, `decide_next_lane`'s
steps 3-8 are computed from live facts every cycle, so a same-task result
from those steps is not a stale confirmation to protect — it's simply
"nothing changed since last cycle," which is only possible if
`cycles_since_productive_spawn` is climbing, in which case step 3 (the
backstop) already dominates before R11 would ever need to intervene. R11
is scoped down to **CORE bookkeeping lanes only** (steps 1-2:
approval-gate, force-remediation) — the only lanes where "stuck on the
same task" is still a meaningful stall signal rather than legitimate
waiting.

### 3.5 Per-historical-stall impossibility argument

| stall | why it's structurally impossible now |
|---|---|
| #656 (verify never retires) | `generation_phase` reads live result files directly every cycle (reusing the corrected #656 correlation logic) — there is no persisted "done" flag for verify to get stuck at; a terminal result is recognized the same cycle it lands. |
| #664 (chain complete, no restart) | Step 4 (`generation_phase in {NONE, GENERATION_DONE}` → new SYNTH) is unconditional and evaluated every cycle; there is no "done tasks are never re-selected" trap because generation phases aren't task-status-gated, they're live-derived. |
| #690 (finite candidate list) | Unaffected/preserved — `_synthesize_hypothesis_from_state` and the open-ended directive are reused verbatim as step 4's content source. |
| #695 (two-cycle confirmation erased by R11) | Step 8 folds reward-accounting and the generation restart into one same-cycle transition — there is no cross-cycle memory for R11 to erase, and R11 no longer has authority over generation-phase steps anyway (§3.4). |
| current (record-reward fallback reproduces #695's shape) | The bypass that caused it (`repeated_synthesized_materialization_completion` skipping the verify handoff) is removed — step 6 unconditionally writes the verify request before any reward accounting can happen, so `generation_phase` can never be ambiguous about whether verify ran. |

## 4. The idle backstop

**Counter**: `cycles_since_productive_spawn`, persisted in the existing
task-plan snapshot (`current.json`, alongside `recorded_task_plan` —
no new state file). Incremented once per cycle by default.

**Reset to 0** when, this cycle, a **real, non-`already_done`
materialization actually spawned a subagent** — i.e. a new
`subagent-verify-materialized-improvement` (or
`materialize-pass-streak-improvement`) request was written to
`state/subagents/requests/` this cycle, and that request is not itself a
short-circuit of an already-known-done artifact (mirrors the existing
`_TERMINAL_SUBAGENT_RESULT_STATUSES` "already_done" distinction,
`cycle_observe.py:93`, so a same-artifact no-op re-check doesn't
falsely reset the clock).

**Force condition**: `cycles_since_productive_spawn > N` unconditionally
selects step 3 (new SYNTH generation) **regardless of `generation_phase`**
— even if the driver thinks it's mid-chain. This is deliberate: if the
driver's own phase computation ever has a gap (a future refactor
reintroduces a sink the design didn't anticipate), the backstop does not
require the gap to be understood or even acknowledged — it just forces
forward motion.

**N = 6, justified**: the observed self-evolving cycle cadence keeps a
full synthesize→materialize→verify round-trip comfortably within 2-3
cycles under normal conditions (materialize and verify each typically
resolve within one cycle of being requested, per the #656/#690 bridge
timing notes in `docs/specs/self-evolving-runtime/spec.md`). N=6 gives a
full round-trip plus one retry margin before declaring the loop idle,
while being tight enough that a genuine stall self-heals within roughly an
hour at the current cycle interval, rather than requiring another
multi-day live-gap investigation like this one. N is a named constant
(not scattered as a literal), so it is trivially tunable if live cadence
data says otherwise.

**Why this makes the whole stall class self-healing**: the backstop does
not need to know *which* sink the driver hit. It only needs one true fact
— "no productive spawn happened in N cycles" — which is derivable
independent of `decide_next_lane`'s internal correctness. Every historical
stall (§3.5) additionally satisfies this backstop's trigger condition as a
second, independent line of defense: even if a future change reintroduces
a sink shaped like #656/#664/#690/#695/current, the backstop bounds the
maximum stall duration to N cycles rather than requiring another manual
diagnosis-and-patch cycle.

## 5. Migration and compatibility

- **#653 (bounded-gate rollout)**: approval-gate steps 1-2 of
  `decide_next_lane` are unchanged in behavior — same predicate, same
  precedence (still evaluated before any generation-phase logic).
- **#678 (consumption-grace period)**: grace-period logic lives in the
  bridge/consumer side, not in `_derive_feedback_decision`'s branch tree;
  unaffected by this collapse. Verify this holds during implementation by
  grepping bridge.py for any direct dependency on the removed branches'
  mode strings.
- **#686 (bounded-gate rollout follow-up / re-seed unblock)**: unaffected;
  same reasoning as #653.
- **#690 (open-ended hypothesis generator)**: explicitly reused as-is
  (§3.3) — this design does not touch candidate-generation content, only
  the decision of *when* to generate.
- **Mode-string compatibility**: dashboards/specs that key off specific
  mode strings (`start_next_improvement_generation`,
  `record_reward_after_synthesized_materialization`, etc.) will see a
  smaller, stable set of modes post-simplification. The implementation PR
  should grep `docs/specs/self-evolving-runtime/spec.md` and any dashboard
  code for mode-string dependencies before removing a string outright, and
  keep the CORE-lane labels (`refresh-approval-gate`, `run-bounded-turn`,
  `record-reward`) as observability outputs even though they stop being
  independent decision states (§3.2).

## 6. Test plan (for the implementation PR)

- **Unit — generation-phase classification**: for each of
  `NONE/SYNTH_PENDING/MATERIALIZE_PENDING/VERIFY_LIVE/VERIFY_PENDING/GENERATION_DONE`,
  construct the minimal live-file fixture (mirroring existing
  `_has_live_verify_request_queue` test fixtures) and assert
  `decide_next_lane` selects the expected step.
- **Regression — each historical stall as a fixture**: replay the exact
  persisted-state snapshot that produced #656/#664/#690/#695's stalls (and
  the current record-reward gap — capture the live host's `current.json`
  + subagent request/result files as a fixture before implementation
  starts) and assert the new driver advances past it in one cycle.
- **Property test — no permanent sink**: for N synthetic cycles with
  randomized live-file states, assert `cycles_since_productive_spawn`
  never exceeds N (i.e. the backstop always fires in time) and that the
  driver never returns the same `(generation_phase, selected_task_id)`
  pair for more than N consecutive cycles without a live-state change that
  justifies it.
- **Backstop unit tests**: counter increments on a no-spawn cycle, resets
  on a genuine new (non-`already_done`) request write, and force-restarts
  at cycle N+1 regardless of `generation_phase`.
- **R11 scope-down regression**: assert `_switch_off_stalled_lane` no
  longer overrides a `decide_next_lane` result whose lane is one of the
  generation-phase steps (4-8), only CORE-lane steps (1-2).
- **Full suite green** (`python -m pytest tests/ -v`) plus a live eeepc
  deploy verification cycle (dev → test → rollout) confirming the loop
  sustains productive cycles across at least one full
  synthesize→materialize→verify→restart round-trip without manual
  intervention.
