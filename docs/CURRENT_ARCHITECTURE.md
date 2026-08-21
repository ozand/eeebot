# Current Architecture

_Status: current. Last updated: 2026-08-12._

The authoritative, one-page map of how eeebot's self-evolving runtime works
**today** — the state-light proposer loop adopted by #702–#708 (June–July 2026),
replacing the retired lane/HADI/reward control-plane machinery. Depth lives in
`docs/specs/` and `docs/changes/`; this page is the index that ties them
together. Every claim below traces to a module docstring or a linked change doc.

## Three-repo model

- **`ozand/eeebot`** — the **product**: the runtime code (`nanobot/`), specs,
  the host deploy tooling, and the harness that owns fitness. Canonical source
  (`docs/specs/promotion-and-release/spec.md` R1).
- **`eeebot-self-evolving`** — the **instance**: the mutable workspace the loop
  edits (`scripts/`, `surfaces/`, `tests/`, `memory/`, `docs/`). Checked out on
  the host beside the state root; the loop integrates onto its `main`
  (`docs/specs/subagent-bridge/spec.md`).
- **`ozand/eeebot-ops-dashboard`** — the ops **dashboard** (WSGI), extracted in
  #617 and canonical there since 2026-07-05 (`docs/specs/migration/spec.md`).

The fitness function (scorecard, targets, sidecars, held-out checkers) lives in
the **product** runtime and the harness-owned state dir — never in the instance
workspace, so the instance cannot redefine how its own value is measured
(#603 invariant; `docs/specs/self-evolving-runtime/spec.md` R26).

## The live loop, end to end

One timer-paced process invocation = one bridge cycle
(`nanobot/runtime/bridge.py`). State is operational/artifact truth (ledgers,
result files, telemetry) — not a control graph (#702 decision).

1. **Propose (sole task source).** The LLM proposer
   (`nanobot/runtime/llm_proposer.py`) is the only source of new tasks: the
   deterministic planner's request-minting lane was first kill-switched off
   (#739) and later deleted (#747 — `cycle_planning.py` itself persists with
   its still-used helpers), so the coordinator mints no requests — and since
   #900/#910 it has no live entrypoint at all (`app.main` and its systemd
   units were deleted; the coordinator modules are inert, imported only by
   the live modules listed above). The #707
   replacement went GO on 2026-07-13. Behind `SELFEVO_DEMAND_DRIVEN_ENABLED`
   (default ON, #760), the proposer works **only when there is demand**: the
   LLM *selects and refines*
   one presented demand item and never invents from a bare inventory. No demand
   ⇒ **zero LLM calls** and one `{phase: "idle", reason: "no_demand"}` ledger row.
2. **Dedup chain (pre-spawn).** A proposal passes the bridge's dedup sequence
   before any subagent spawns: **already-done** (`_task_already_done`, git-log /
   done-ledger keyword overlap), **recent-failure** suppression
   (`_recent_failure_match`, structured-intent keyed, R37/#757), and the
   **existence index** (`nanobot/runtime/existence_index.py`, FTS5 over script
   filenames/docstrings + past titles, R35/#750) that catches intent duplicates
   whose wording differs.
3. **Bounded gate.** Exactly one fresh-context subagent runs, commits on a cycle
   branch (`selfevo/cycle-<id>`), then the **bounded smoke gate** (#686): import
   `py_compile` of changed files + the tests they affect + a small fixed core
   set — NOT the full suite (sized to the host per-cycle budget). A
   **mutation-surface** violation is a hard block (R12a). The gate **fails safe**
   — any error/timeout/missing-pytest is treated as failure.
4. **Integrate on green (script tier).** Only on a clean gate does the bridge
   merge a **script-tier** cycle into the instance repo's `main`
   (`_integrate_cycle_to_main`, merge `--no-ff`). `origin/main` never advances on
   red — the git-verifiable rollback guarantee
   `main_sha_before == main_sha_after` when not integrated (#653/#678).
4b. **Two-tier surface (#812).** The mutation surface has two tiers
   (`_classify_mutation_surface`). The **script tier** (`surfaces/`, `scripts/`,
   `memory/`, `lessons/`, `docs/`, `tests/`) auto-integrates as in step 4. The
   **runtime-slice tier** — an operator-approved slice of `nanobot/runtime/*.py`
   modules opted in via `SELFEVO_RUNTIME_SLICE` (empty ⇒ off) — makes the loop's
   PRIMARY goal (Vector 1: optimize its own runtime) reachable, but such a cycle
   is **never auto-integrated**: on a green stricter gate it lands as a pending
   promotion candidate (`state/promotions/`, `review_status=not_ready_for_policy_review`)
   with a rollback record, for operator review + a product PR. An immutable
   deny-set (`bridge.py`/`promotion.py`/`coordinator.py` + gate/safety/approval
   modules by token) is never mutable even if listed in the slice env — the
   allow-slice can only add compute modules, never re-open the safety shell
   (R12b, S2; #603 bounded blast radius).
5. **Cycle ledger.** Every phase (start, dedup decision, gate decision, terminal
   outcome) appends to a single flat file `state/ledger/cycles.jsonl`
   (`nanobot/runtime/cycle_ledger.py`, #720 — the minimal form of #704),
   rotated daily to `cycles-YYYY-MM-DD.jsonl.gz`.
6. **Scorecard (fitness).** `nanobot/runtime/scorecard.py` (#765) computes a
   deterministic, LLM-free 7-day snapshot — loop/cost/quality/value/heldout/
   integrity metrics — to `state/scorecard/latest.json` + `history.jsonl`,
   30-minute watermarked. It reads across ledger rotation. Post-integration
   consumption is verified by harness-observed usage evidence only
   (`nanobot/runtime/usage_evidence.py`, #761) — never self-reported claims.
7. **Demand feeds the next proposal.** `nanobot/runtime/demand.py` (#760)
   deterministically scans state and yields demand items in trust order:
   **priority** > **defect** > **goal-gap** > **hypothesis** > **decay**
   (goal-gap from scorecard target breaches #765; defect includes held-out
   check failures #780). This demand is what step 1 presents to the LLM next
   cycle — closing the loop.

Held-out verification (#780, `nanobot/runtime/heldout/`) runs product-side
sandboxed behavioral checkers against instance artifacts on the scorecard
recompute path; failures become `defect` demand carrying the checker's evidence
without the instance ever seeing the checker.

## Host layout & deploy

- Release symlink: `/opt/eeepc-agent/runtimes/self-evolving-agent/current` →
  the active timestamped release under `.../releases/`.
- State root: `/var/lib/eeepc-agent/self-evolving-agent/state` (goal_text,
  ledgers, scorecard, sidecars, telemetry).
- Deploy path (`host/eeepc/scripts/deploy_release.sh`): `git archive` HEAD →
  `scp` to host → extract into `releases/` → flip the `current` symlink. No
  restart is issued — since #601 the bridge unit runs
  `-m nanobot.runtime.bridge` straight from the release (no file copy), so it
  picks up the new release at its next timer firing
  (`eeepc-self-evolving-subagent-bridge.timer`). See
  `docs/specs/host-runtime/spec.md`.

## Recent hardening (2026-08)

- **#798/#799** — dedup cascade: skips are not failures; cross-target precision
  in the recent-failure/dedup path.
- **#800/#801/#802** — decay-farming guards: a decay eligibility gate, a
  churn split in the scorecard, a double-dip block, and the birth-use rule
  (birth-use does not count toward decay eligibility).

## Read next (depth)

- `docs/changes/702-ledger-loop-architecture-decision/decision.md` — why the
  state-light loop replaced the control-plane state machine.
- `docs/changes/704-ledger-artifact-memory/design.md` — the ledger/artifact
  memory schema (the loop's only durable state).
- `docs/changes/760-demand-driven-proposer/proposal.md` — the supply→demand
  inversion and demand kinds.
- `docs/changes/765-scorecard/proposal.md` — the fitness function and goal-gap
  demand.
- `docs/changes/780-heldout-pack/proposal.md` — the private held-out
  evaluation split.
- `docs/specs/self-evolving-runtime/spec.md`, `docs/specs/subagent-bridge/spec.md`,
  `docs/specs/host-runtime/spec.md`, `docs/specs/promotion-and-release/spec.md`
  — the normative capability specs.
