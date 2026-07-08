# Shadow experiment results — LLM-proposal arm vs old planner (#706)

See `protocol.md` for method and fidelity caveats — read those caveats before
acting on the numbers below.

## Per-cycle results

| cycle | proposal_title | target_path | novelty(self) | precheck | implemented | files_changed | gate | class | wall_s |
|---|---|---|---|---|---|---|---|---|---|
| C1 | Diagnostic script: detect stale/orphaned git worktrees on constrained host | `scripts/check_stale_worktrees.py` | new | accept | true | 2 | pass | general | ~69 |
| C2 | Unreferenced-scripts hygiene diagnostic | `scripts/find_unreferenced_scripts.py` | new | accept | true | 2 | pass | general | ~90 |
| C3 | Add disk_usage_report.py diagnostic script for the constrained host | `scripts/disk_usage_report.py` | new | accept | true | 2 | pass | general | ~57 |
| C4 | HISTORY.md cadence/staleness diagnostic script | `scripts/history_cadence_check.py` | new | accept | true | 2 | pass | general | ~86 |
| C5 | Add repo disk-footprint diagnostic script (repo_disk_report.py) | `scripts/repo_disk_report.py` | new | accept | true | 2 | pass | general | ~48 |

Notes (not part of the metric table, kept for context):

- C1: read-only, parses `git worktree list --porcelain`; 7 unit tests.
- C2: scans `scripts/` for names unreferenced elsewhere; found 2 real
  unreferenced scripts; 5 unit tests.
- C3: stdlib-only top-level disk usage; tests cover size/sort/errors/CLI-JSON.
- C4: parses `memory/HISTORY.md` dates, reports cadence/staleness, exits 1 if
  stale; 10 tests.
- C5: working-tree vs `.git` size + largest entries; 5 tests.

Aggregates: total subagent tokens 162,650 (~32.5k/cycle, Sonnet 5 — NOT the
host model). Actual wall time ran in parallel (~90s total for all 5 cycles);
the sequential-equivalent is ~70s/cycle (measured), which is preferred over
the higher per-cycle self-estimates (90-240s) that came from each cycle's own
approximation.

## Nine #705 metrics — LLM (shadow) arm

Using the #705 definitions, numerator/denominator and value for each:

1. **genuinely_new_proposal_rate (title-level):** 5/5 = **1.0** — all five
   titles/targets are distinct and none duplicate the ~22-item done-ledger
   proxy.
   - **Semantic-diversity note:** title-level novelty of 1.0 overstates
     independence. C3 (`disk_usage_report`) and C5 (`repo_disk_report`) are
     thematically adjacent disk-footprint diagnostics, and all five proposals
     cluster into the same idiom — "read-only diagnostic script under
     `scripts/`." Semantic diversity across the 5 draws is therefore
     **moderate**, not high. A weaker model (e.g., the host model) could
     converge harder on this idiom than title-level novelty alone would
     suggest — flagged as a risk for the on-host follow-up.
2. **duplicate_rate:** 0/5 = **0.0** (inverse of metric 1 at title level).
3. **precheck_accept_rate:** 5/5 = **1.0**.
4. **precheck_reject_rate (P1/P2/P3 combined):** 0/5 = **0.0** — see caveat 4
   in `protocol.md`: no proposal fell outside the mutable surface, so this
   metric reflects absence of exercise, not a validated reject path.
5. **implementation_rate:** 5/5 = **1.0**.
6. **gate_pass_rate:** 5/5 = **1.0**.
7. **gate_fail_rate:** 0/5 = **0.0** (no gate failures this run).
8. **productive_spawn_rate** (precheck-accept AND implemented AND gate-pass):
   5/5 = **1.0**.
9. **integration_rate:** **not meaningfully computable in a shadow.** Nothing
   was integrated by design (all 5 changes live only in throwaway worktrees,
   uncommitted). Reporting a number here would misrepresent a shadow run as a
   live-loop measurement; this metric requires the on-host follow-up with
   real integration.

`human_intervention`: 0/5 cycles required operator intervention.

## Old-planner (documented) baseline

- genuinely_new_proposal_rate ≈ **0** (collapsed to already-done titles).
- duplicate_rate ≈ **1.0**.
- productive_spawn_rate ≈ **0** (gate never reached).
- human_intervention: high (~7 operator-authored planner fixes:
  #656/#664/#690/#695/#697/#700).
- This is **observed/documented evidence**, not a side-by-side re-run in this
  experiment.

## Side-by-side comparison (headline metrics)

| metric | LLM-shadow arm | old-planner (documented) |
|---|---|---|
| genuinely_new_proposal_rate | 1.0 (title-level; moderate by semantics) | ≈0 |
| duplicate_rate | 0.0 | ≈1.0 |
| productive_spawn_rate | 1.0 (5/5) | ≈0 |
| gate_pass_rate | 1.0 (5/5) | N/A — gate never reached |
| human_intervention | none (0/5) | high (~7 fixes) |
| liveness | HEALTHY | DEAD |

## Liveness watchdog signal

- **LLM-shadow arm: HEALTHY** — window threshold is ≥1 non-duplicate proposal
  AND ≥1 productive spawn in-window; this run produced 5 of each.
- **Old-planner arm: DEAD** — 0 productive spawns over 16+ ticks (documented).

## Gate-fail reason breakdown

- Precheck rejects: 0
- Gate fails: 0
- No-commit: 0
- No failures occurred this run, which also means the failure ledger got **no
  exercise** — the failure-handling/reporting path is untested by this
  experiment.

## Go/no-go recommendation

The architecture bet is **strongly supported on a strong model**: 5/5 novel
(by title), precheck-accepted, implemented, gate-passing, harvestable
`general` spawns, with zero human intervention — directly refuting the old
planner's novelty-collapse / 0-productive-spawn failure mode.

However, two decisive questions remain untested:

- **Host-model fidelity (caveat 1):** this run used Sonnet 5, not the weak
  host model. Whether the host model can sustain this novelty/quality is the
  central risk #706 was raised to test, and it is not answered here.
- **Safety-rejection coverage (caveat 4):** no cycle attempted an
  out-of-surface change, so the P1/P2 reject path has zero exercise in this
  run.

The baseline (old planner) was also not re-run side-by-side; it is documented
evidence only.

**Recommendation: PARTIAL PASS — architecture-validated, NOT sufficient on its
own for a #707 GO.** #707 should stay **BLOCKED** pending a follow-up on-host
shadow run using the real host model, the real instance ledgers, and at least
one deliberately out-of-surface proposal to exercise the safety-rejection
path. This is a recommendation; the operator makes the final call.

## Artifacts note

The 5 implemented changes exist only in throwaway isolated worktrees,
uncommitted, and were never integrated into any branch. No secrets, raw logs,
or prompts are included in this report by design.
