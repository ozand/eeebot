# On-host shadow run protocol — real host model + real instance ledgers (#711)

- **Issue:** #711
- **story_id:** `docs/specs/self-evolving-runtime/spec.md`
- **Status note:** on-host shadow/offline measurement; no live-loop
  replacement; no auto-integration to instance main. Gate for #707. Report
  scripts = #710 (not yet implemented — this run is executed and recorded by
  hand, per the results template). Ledger write-points = #707 (not yet
  implemented — this run uses proxies, see below). Stacked (design-lineage)
  on #706 (`docs/changes/706-shadow-experiment/`).

## Why this run exists

The dev session that authored this kit cannot reach the eeepc host — no SSH
alias, no instance-repo checkout, no host-model gateway credentials are
available from this environment. #711 must therefore run **on the host**,
executed by the **operator**, using the real host proposal model and the real
instance repo/ledgers. That is the entire point of this run: it removes the
fidelity gaps that #706 (run from a dev session, on Sonnet 5, against the
`nanobot` product repo) could not close. This document is the design
protocol; `operator-runbook.md` is the exact procedure; `results-template.md`
is the blank artifact the operator fills in and pastes back to the
orchestrator for the #711 report + GO/NO-GO write-up.

## The five #706 fidelity gaps this run is designed to close

`docs/changes/706-shadow-experiment/protocol.md` names five caveats. This run
closes each as follows:

1. **MODEL FIDELITY** — #706 ran on Sonnet 5, not the weak host model
   (qwen on i386), so #706 validated the *architecture*, not whether the real
   host model can sustain novel, gate-passing proposals. **This run uses the
   real host model**, invoked through the `litellm.env`-configured gateway
   (`cl/`/`an/`/`un/`-prefixed model id), for every proposal.
2. **TARGET-REPO FIDELITY** — #706 ran against the `nanobot` product repo as
   a stand-in. **This run uses the real self-evolving instance repo and its
   real ledgers/state** on the host, not a product-repo proxy.
3. **PROPOSAL-DRAW FIDELITY** — #706's five cycles were independent and
   parallel, each seeing the same static done-proxy, so it tested
   cross-draw novelty but not whether the loop suppresses repeats of its
   *own* prior shadow proposals. **This run is sequential** (minimum 3,
   target 5 cycles): each cycle's context includes the titles of every prior
   proposal accepted *in this run*, actively feeding the running
   ledger-accumulation behavior the real loop would exhibit.
4. **SAFETY-PATH COVERAGE** — no #706 cycle proposed outside the mutable
   surface, so precheck P1/P2 reject paths got zero exercise. **This run adds
   an explicit, deliberate, non-mutating safety-rejection probe** (see
   below) that feeds an out-of-surface target straight to the precheck and
   confirms P1 rejects it before any subagent is spawned.
5. **BASELINE COMPARISON** — #706's old-planner baseline was documented/
   observed evidence, not a side-by-side re-run. **This run's results
   template renders metrics side by side against #706's LLM-shadow numbers**
   (`docs/changes/706-shadow-experiment/results.md`) so the operator/
   orchestrator can read host-model fidelity directly off the delta, not
   just against the old-planner's historical 0-productive-spawn baseline.

## Design

### Sequential, ledger-accumulating cycles

Minimum 3 cycles, target 5, run **sequentially** (not in parallel, unlike
#706) so each cycle can build on the outcome of the ones before it. Each
cycle's compact context includes:

- The eeebot goal vectors (`host/eeepc/etc/goal_text.json`).
- A done-ledger proxy: the instance repo's `git log --oneline -30` (the
  ledger-write-points design in `docs/changes/704-ledger-artifact-memory/
  design.md` is not yet implemented in code, so this run uses the git-log
  proxy `_task_already_done` already relies on, per #703's note that this is
  the stopgap "until #704's ledger schema exists").
- The list of **this run's own prior accepted proposal titles**, appended to
  after each cycle — the mechanism under test for gap 3 above.
- A short state digest (one paragraph on current planner/loop health).

### Fixed harness (held constant, unchanged from #706)

- Mutable surface: `tests/`, `scripts/`, `memory/`, `lessons/`, `docs/`,
  `surfaces/` (per `docs/changes/703-safety-shell-invariants/
  precheck-contract.md`'s `_ALLOWED_PATH_PREFIXES`).
- No-secret constraint.
- One bounded change per cycle.
- Bounded gate: #686 (import-smoke + affected tests + core smoke).
- Only the proposal source varies across this run vs. #706: the real host
  model in place of Sonnet 5.

### Safety-rejection probe

Exactly one step, run once during the sequence (position at the operator's
discretion, suggested after cycle 1 so the mechanism is warmed up), where the
operator manually constructs an out-of-surface proposal — e.g. a target path
like `nanobot/runtime/foo.py`, which is core runtime, not in
`_ALLOWED_PATH_PREFIXES` — and feeds it to the precheck only. This step is
**non-mutating by construction**: the precheck's P1 check runs *before* any
subagent is spawned or any branch created, so confirming a reject here can
never touch the tree. If the operator judges any part of this unsafe to
attempt on the live host for any reason, they document why in the results
template's notes/anomalies section and skip it — the probe is valuable
evidence, not a hard requirement of the run.

### Explicit non-goals

- No integration of any shadow-cycle output to the instance's `main`.
- No changes to runtime/gate code as part of running this experiment.
- No deletion or disabling of the current (fragile) planner — it keeps
  running the live loop throughout; this shadow run is purely observational,
  alongside it.
- All cycle work happens on throwaway/cycle branches with an explicit
  rollback step; nothing here is a persistent artifact in the instance repo
  beyond this run's own note-taking.

## Relationship to #707

#707 (the replacement core loop, and the ledger write-points this report
format depends on) stays **BLOCKED** until this run's filled results meet the
acceptance thresholds below (see `results-template.md`'s GO/NO-GO field and
the thresholds list in the operator runbook). This run does not implement
#707 or #710; it is a measurement gate for #707, executed with the tools
that already exist (#703's precheck contract, #686's bounded gate, the
git-log ledger proxy) plus manual/by-hand recording of the #705 metrics,
since #710's report script does not exist yet.

## Cross-links

- `docs/changes/706-shadow-experiment/protocol.md` and `results.md` — the
  run this improves on; read before running this kit.
- `docs/changes/703-safety-shell-invariants/precheck-contract.md` — P1/P2/P3
  definitions used by the per-cycle precheck and the safety-rejection probe.
- `docs/changes/704-ledger-artifact-memory/design.md` — the ledger schema
  this run proxies (not yet implemented in code).
- `docs/changes/705-observability-metrics/metrics.md` and `report-spec.md` —
  the nine metrics, liveness states, and gate-fail breakdown this run
  records by hand.
- `operator-runbook.md` — the exact on-host procedure.
- `results-template.md` — the blank artifact the operator fills and returns.
