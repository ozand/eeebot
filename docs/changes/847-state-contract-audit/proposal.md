# Change: Export/reload state-contract audit (cycle-boundary consistency)

- **change-id:** state-contract-audit
- **issue:** #847
- **capability:** docs/specs/self-evolving-runtime (cycle boundaries / persistence)
- **role / workstream:** role:developer / workstream:trust-safety

## Problem

a-evolve treats "agent runtime (in-process) state silently diverging from the
persisted ledger/state-dir" as THE main persistence risk, and mitigates it
structurally (forced export-to-fs before observation, reload-from-fs after
mutation). evoagentx/anima suffer split-truth. This is a one-time audit to
verify our loop guarantees cycle-boundary consistency between in-process state
and the ledger/state dir rather than assuming in-process survival — and to
close any gap found.

## Audit verdict

**The loop already satisfies the export/reload contract. No trust-relevant gap
found.** This change is documentation of the verified contract plus a
regression test pinning its subtlest seam — there is no runtime fix, because
forcing one where the discipline already holds would be overengineering.

### Cross-cycle: structurally safe (fresh process per cycle)

Both loops are single-shot `main()` entrypoints with no in-process scheduler:

- Coordinator: `app/main.py` `def main()` → `asyncio.run(run_self_evolving_cycle(...))`
  once, then exits (the load-bearing, unit-independent proof — `main()` has no
  loop and returns after one cycle). The recurring per-cycle execution is driven
  by `eeepc-self-evolving-agent-health.timer` → `eeepc-self-evolving-agent-health.service`
  (`Type=oneshot`, `-m app.main`); the base `eeepc-self-evolving-agent.service`
  is only a `Type=simple` post-deploy single-shot kick ("do NOT enable"), not the
  recurring driver.
- Bridge: `nanobot/runtime/bridge.py` `cli_main()` → `asyncio.run(main())`; also
  `Type=oneshot` (`eeepc-self-evolving-subagent-bridge.service`,
  `-m nanobot.runtime.bridge`). The module comment is explicit
  (`bridge.py:~1381`): "*This process runs once per cycle … and exits
  afterward … it never outlives this process.*"

All state entering a cycle is read from disk at the top (coordinator:
`goals/current.json`; bridge: `outbox/report.index.json`, `goals/registry.json`;
demand/scorecard/usage sidecars via plain `read_text`/`json.loads`, no
module-level cache). Nothing is held in a class instance or module global
between invocations, so **cross-cycle divergence is structurally impossible —
there is no process to hold stale state.**

### Within-cycle: every read/mutate/decide seam already re-derives

Each seam where state is read early, mutated, then used late already re-reads
from disk/git rather than trusting an in-memory copy — and each cites the
incident that produced the discipline:

| Seam | Re-derive discipline | Origin |
|---|---|---|
| `origin/main` before the gate decision | `_detect_out_of_band_main` / `_safe_rev_parse` re-fetch + re-check right before integrate | #846 |
| fitness sidecars across the spawn window | `_fitness_sidecar_hashes` hashed pre-spawn and re-hashed post-spawn before the gate verdict | #789 |
| changed files / mutation-surface violations | `_changed_files_and_violations` recomputed after every repair turn and again right before the gate | #678 F1/F3 |
| concrete-change probe | `_has_concrete_changes` is a live `git log` subprocess, never cached, called 3× per coordinator cycle | #565 |
| reward → outcome/stall/stop_reason | re-derived after the materialize-artifact reward upgrade, never taken from the pre-upgrade value | #581 |
| `demand.collect_demand` completed/exhausted suppression | the completed-sidecar fold runs after all batches are collected and a post-fold filter is a second-pass safety net; each call re-folds from the current ledger (no cross-call cache) | #773/#778 |

### Two intentional staleness windows (named so they are not mistaken for gaps)

1. `scorecard.compute_scorecard` — 30-minute time watermark.
2. `usage_evidence.refresh_usage` — HEAD+6h watermark.

Both are **benign, not trust-relevant**: they only feed `goal-gap`/`decay`
*demand suggestions* that a subsequent bridge cycle must still independently
propose, gate, and integrate. They never touch `confirm_serves`'s per-call
re-derivation (which re-checks the current sidecar every call and tamper-repairs
foreign signals), never touch the gate's smoke/mutation-surface decision, and a
stale-favorable read only delays a correct demand item by up to the window — it
cannot manufacture unearned fitness or bypass a gate. Both are documented as
intentional ("idle cycles stay cheap"), not silent.

## Intended change

- This audit document (the verified export/reload contract + the two named
  staleness windows).
- A regression test pinning the subtlest seam — `demand.collect_demand`
  re-reads `completed.json` on every call — so a future refactor that
  introduces an in-memory cache of the completed/exhausted set (silently
  re-creating split-truth) is caught.

No runtime code changes: the contract already holds.

## Acceptance

- [x] cycle-start state is re-derived/verified from disk; no silent in-process
  carryover of trust-relevant state (verified above — fresh-process per cycle +
  per-seam re-derive)
- [x] test pinning the contract at its subtlest seam; deployed

## Out of scope

- Adding an explicit `export_to_fs`/`reload_from_fs` framework — the loop is
  already stateless-per-cycle by construction; a framework would be
  overengineering with no gap to close.
- Tightening the two intentional staleness windows (they are benign and
  deliberately cheap).
