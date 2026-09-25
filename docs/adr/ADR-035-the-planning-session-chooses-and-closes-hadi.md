---
title: The agent's planning session chooses the work and closes the HADI loop; the outer contour diagnoses and never assigns
status: proposed
date: 2026-09-24
authors: [ozand, architect]
related: ["#1854", "#1852", "#1893", "#1894", "#768", "#878", "#1796", "#1903", "ADR-018", "ADR-027", "ADR-030", "ADR-031", "ADR-032", "ADR-034"]
tags: [autonomy, planning, hadi, proposer, goal-review, reflector, day-judge]
---

# Status

Proposed 2026-09-24. Implements ADR-032 rules 1–3 and ADR-030's loop in the code
that runs, which today implements neither. Becomes accepted when its named tests
land on `main` and the operator flips the status, per `docs/adr/README.md`.

# Context

ADR-032 decided that the agent chooses its own work and the outer contour judges
the day. ADR-030 decided that a cycle is a HADI loop — hypothesis, action, data,
insight — and that each insight feeds the next hypothesis. Both are accepted. The
running release (`a6b5214d`) implements neither. Verified in code and on the host,
2026-09-24:

**Four actors outside the agent decide what it does.**

| actor | model, cadence | what it does today | ADR-032 role |
|---|---|---|---|
| goal review (`goal_review.py`, #768) | gemini, daily | writes the agent's priorities (`derived_priorities.json`, 10 entries, 53–62), under a rule of *"a single-function change of at most ~40 lines in one file"* (`goal_review.py:43`) | should diagnose, not assign |
| reflector | gemini, after each cycle | turns findings into demand items (hints, instruction changes) | should inform |
| selection | harness, per cycle | LRU rotation; the ranking of ADR-027 exists but is off (`SELFEVO_RANKED_PROPOSAL_ENABLED` unset) | ranking is an input |
| proposer (`llm_proposer.py`) | gemini, per cycle | writes the task: *"Implement and commit: X"*, plus *"priorities are handled by the proposer"* | should offer candidates |

**The agent's own planning session plans blind.** It runs after the task is
selected (`bridge.py:5031`), is told only *"Plan the next cycle."*
(`bridge.py:3149`), and writes its plan to the diary. The executor then receives
the proposer's task. On 2026-09-23 the diary's plan block read *"(no plan recorded
yet)"* all day; one plan landed in two days, because the session spent its 20
ticks without a final answer (#1893, fixed on `main`, not deployed).

**The one outer role ADR-032 wants has no reader.** The day judge (#1855) writes
`state/day_verdict/`; nothing reads it.

**The HADI loop is broken at both ends.**

- *H.* The only writer of `state/hypotheses/durable.json` was the daily strategist;
  its timer was retired by #1852, and the file was last written 2026-09-22 03:01.
  The planner's hypotheses go into a plan block that does not land.
- *A.* The executor acts — on the proposer's task, not on a hypothesis.
- *D.* The harness verdict works (#878): of 154 lifecycle entries, 14 supported,
  3 inconclusive, 1 refuted, measured from sidecars the instance cannot write.
- *I.* Nothing writes an insight. A verdict feeds two automatic effects only: a
  refuted title is blocked from re-proposal; a supported one becomes evidence for
  the goal review, which turns it into another small task. No actor decides, from
  what was learned, to continue, revise or drop a line of work.

The consequence the operator sees is the loop doing small, disconnected pieces of
work that it did not choose, planned by nobody, learned from by nobody.

# Decision

### 1. The planning session chooses, before selection

The planning session runs **first**, before any task exists for the cycle. It
receives, as information and never as orders:

- the charter and the capability ladder;
- the operator priorities with their state (ADR-034);
- the day diary and the month file;
- yesterday's day verdict and diagnosis (rule 4);
- the open hypotheses with their verdicts (rule 3);
- the ranked candidates (ADR-027 ranking; the proposer's ideas are candidates).

Its output is the cycle's increment: what will be done, which goal or priority it
serves, the stage claim (ADR-032 rule 4), and the hypothesis it tests. The
executor receives **this** plan as its task. The line *"priorities are handled by
the proposer"* is removed; the executor is never handed a title it did not choose.

**Planner and executor are one agent, so the plan is not an order.** ADR-032 rule 1
already places the choice here — *"Choosing happens in the planning session
(ADR-031 rule 5)"* — and ADR-031 rule 5 defines the two as *"one agent separated in
time, not a manager and a subordinate"*. The executor may amend the plan: when its
own discovery shows the plan is wrong — the work is already done, the premise is
false, the target does not exist — it records an amendment in the diary with the
reason, and changes course within the same goal and hypothesis or ends the
increment early. An amendment is attributed (`amended_by: executor`) and is data
for the next session, not a failure.

**Duties of the old paths are kept, not dropped.** The ranked list the planner
reads keeps every source that exists today, with its urgency:

- the demand trust order is kept exactly as today (`demand.py`: operator
  `priority` first, then `defect`, then `goal-gap` and the rest); a new or changed
  operator priority is shown to the planner in the next cycle, marked *new* — the
  R30 wake-up invariant, held by the harness, not by the model's inclination;
- confirmed defects and evidence of failed cycles follow, marked as such;
- the proposer's ideas and hypothesis candidates follow.

The planner may decline any candidate, including a defect, but must name the
declined defect and why; a defect declined three sessions running is raised to
the operator.

**No plan is a recorded outcome, never an assignment.** When the session produces
no plan, the cycle records `no_plan` and does not run anything else in its place.
A silent fallback to assignment would make rule 1 optional in exactly the cycles
where it matters. Recovery is bounded and visible:

1. every `no_plan` records its cause, in two families: **supply** (the model
   call did not complete — timeout, 503, queue) and **planner** (the model
   answered without a usable plan — empty final, malformed, refused);
2. after **3 consecutive planner-family** `no_plan`, the harness raises
   `planner_degraded` on the dashboard and in the ledger, and the next session
   runs in **minimal mode** — charter, operator priorities, the top five
   candidates and the last diary entry. Minimal mode only shortens the input; the
   harness never picks a candidate for it. The duty to handle new verdicts is
   **deferred, not dropped**: their count is carried and shown, and the next full
   session handles them first;
3. after **6 consecutive planner-family** `no_plan`, the bridge stops starting
   cycles and the state is the loudest item on the dashboard, until the operator
   resumes it;
4. **supply-family** `no_plan` never triggers minimal mode, because a shorter
   prompt does not shorten a queue; a run of them raises `model_supply_degraded`
   instead, the #1899 condition;
5. any session that produces a plan resets both counters. While cycles are
   stopped, diagnostics, health, the publisher and the operator's resume stay
   working.

The thresholds 3 and 6 are starting values, not proven bounds. They ship as a
hypothesis in ADR-030's sense: the ledger records every transition, and the first
two weeks decide whether they are kept.

Infrastructure recovery — model endpoint, deploy rollback, unit health — is not a
cycle's work and never waited on a plan; it stays with the harness processes that
own it today.

**Rest is a choice with a wake condition, and the harness only notices change.**
Most bridge invocations today start no cycle, because the queue is empty. With the
planner first, every timer tick (about three minutes) would otherwise become a
20-tick session on the shared model queue. So the session has a third outcome
besides a plan and `no_plan`:

- **`rest`** — the planner decides that no increment is worth starting now. It is
  structured, not prose: a **wake condition** that names an observable input
  (a hypothesis verdict, an operator priority, a candidate, a commit on `main`,
  a unit or file the harness can read) and a **deadline** for the next look. A
  rest without both is `malformed`. Rest never counts toward the 3/6 `no_plan`
  thresholds.
- **The harness pre-check tests change, never value.** At a rest it records a
  snapshot of the versions of the listed inputs. On the next ticks, before any
  model call and before repository preparation, it compares versions only. If
  none changed and the deadline has not passed, no session runs. Any change, any
  input that cannot be read, or the deadline passing returns the choice to the
  planner. The harness never judges whether a change is important enough — that
  is a choice, and choices are the planner's.
- **Six rests in a row** raise a review signal to the operator. It is a prompt
  to look, not proof of a defect.
- **Three counts stay separate** in the ledger and on the dashboard: timer ticks
  that started no session (pre-check held), `rest` decisions, and `no_plan`.
  Without the split, the saving would be invisible and a silent stop would look
  like healthy idleness.

**An interrupted increment is carried, not retried by the harness.** When the model
supply fails mid-increment (timeout, 503, queue), the harness records the plan as
an `open_increment` with the cause and hands it to the next session first, marked
`interrupted(supply)`; that session decides keep, edit or delete (rule 3), and keep
continues the same plan (ADR-031 rule 2). There is no runtime state called
"supplier paused", so the hold reuses the rest mechanism: the interruption writes
the same input-version snapshot as a rest, with a deadline of now plus a supply
cooldown — 15 minutes, doubling on each consecutive supply interruption up to
60 minutes, reset by any completed model call. The pre-check holds until the
deadline; a changed or unreadable input, or the deadline passing, starts a session,
and the session itself discovers whether supply is back. Held ticks join the
"ticks without a session" count. The same `open_increment` interrupted by supply
three times running is flagged to the operator (#1765) and is never dropped
automatically. A failure that is not supply — code, gate — is an ordinary outcome
and reaches the next session as data, unmarked.

**The work of an interrupted attempt is kept, not only its plan.** Carrying the
plan is not enough if the work is thrown away. `cycle-77ca3cf3580c` (2026-09-25)
ran four attempts; the first three were killed by the unit timeout after 27–36
executor calls each, and every retry began with `checkout … to main` and
`reset: moving to HEAD`, a new opening entry and a new plan: no commit from the
killed attempts survived, about two and a half hours of work.

- **Checkpoint commits.** The executor's work is committed to the cycle branch as
  it goes, at least at every completed step that changed files (ADR-017's
  checkpointing, applied inside a cycle). A kill loses minutes, not the attempt.
- **A retry resumes the branch.** When the next session keeps an interrupted
  `open_increment`, the executor continues on the existing cycle branch with its
  checkpoints; the branch is never reset to `main` for a kept increment. Only
  *edit* or *delete* may start from `main`, and *delete* leaves the branch in
  place, named, for inspection.
- **One opening, one plan chain.** A resumed increment appends to its existing
  diary entry and plan instead of writing a new opening entry per attempt.
- **Checkpoints are not results.** A checkpoint commit is never an integration;
  the gate judges only the finished branch, and rule C of #1903 already excludes
  diary-only and bookkeeping-only changes from counting as a result.

**A duplicate is a recorded outcome too.** When the chosen increment is refused by
the duplicate check, the cycle records `rejected_duplicate` with the evidence sha
and reason, runs nothing in its place, and does not count toward `no_plan`. The
sha and the reason are an input to the next session, or it would choose the same
increment again.

**The plan is appended, never overwritten.** Each session's plan is a new dated
entry in the diary, with the cycle id, above the previous one. Today's writer
replaces a single plan block, so the history of plans is lost and a planner learns
that it "named the same follow-up twice" only by reading commits (diary
2026-09-25). The insight of rule 3 needs the previous plan to exist.

**The plan carries its own size.** It states the forecast iterations for the
increment, and the harness records the actual count beside it (ADR-031 rule 3).
A forecast far below the box is visible to the next session as a sizing datum,
not only to the operator.

### 2. The proposer offers, it does not assign

The proposer's output becomes candidates in the ranked list. It keeps its
evidence duties (#1785: which commits touched the target) and loses the right to
name the cycle's task.

### 3. HADI closes inside the agent

- **H — the planning session writes it.** Each increment carries one hypothesis in
  ADR-030's form: what will become true, the measure, and what result would refute
  it. The harness stores it in `state/hypotheses/` with a stable id; the
  hypothesis entry is instance-written, as today. A statement without a measure
  and a refutation condition is not a hypothesis and is rejected at parse time —
  the plans of 2026-09-25 carried implementation assumptions ("hunk headers are
  sufficient to compute ranges") that no verdict can ever settle.
- **A — the executor.** It works the plan and writes intent to the diary at the
  start, as ADR-028 says, not a result report.
- **D — the harness.** Unchanged: verdicts come from sidecars the instance cannot
  write (#878, ADR-018). The day judge adds the day-level datum.
- **I — the next planning session writes it.** Before choosing, it must, for every
  open hypothesis with a new verdict, record one of: **continue** (keep the line,
  next increment), **revise** (change the hypothesis or the measure, say why),
  **drop** (abandon the line, say what was learned). And for its own previous
  plan: keep, edit or delete. The insight is a diary entry and a lifecycle field,
  not a new task in someone else's queue.

The agent writes the insight; the harness writes the verdict. The agent never
writes the verdict (ADR-032 rule 5). Three constraints keep the insight from
becoming self-evaluation:

- **Verdicts and measures are immutable.** *Revise* creates a new hypothesis
  version with a link to the original; the original's measure and verdict stay as
  they were. *Drop* closes the line and keeps its negative evidence visible; it
  never deletes a refutation.
- **The insight interprets, it does not grade.** It may say what the data means
  for the next step; it may not declare an increment successful against a verdict
  that says otherwise.
- **The backlog is bounded.** A session handles at most five hypotheses with new
  verdicts, oldest first; the count left unhandled is recorded and shown, so a
  queue of verdicts cannot consume the 20 ticks and cannot disappear silently.

### 4. The outer contour diagnoses and never assigns

- **Goal review and the day judge merge into one daily role.** Its question is
  ADR-032 rule 3's: did the day move toward the goal or produce the appearance of
  work, and if it diverged, why — circles over the same files, documentation or
  tests rewritten without effect, work already done offered again. Its output is a
  diagnosis in the diary for the next planning session, graded by the strength of
  its evidence. It writes **no priorities and no tasks**. `derived_priorities.json`
  gets no new writer; the rule *"at most ~40 lines"* is deleted with it.
- **The reflector informs.** Its findings go to the diary and to lessons, where
  the planning session reads them. It stops producing demand items.

### 5. Attribution

Every cycle records who chose the work (`planner`, or `no_plan`), the hypothesis
id, and the insight decision it recorded. Without this no later measurement can
tell whether choosing helped (ADR-032 consequence).

### 6. Decommission map

Each role that loses authority is mapped by what it produces and who reads it, so
no reader is left with a healthy-looking empty input.

Re-checked against every live reader in #1941 (B1 census, 2026-09-25): runtime,
both dashboard generators (`scripts/eeebot_dashboard.py` on the host and
`eeebot-ops-dashboard/scripts/techtree_viewer.py`) and host units.

| role | output today | live readers | after this record |
|---|---|---|---|
| goal review as priority writer | new entries in `derived_priorities.json` | `demand._priority_items`, `llm_proposer` goal context, bridge mission block, `health.py:383`, `eeebot_dashboard.py`, `techtree_viewer.py` derived view, `about_page.py` | **dropped** — no new entries; the existing ten stay readable as `source: derived, legacy` candidates until each is done or the planner drops it. `health.py` and both dashboards show the state `frozen` and the count, never a depth against a limit. These readers change in the same release as the writer stops, or the freeze looks like a healthy queue that went quiet |
| goal review as evidence reader | supported hypotheses → priority candidates | goal review itself; `tech_tree.py` and `scorecard.py` read supported hypotheses for tech-tree nodes | **superseded** by rule 3 for the goal review; the tech-tree readers are unaffected and stay. `demand._hypothesis_items` gains the three-state rule: a missing lifecycle file is `unavailable`, not zero hypotheses |
| reflector demand items | `reflection`/instruction-change demand | `demand`, `llm_proposer` candidate join | **superseded** — findings go to lessons and the diary, which the planner reads. **Kept:** the reflection-context hints (`reflection_context.py`) and the knowledge curator's reading of the journals; only the demand lane closes. The dashboard's cycle-detail join labels reflection as `informational` rather than rendering an empty list |
| rotation selector | the cycle's one item | `llm_proposer` stamps it; ops dashboard renders served times | **superseded** by the planner's choice; ranking (ADR-027) orders the list the planner sees. The dashboard labels the served map `retired (ADR-035)` from the switch-over, so its stale times are not read as recent selection |
| proposer task text | "Implement and commit: X" into `improvements/` requests | executor via `bridge.py`; dashboard proposer panel; `loop_metrics_report.py` | **dropped**. **Drain rule:** at the switch-over every queued proposer request is marked `superseded (ADR-035)` and never executed; its candidate re-enters the ranked list. The proposer panel keeps its history and shows `authority: candidates only` from the switch-over |
| day judge verdict | `state/day_verdict/` | none today (writer runs at 03:00; no runtime, dashboard or unit reader) | **gains its readers** in this record's implementation: the planner (rule 1) and the dashboard. Until they land, the absence of a reader is a known void recorded here, not a healthy state |

**Rates whose meaning changes.** Per-cycle rates computed over the proposer's
cycle population change denominator when planner-first cycles and `no_plan`
arrive: `scorecard.py` (terminal cycles, hypothesis selection rate, idle share),
`judge_daily_movement.py` (total attempts, productive ratio, appearance cycles),
`loop_metrics_report.py` (spawn, gate and integration rates), and the rate panels
of both dashboards. Each carries a version boundary at the switch-over; values are
never compared across it without recalculation on one definition.

A reader whose input is dropped must say so (ADR-034 rule 3), never read the
absence as "nothing to do". The frozen `derived_priorities.json` is labelled
`frozen` wherever it is shown, so it cannot look like a healthy queue that stopped
growing. Before implementation the table is re-checked against every live reader —
runtime, dashboard generator and host units — the way #1699's census was.

# Consequences

- **Order is fixed by what already exists.** #1894 must be deployed first — until
  the planner's plan lands in the diary, rule 1 would hand the executor nothing.
  ADR-034 must be implemented first — the chooser must see the operator's intent.
  And the #1903 measurement window must close first, because this record changes
  both selection and the executor's task.
- **The daily cycle count and its rates change meaning** the day this lands, as
  ADR-031 already warns; readers of per-cycle rates must be identified before it
  ships.
- **`no_plan` cycles will be visible and possibly frequent at first.** That is the
  instrument working. A rising `no_plan` share is a planner defect to fix, never a
  reason to restore assignment.
- **The proposer and goal review lose authority, not code.** Their modules shrink;
  their inputs remain useful as candidates and evidence.
- **Withdrawal condition, per ADR-030 rule 1.** If after the change the day
  judge's `appearance_of_work` share rises, or the share of cycles that reach a
  verified result falls, against the #1903 package baseline, this record is
  withdrawn on the measurement.

# Alternatives considered

**Keep assignment and give the executor a veto.** Rejected: a veto over an order
is still management by order, and the executor already "vetoes" by finding work
done — seven cycles on 2026-09-23 ended as *already done on HEAD — skipped*.

**Let the goal review keep writing priorities, but larger ones.** Rejected: the
size was a symptom. An external model that has never spent an iteration cannot
size an increment (ADR-031 rule 3), and a mind that sets the agent's tasks is the
arrangement ADR-032 exists to remove.

**Let the executor write its own insight at cycle end.** Rejected as the only
place: the executor ends before the verdict exists. The insight needs the datum,
and the datum arrives after the cycle — the next planning session is the first
actor that has both.

## Test Contract

| Decision claim | Test | Currently |
|---|---|---|
| The planning session runs before selection and its plan is the executor's task | `tests/test_agent_chooses.py::test_planner_runs_first_and_its_plan_is_the_task` | not written |
| The executor prompt never contains a proposer-authored task or "priorities are handled by the proposer" | `tests/test_agent_chooses.py::test_executor_never_receives_assigned_title` | not written |
| No plan → `no_plan` recorded, no fallback to the proposer's task | `tests/test_agent_chooses.py::test_no_plan_does_not_fall_back_to_assignment` | not written |
| Each plan carries one hypothesis with measure and refutation condition, stored with a stable id | `tests/test_agent_chooses.py::test_plan_hypothesis_is_stored_with_refutation` | not written |
| The next session records continue/revise/drop for every hypothesis with a new verdict, and keep/edit/delete for its previous plan | `tests/test_agent_chooses.py::test_insight_decision_required_for_new_verdicts` | not written |
| Verdicts are still computed only from harness sidecars; nothing the planner writes changes a verdict | `tests/test_agent_chooses.py::test_planner_cannot_write_verdicts` | not written |
| The daily diagnosis writes to the diary and never to `derived_priorities.json` or demand | `tests/test_agent_chooses.py::test_daily_diagnosis_never_assigns` | not written |
| Reflector findings reach lessons/diary and create no demand items | `tests/test_agent_chooses.py::test_reflector_findings_do_not_enter_demand` | not written |
| Every started cycle — including `no_plan` — records chooser, hypothesis id and insight decision | `tests/test_agent_chooses.py::test_cycle_attribution_recorded_for_every_started_cycle` | not written |
| A new operator priority is shown to the planner, marked new, in the next cycle | `tests/test_agent_chooses.py::test_new_operator_priority_wakes_the_planner` | not written |
| A confirmed defect after a failed cycle reaches the planner first and a decline is named; three declines raise to the operator | `tests/test_agent_chooses.py::test_defect_urgency_survives_and_declines_are_visible` | not written |
| `no_plan` creates no task; 3 planner-family in a row → `planner_degraded` + minimal mode; 6 → cycles stop, dashboard loud | `tests/test_agent_chooses.py::test_no_plan_recovery_is_bounded_and_visible` | not written |
| Six supply-family `no_plan` (model timeouts) never enter minimal mode and raise `model_supply_degraded`; six planner-family ones do | `tests/test_agent_chooses.py::test_supply_and_planner_no_plan_are_counted_apart` | not written |
| A produced plan resets both counters; after a stop, resume works and diagnostics kept running | `tests/test_agent_chooses.py::test_reset_and_resume_after_stop` | not written |
| Minimal mode with pending verdicts defers them with a visible count; the next full session handles them first | `tests/test_agent_chooses.py::test_minimal_mode_defers_verdicts_visibly` | not written |
| No path — minimal mode included — selects the first candidate when there is no plan | `tests/test_agent_chooses.py::test_no_automatic_candidate_selection` | not written |
| The planner's list keeps the demand trust order: new operator priority, then confirmed defect | `tests/test_agent_chooses.py::test_trust_order_new_priority_then_defect` | not written |
| Several new verdicts are handled oldest first up to five, the rest counted and shown | `tests/test_agent_chooses.py::test_verdict_backlog_bounded_and_counted` | not written |
| Revise creates a linked version; the original measure and verdict are unchanged; drop keeps the refutation | `tests/test_agent_chooses.py::test_revise_and_drop_preserve_evidence` | not written |
| The executor may amend the plan with a recorded reason; the amendment is attributed | `tests/test_agent_chooses.py::test_executor_plan_amendment_is_attributed` | not written |
| Every decommissioned output leaves its readers reporting the change, not an empty healthy input | `tests/test_agent_chooses.py::test_decommissioned_outputs_leave_no_healthy_void` | not written |
| Each plan is a new dated diary entry; earlier plans survive | `tests/test_agent_chooses.py::test_plans_are_appended_not_overwritten` | not written |
| The plan's forecast iterations and the actual count are both recorded | `tests/test_agent_chooses.py::test_plan_forecast_and_actual_recorded` | not written |
| A hypothesis without measure and refutation condition is rejected at parse time | `tests/test_agent_chooses.py::test_hypothesis_without_refutation_is_rejected` | not written |
| At switch-over queued proposer requests are marked superseded and never executed | `tests/test_agent_chooses.py::test_queued_proposer_requests_drained_at_switchover` | not written |
| Frozen derived priorities show as `frozen` in health and both dashboards | `tests/test_agent_chooses.py::test_frozen_derived_is_labelled_everywhere` | not written |
| Rate readers carry a version boundary at the switch-over | `tests/test_agent_chooses.py::test_rate_readers_carry_switchover_boundary` | not written |
| `rest` requires a wake condition naming an observable input and a deadline; without both it is `malformed`; rest never counts toward 3/6 | `tests/test_agent_chooses.py::test_rest_is_structured_and_outside_no_plan_counts` | not written |
| The pre-check compares input versions only; unchanged + deadline not passed → no session and no repo preparation; any change, unreadable input or passed deadline → session | `tests/test_agent_chooses.py::test_precheck_tests_change_not_value` | not written |
| Six rests in a row raise a review signal | `tests/test_agent_chooses.py::test_six_rests_raise_review_signal` | not written |
| Held ticks, `rest` decisions and `no_plan` are counted separately | `tests/test_agent_chooses.py::test_held_rest_and_no_plan_counted_apart` | not written |
| `rejected_duplicate` records the evidence sha, substitutes nothing, stays outside `no_plan`, and reaches the next session | `tests/test_agent_chooses.py::test_rejected_duplicate_is_recorded_and_fed_forward` | not written |
| A supply interruption carries the plan as `open_increment`, first and marked, to the next session; the harness never re-runs it by itself | `tests/test_agent_chooses.py::test_supply_interruption_carries_open_increment` | not written |
| The supply hold: before the deadline no session, no repo preparation, no provider call; cooldown doubles to a 60-minute cap and resets on a completed call; after the deadline the session sees the open increment first | `tests/test_agent_chooses.py::test_supply_hold_uses_rest_snapshot_with_backoff` | not written |
| The executor's file-changing steps are checkpoint-committed to the cycle branch; a killed attempt keeps them | `tests/test_agent_chooses.py::test_killed_attempt_keeps_checkpoint_commits` | not written |
| A kept `open_increment` resumes on its existing cycle branch and is never reset to `main` | `tests/test_agent_chooses.py::test_kept_increment_resumes_branch_without_reset` | not written |
| A resumed increment appends to its opening entry and plan chain instead of writing new ones | `tests/test_agent_chooses.py::test_resumed_increment_keeps_one_opening` | not written |
| A checkpoint commit never integrates on its own | `tests/test_agent_chooses.py::test_checkpoint_is_not_an_integration` | not written |
| Staged promotions and pending pushes survive repository preparation that now runs on every started cycle | `tests/test_agent_chooses.py::test_repo_preparation_preserves_staged_and_pending` | not written |

# References

ADR-032 (the agent chooses; the outer contour judges the day); ADR-030 (HADI);
ADR-031 (the box; the planning session); ADR-034 (operator documents); ADR-027
(ranking as input); ADR-018 (the harness judges); #1852 (planning session, "does
not yet change how a cycle's task is chosen"); #1893/#1894 (planner with no final
answer); #768 (goal review); #878 (hypothesis verdicts); #1855 (day judge);
#1796 (ranking, off); #1785 (evidence for done work); #1903 (measurement window).
Host, 2026-09-24: `state/hypotheses/durable.json` last written 09-22 03:01;
`lifecycle.json` 154 entries (14 supported, 3 inconclusive, 1 refuted); diary
2026-09-23 plan block empty; `derived_priorities.json` entries 53–62.
