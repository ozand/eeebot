# Decision: ledger-based, state-light autonomous improvement loop

- **Issue:** #702 (ticket A of the loop-redesign set A-G, #702-#708)
- **Status:** ratified direction; no code changed by this record
- **story_id:** docs/specs/self-evolving-runtime/spec.md

## 1. Decision

The project adopts, as its target direction, a **state-light / ledger-based**
autonomous improvement loop, following the KB formula validated against
external agent-systems practice (Anthropic "Building Effective Agents",
LangGraph, OpenAI Agents SDK, and the agents_library KB):

> Stable minimal core loop + fresh-context subagents + artifact/ledger memory
> + fixed harness + replaceable safety/observability shells.

Target core-loop shape:

```
observe
  -> build compact context
  -> LLM proposes ONE bounded new task
  -> lightweight precheck
  -> run exactly ONE fresh-context isolated subagent
  -> hard gate
  -> append ledger
  -> repeat
```

State is **operational/artifact truth** (done ledger, failure ledger,
integration record, prompt/response dump, telemetry) — not a control graph.
The LLM proposes against the ledgers; safety and observability read the same
truth. No cross-cycle semantic state machine drives the next action.

## 2. Deprecation

Local patching of the current reward/lane/discard/HADI/stall-switch
control-plane state machine is **deprecated**. Specifically:

- reward, experiment/lane assignment, discard, pass-streak, HADI, and the
  stall-switch are **not carried into the core loop**.
- If any of that logic is retained at all, it survives only as **passive,
  post-cycle analytics** that read the ledgers after the fact — it may never
  gate liveness, block a spawn, or steer the next proposal. This is optional
  and scoped to issue #708.
- No further local fixes to the control-plane machinery should be invested in
  as a strategy for stall/convergence problems; the fix is architectural
  (this direction), not another patch to the state machine.

## 3. Not-now / gating

This record ratifies **direction and deprecation stance only**:

- No code is changed and nothing is deleted by this decision.
- The current (control-plane) loop **stays live** until the shadow experiment
  (#706) validates a replacement against fixed success thresholds.
- Implementation of the replacement core loop (#707) is **gated on #706**
  passing its go/no-go criteria — it must not start beforehand.
- Ledger schema (#704) and metrics (#705) are separate design efforts; this
  ticket commits only to the loop being built on artifact/ledger state, not
  their specifics.

## 4. Safety shell independence

The immutable safety shell is **loop-independent** and is not weakened,
relaxed, or reinterpreted by this redesign. It applies identically to the
current loop, the shadow experiment, and any eventual replacement:

- green-only integration (`origin/main` never advances on red, error, or
  timeout; the gate fails safe), protected paths / mutation-surface limits,
  no-secret checks, a suite-shrink guard (a subagent cannot weaken the suite
  it is judged by), a git-verifiable rollback record every cycle, a
  concurrency lock plus exactly one bounded subagent per cycle, stop-guard
  time/iteration budgets, and a bounded gate sized to the host's per-cycle
  time budget (the full suite does not run per cycle).

These invariants are documented and frozen in detail by issue #703; this
record only states that the loop redesign may consume that shell but may not
alter it.

## 5. Rationale

Evidence from this project's own history motivates the direction change:

- Approximately seven sequential planner fixes (#656, #664, #690, #695, #697,
  #700) targeted the reward/lane/discard/HADI/stall-switch control-plane and
  did **not** converge: the machinery kept getting repaired while the
  underlying hypothesis content stayed already-done, and the loop repeatedly
  produced zero productive spawns.
- Over the same period, the execution half proved reliable: bridge spawn ->
  bounded gate (#686) -> integrate-on-green (#653), hardened further in #678.
- Conclusion: failure is concentrated in the **stateful planner** and in
  **bounded-deterministic novelty generation**, not the execution/safety
  shell. Patching the control-plane state machine has a demonstrated ceiling;
  replacing its role with an LLM proposal grounded in ledger truth, inside
  the same proven execution shell, is the more promising direction — pending
  the shadow experiment (#706) confirming the LLM path sustains novelty and
  productive, gate-passing work.

## 6. Cross-links and dependency order

Implementation constraints for F #707: see [`design-constraints.md`](design-constraints.md)
(added per #722).

```
A #702 -> (D #703, C #704, E #705) -> B #706 (go/no-go gate) -> F #707 (gated on B) -> G #708 (optional)
```

- **A #702** (this record) — architecture decision: adopt the ledger-based
  state-light loop; deprecate control-plane patching. Foundation for D, C, E.
- **D #703** — freezes and documents the immutable safety-shell invariants
  and the per-cycle precheck contract; must be settled before B and F.
- **C #704** — designs the done/failure/integration/prompt/telemetry ledger
  schema that is the loop's only durable state; minimal form is a prerequisite
  for B, E, and F.
- **E #705** — defines observability metrics (liveness, novelty, integration,
  harvest, cost) computed purely from the ledgers; minimal form is a
  prerequisite for B.
- **B #706** — the shadow experiment: evaluates LLM-proposed bounded tasks
  under the fixed harness, side by side with the current planner, without
  replacing the live loop. This is the **go/no-go gate** for F.
- **F #707** — implementation: replaces the planner with the minimal core
  loop, gated strictly on B passing its success thresholds.
- **G #708** — optional, after F: migrates any retained reward/HADI value
  into passive, non-blocking analytics over the ledgers.

Nothing in F starts until B passes its success thresholds.
