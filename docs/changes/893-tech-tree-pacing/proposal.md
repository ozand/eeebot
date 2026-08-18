# Change: tech-tree gains paced by integrations, not scorecard recompute ticks

- **change-id:** 893-tech-tree-pacing
- **issue:** #893 (+ #894 one-liner rider, same PR)
- **capability:** self-evolving-runtime (tech-tree #879, hypothesis loop
  #878, scorecard #765/#789/#865)
- **role / workstream:** RSI (recursive self-improvement) — pacing fix to
  an existing ranking input, no new machinery

## Live finding

`tech_tree.record_gains` (`nanobot/runtime/tech_tree.py`) is invoked from
`scorecard.compute_scorecard`'s recompute path — a timer-driven tick, observed
firing ~27 times over a ~14h window on the live instance. Its lever metrics
(`loop.repeat_failure_rate`, `cost.tokens_per_integration`,
`loop.confirmed_integration_ratio`, `heldout.heldout_gap`,
`quality.compile_clean_ratio`) are all 7-day rolling aggregates computed over
the cycle ledger — over that same ~14h window, the loop only recorded ~5
actual integrations, so the underlying aggregates barely moved between
consecutive recomputes.

Recording one gain observation per recompute regardless of whether any real
work had landed meant each node accumulated a full `GAIN_HISTORY_MAX=8`
window of near-zero-delta observations in a matter of hours — with zero
cycles' worth of real integration progress behind most of them — and
`is_plateaued` correctly (given the data it was fed) called every node
plateaued. All five seed nodes plateaued overnight; `current` read `None`
because `select_current_direction` found no eligible (non-plateaued,
non-cooldown) node left to pick.

This is not a bug in the plateau/selection math — it is a pacing bug:
`record_gains` was measuring *ticks*, not *progress*.

## Fix

`record_gains(state_dir, scorecard_result)` now gates on the harness's own
cycle-progress counter, `loop.integrations` (read via the existing
`_dotted_get` dotted-path helper, same as every lever lookup):

- **Absent / non-numeric** `loop.integrations` → fail open: record nothing
  this call. This never falls back to the old per-tick behavior — a missing
  counter is treated as "no signal," not "assume progress."
- **Present but not advanced** past the portfolio's own `last_integrations`
  watermark (a new single top-level field on the portfolio sidecar — global,
  not per-node, since it paces every node identically) → record nothing this
  call: no gain observation and no `last_lever_value` update, for any node.
- **Present and advanced** → proceed exactly as before: each node's lever
  value is read from the scorecard result, compared to its own prior
  `last_lever_value`, and one signed gain observation is appended (bounded to
  `GAIN_HISTORY_MAX`); `last_integrations` is then advanced to this call's
  value.
- **First-ever observation** (no `last_integrations` watermark yet) sets the
  baseline — `last_integrations` and each node's `last_lever_value` — but
  appends no gain observations, matching the pre-existing per-node
  first-observation convention.

No change to `PLATEAU_FLOOR`, `GAIN_HISTORY_MAX` (K), `COOLDOWN_HOURS`, or the
epsilon-greedy selection logic in `select_current_direction` — this is a
pacing fix to the gain-recording input those consume, not a change to how
they interpret it. With this fix, a plateau verdict now means "K
observations, each backed by real integration progress, showed no
improvement" rather than "K scorecard-recompute ticks fired, most with
nothing to measure."

## #894 rider (same PR) — hypothesis re-eval misses legacy no-verdict entries

`hypothesis_backlog.reconcile`'s re-evaluation branch for already-`answered`
entries checked `entry.get("verdict") == "inconclusive"`. Any `answered`
lifecycle entry that predates #878 (the verdict feature) has no `verdict`
key at all — `None`, not the string `"inconclusive"` — so those legacy
entries were silently excluded from ever getting a first verdict computed.
Changed the condition to `entry.get("verdict") in (None, "inconclusive")`.
The existing guard (`answered_evidence` / serving `cycle_id` required, else
skip quietly) is unchanged — an answered entry with neither a verdict nor a
recorded serving cycle is left alone rather than raising.

## Rollout note (operator does this by hand after merge, not part of this PR)

The live portfolio's `gain_history`/plateau state was built entirely under
the broken per-tick pacing and is meaningless. On deploy, the operator will
reset the live `tech_tree/portfolio.json`: every node back to `status:
"active"` with cooldown cleared and `gain_history` cleared, and the new
`last_integrations` field set to `null` so the very next scorecard recompute
is treated as the first-ever (baseline-only) observation under the corrected
pacing. No script for this is included in this PR — it's a one-time manual
state heal on the live host, not machinery this change should ship.
