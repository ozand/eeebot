# eeebot audit remediation todo

Goal: bring live behavior and operator surfaces closer to the canonical operating contract.

## Priority order

- [ ] 1. Approval truth normalization
  - Problem: approval file can be expired while repo/dashboard surfaces still imply `fresh`.
  - Product changes:
    - recompute approval freshness from `workspace/state/approvals/apply.ok`
    - expose expiry/freshness/ttl fields in runtime + dashboard
    - make overview/cycles/approvals/system truthful at current time
  - Acceptance:
    - if approval is expired at audit time, UI/API say expired/stale
    - no page shows implied PASS/fresh solely from stale copied state

- [ ] 2. Experiment execution status vs evaluation outcome reconciliation
  - Problem: latest experiment can show `result_status=PASS` but `outcome=discard`, which is semantically valid but operator-misleading.
  - Product changes:
    - preserve execution status and evaluation outcome as separate fields
    - show both clearly in overview/experiments/API
  - Acceptance:
    - `/experiments` and `/api/experiments` clearly show PASS + discard as distinct dimensions

- [ ] 3. Canonical current control-plane summary
  - Problem: current blocker / current task / active execution truth is spread across multiple partial sources.
  - Product changes:
    - create one canonical summary object for current control-plane state
    - include goal, blocker, task, experiment, approval, execution, revert, stale flags
    - surface in overview, `/api/summary`, `/system`, `/api/system`
  - Acceptance:
    - operator can answer the key “what is happening now?” questions from one summary object

- [ ] 4. Stale execution control-state repair
  - Problem: active execution control state can be stale/null while project appears in progress.
  - Product changes:
    - tighten stale execution semantics in control snapshot/feed/dashboard
    - clearly separate live execution vs stale/blocked/waiting-for-dispatch
  - Acceptance:
    - no “in progress” active execution with null executor linkage presented as healthy

- [ ] 5. `/api/system` upgrade
  - Problem: `/system` page is useful, `/api/system` is too thin.
  - Product changes:
    - add richer system/control-plane payload to `/api/system`
    - include file previews/control summary useful to operators
  - Acceptance:
    - `/api/system` meaningfully reflects `/system`

- [ ] 6. Verification and proof
  - Run targeted tests and live checks after each slice.
  - Capture final proof note if all slices land cleanly.

## CI stability

- [x] 7. Fix two pre-existing `main` test failures blocking all PR CI (done — 2026-06-23)
  - Problem: `main` CI is red on every PR due to two failures unrelated to the PR under test:
    - `tests/test_research_feed_backlog_fallback.py::test_auto_seed_adds_priorities_when_empty` — `NameError: _task_already_done` (the AST test-loader extracts `_auto_seed_backlog_from_research` but not its `_task_already_done` dependency).
    - `tests/test_runtime_coordinator.py::test_cycle_executes_configured_subagent_executor_and_consumes_completed_result` — `executed_count` stays 0 (`assert 0 == 1`): a configured subagent executor's completed result is not consumed.
  - Fix (minimal, no over-engineering):
    - Bug 1 (test-only): stub `_task_already_done -> False` in the one test that reaches it (real fn returns False for fresh titles in a temp repo; the function is not under test here). No change to the fragile AST loader.
    - Bug 2 (production): `nanobot/runtime/subagent_materializer.py:346` — add `"bounded_execution"` to the executor-eligible profile gate. The coordinator emits `bounded_execution` requests (coordinator.py:2056/2488/3083) but the gate dropped them, so materialization could never run on a configured executor. Gated on `configured_executor` being set, so hosts without the executor env are unaffected.
  - Proof: both target tests pass; full suite `993 passed` (was `991 passed, 2 failed`); zero new ruff findings on changed files.
  - Rollout: dev = canonical `eeebot` (PR); rollout to running `eeebot-self-evolving` (PR) — see commit/PR links in HISTORY.

## Self-improvement engine (HADI loop)

- [x] 8. Close the Insight → Hypothesis arc (root-cause of backlog-exhaustion stalls) — done 2026-06-23
  - Problem: H→A→D→I arcs are closed, but **I → next H is open**. Insights accumulate in `lessons.yaml` and are only re-read as pitfall-context for an already-selected task (`LessonsDB.query_for_task`, coordinator.py:2458); they do NOT drive next-hypothesis generation. When the backlog empties, the next hypothesis comes from hardcoded templates (`_synthesized_*_candidate`, coordinator.py:250/270) or an exhaustible `research/feed.json` — neither reads accumulated insights. No content gradient → synthesize→materialize→discard busywork → stall.
  - Product changes:
    - feed top-N fresh `reusable_insight`/`generalized_insight` from `LessonsDB` + recent `reward_signal`/material-progress delta into `_synthesized_*_candidate` / `_write_research_feed`
    - derive candidate `title`/`acceptance` from those inputs (not a static string), preserving HADI metadata + DoR/DoD
  - Acceptance:
    - with an empty Active backlog and ≥1 fresh insight, coordinator forms a concrete hypothesis derived from that insight (unit-tested), not a generic template
    - no regression to HADI escalation / goal-rotation
    - "backlog empty" is no longer a terminal stall state while insights/metric deltas exist
  - Design: `docs/EEEBOT_INSIGHT_HYPOTHESIS_LOOP_CLOSURE.md`
  - Implementation: `coordinator.py` — new `_freshest_reusable_insight(workspace)` reads newest `reusable_insight` from `LessonsDB`; `_synthesized_{next,materialize}_improvement_candidate` accept `insight=` and derive title/acceptance/hadi_cycle from it (template fallback preserved); wired at the central `generated_candidates` assembly in `_build_task_plan_snapshot`, so this cycle's insight flows into the candidate → `feed.json` → next cycle's hypothesis.
  - Proof: `tests/test_insight_driven_hypothesis.py` (7 tests, incl. backward-compat + freshest-wins); full suite `1000 passed`; zero new ruff findings on changed files (9 pre-existing in coordinator.py are outside changed ranges).
  - Rollout: dev = canonical `eeebot` (PR) → CI → rollout to `eeebot-self-evolving` (PR)

- [x] 9. Rank insights by goal-relevance + reward_signal instead of just newest — done 2026-06-23
  - Problem: #8 seeds the next hypothesis from the *freshest* insight only. A newer but off-goal / low-reward insight can crowd out a more relevant, higher-reward one → weaker hypotheses.
  - Product changes:
    - rank lessons by goal-keyword relevance (primary) + parsed reward value (`Positive reward signal: X` / `reward=X`, already embedded by lessons.py) + recency (tiebreak); select top insight for the active goal
    - `_select_insight_for_goal(workspace, goal_id)` replaces the `_freshest_reusable_insight` call at the candidate-assembly wiring point; no lesson-schema change
  - Acceptance:
    - a goal-relevant insight is selected over a newer off-goal one (unit-tested); reward breaks ties among equally-relevant insights; empty DB → None; freshest returned in the degenerate all-zero case
    - backward-compatible: `_freshest_reusable_insight` preserved as fallback
  - Implementation: `coordinator.py` — `_lesson_reward_value` (parses embedded `reward=`/`Positive reward signal:`), `_goal_relevance_tokens`, `_rank_insights_for_goal` (score = 10·relevance + reward + recency), `_select_insight_for_goal`; wired at `_build_task_plan_snapshot` replacing the `_freshest_reusable_insight` call (goal_id in scope). No lesson-schema change.
  - Proof: 4 new tests in `tests/test_insight_driven_hypothesis.py` (reward parse, relevant-over-newer, reward tiebreak, empty→None); full suite `1004 passed`; changed-code ranges ruff-clean.
  - Rollout: dev = canonical `eeebot` (PR) → CI → rollout to `eeebot-self-evolving` (PR)

- [x] 10. Executed materialize/synthesize lane was generic → no-op materialization — done 2026-06-23
  - Live evidence (eeepc, 8 cycles / ~80 min after #8/#9 deploy): every cycle `PASS/keep` but `changed_files=None`, `derived_from_insight=False`. `materialize-synthesized-improvement` ran (status=done) yet produced no concrete change → metadata-only no-op.
  - REJECTED first approach: "force materialize after N change-free PASS cycles" (`_change_free_pass_streak` + escalation reason). Conflicts with the designed pass-streak ladder and broke 15 tests; reverted. Lesson: don't blanket-override the lane cadence.
  - Root cause: #8/#9 enriched the `generated_candidates` *feed*, but the lane the subagent actually executes comes from `feedback_decision.selected_task` built in `_derive_feedback_decision` via `_synthesized_{materialize,next}_improvement_candidate(...)` **without `insight=`**. So the executed materialize task was always a generic template → vague subagent prompt → metadata-only artifact → `changed_files=None`. #8/#9 were effectively dead on the executed path.
  - Fix (`coordinator.py`, single point): thread `workspace` into `_derive_feedback_decision`; just before building the decision dict, if `selected_task` is the synthesize/materialize lane and not yet insight-derived, re-create it via the factory with `insight=_select_insight_for_goal(workspace, goal)`. Its title flows `selected_task_label → _derive_bounded_tasks_from_plan →` the subagent prompt, so the executed lane now carries a concrete insight-derived hypothesis. Backward-compatible: no workspace / no lessons → `insight=None` → generic (all 15 cadence tests still pass).
  - Proof: `tests/test_insight_executed_lane.py`; full suite green.
  - Follow-up (all-paths fix): the first version enriched `selected_task` only on the fall-through path, but `_derive_feedback_decision` returns the decision via many EARLY `return {...}` branches (e.g. `synthesize_next_candidate`), which bypassed it — live re-observation showed the executed `synthesize` lane still generic. Moved enrichment to a single caller-side post-processor `_enrich_decision_lane_with_insight(decision, workspace, goal)` applied to the RETURNED decision in `run_self_evolving_cycle`, so every return path is covered. `tests/test_insight_executed_lane.py` (6); full suite `1010 passed`.
  - Rollout: dev = canonical `eeebot` (PR) → CI → deploy to eeepc + re-observe that the executed lane is insight-derived and `changed_files` ≠ NONE.
  - Live result after all-paths deploy: executed `synthesize` lane became insight-derived (`insight_in_lane=True`, "Synthesize … from insight: …") — HADI I→H confirmed closed on the executed path. BUT `changed_files=NONE` persisted: the loop's own lessons are non-actionable meta-insights ("Consolidate this pattern", no file target), so the hypothesis was concrete-in-form but vague-in-content → see #11.

- [~] 11. Goal-source hypothesis: self-evolve on OUR goals when lessons are non-actionable — in progress 2026-06-23
  - Root cause (from #10 live data): a loop running metadata-only cycles generates vague meta-lessons; with no actionable (file-targeted) insight, even an insight-derived materialize lane gives the subagent no concrete target → `changed_files=None`. Autoresearch needs a concrete target ("train.py"); when the backlog is empty, ours is missing.
  - Fix (`coordinator.py`): `_insight_is_actionable(text)` (insight names a concrete `.py/.md/.yaml/...` artifact); `_next_open_goal_hypothesis(workspace)` reads the top open `- [ ] N. Title` + `Problem:` from `todo.md` (shipped in the workspace/release). In `_enrich_decision_lane_with_insight`, when the best lesson insight is not actionable, use the top open todo goal as the hypothesis → the materialize/synthesize lane targets OUR goals.
  - Acceptance: with a vague lesson + a todo.md open goal, the executed lane's hypothesis is the concrete goal (unit-tested); actionable lesson insight still preferred; no todo/no lessons → unchanged.
  - Proof: `tests/test_insight_executed_lane.py` (9); full suite `1013 passed`.
  - Rollout: dev = canonical `eeebot` (PR) → CI → deploy to eeepc + observe `changed_files` ≠ NONE.

- [~] 12. Execute step: route OUR goals into the subagent's executable task — in progress 2026-06-24
  - Root (traced to the bottom): the bridge subagent IS imperatively told to implement+commit, and the materialized artifact's `next_bounded_candidate.{title,backlog_instructions}` with acceptance "Implement and commit Priority N" is what it reads — BUT when the MEMORY backlog is empty, `_write_materialized_improvement_artifact` fell back to `_pick_candidate_from_research_feed` (a stale "Exploit dashboard" candidate), so the subagent never worked on our goals → `completed_no_commit`. #11's goal reached `selected_task_title` (synthesize lane) but NOT `next_bounded_candidate`.
  - Fix (`coordinator.py`): `_next_open_goal_as_backlog_task(workspace)` returns the top open `todo.md` goal as `{title, instructions, priority}`; `_write_materialized_improvement_artifact` takes `workspace` and uses it as Fallback-1 (before the stale research feed) when MEMORY backlog is empty → the subagent's concrete task becomes our goal with "Implement and commit" acceptance + full instructions.
  - Proof: `tests/test_goal_backlog_routing.py` (3); full suite `1016 passed`.
  - Open dependency: subagent reliability — even concrete tasks have yielded `commits_pushed=0` (mini model `cl/gpt-5.4-mini`). If first material commit still doesn't appear post-deploy, the remaining lever is subagent model/profile (operator cost decision), not coordinator logic.
  - Rollout: dev = canonical `eeebot` (PR) → CI → deploy to eeepc + observe first `changed_files` ≠ NONE.

- [~] 13. Subagent request directed VERIFY not IMPLEMENT → 0 commits (NOT the model) — in progress 2026-06-24
  - Correction: the executor model `un/qwen3.6-27b-mtp` is a REQUIRED local-GPU model that writes code fine — not to be changed. The blocker was never the model.
  - Root cause (live): every emitted subagent request was `subagent-verify-materialized-improvement` with `verification_role=materialized_improvement_review` and `task`="Use one bounded subagent-assisted **review to verify** the materialized improvement artifact". The subagent (qwen) correctly reviews → no code. The concrete goal sat in `next_bounded_candidate` but the request's verify framing overrode it. No IMPLEMENT request was ever emitted.
  - Fix (`coordinator.py` `_write_subagent_request_artifact`): when the materialized artifact carries a concrete goal (`next_bounded_candidate.title` + `backlog_instructions`), set the request's `task`/`task_title`/`recommended_next_action` to an "Implement and commit Priority N: <goal>. <instructions>" directive and `verification_role=materialized_improvement_implementation`. Falls back to verify when no goal. Per the operating contract, Execute must perform the work.
  - Proof: `tests/test_implement_subagent_request.py` (2); full suite `1016 passed`.
  - Rollout: dev = canonical `eeebot` (PR) → CI → deploy → observe first subagent commit on our goal with qwen.
