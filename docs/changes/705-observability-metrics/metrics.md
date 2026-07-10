# Metric catalog

- **Issue:** #705 (loop-redesign ticket E)
- **Status:** design/contract — no code changed by this document
- **story_id:** docs/specs/self-evolving-runtime/spec.md

## Conventions

- **Window**: every metric is computed over an explicit time window
  `[t0, t1)`, e.g. "last 24h" or "last N cycles." The report contract
  (`report-spec.md`) parameterizes the window; this catalog does not fix one.
- **Cycle**: one bridge iteration, identified by `cycle_id`, joining across
  the done ledger, failure ledger, and telemetry (#704's join key).
- **Ledger paths** (from #704's design, reused verbatim):
  - Done ledger: `<STATE_DIR>/ledger/done/YYYY-MM-DD.jsonl` — fields `ts`,
    `cycle_id`, `title`, `summary`, `files_changed`, `commit_sha`,
    `general_or_host_local` (`general`\|`host_local`\|`unclassified`),
    `source_artifact`.
  - Failure ledger: `<STATE_DIR>/ledger/failure/YYYY-MM-DD.jsonl` — fields
    `ts`, `cycle_id`, `proposed_title`, `stage`
    (`precheck`\|`gate`\|`no_commit`), `reason`, `target_paths`.
  - Integration ledger = the `rollback` record
    (`{integrated, cycle_branch, main_sha_before, main_sha_after, reason,
    auto_committed}`), surfaced today in `_write_bridge_completed_result`
    (`nanobot/runtime/bridge.py` ~lines 2279-2386) and mirrored into the
    done/failure ledger entries above — metrics below cite whichever of the
    two is the more direct source.
  - Telemetry (#675): `<STATE_DIR>/llm_calls/YYYY-MM-DD.jsonl` — `ts`,
    `model`, `duration_ms`, `prompt_tokens`, `completion_tokens`,
    `total_tokens`, `finish_reason`, `retries`, `cycle_id`, `component`.
  - Prompt dump (#693): `<STATE_DIR>/llm_calls/prompts/YYYY-MM-DD.jsonl` —
    `ts`, `model`, `cycle_id`, `component`, `seq`, `prompt_tokens`,
    `completion_tokens`, `finish_reason`, `messages`, `content`,
    `reasoning_content`.
- **"Total cycles" / "total proposals" in a window**: the set of distinct
  `cycle_id` values appearing in *either* the done ledger or the failure
  ledger within the window (every cycle that reached a terminal outcome
  writes to exactly one of the two, per #704 — precheck P2 "already done"
  skips write once to the failure ledger, never double-counted). This is the
  metric-catalog's canonical denominator population unless a metric states a
  narrower one.
- **Duplicate `cycle_id` across ledgers**: #704's write points guarantee a
  cycle writes to at most one of {done, failure} exactly once; if a
  duplicate `cycle_id` is nonetheless observed in a window (e.g. a replay or
  a ledger-write bug), the report must count the cycle once, keeping the
  chronologically last entry and logging the collision — it must never
  silently double-count numerator or denominator.

---

## 1. `genuinely_new_proposal_rate`

**Definition.** The fraction of proposals in the window that were novel —
i.e. not rejected by precheck P2 as already matching a done-ledger entry.

**Numerator / Denominator.** Numerator = count of distinct `cycle_id` in the
window whose terminal outcome is *not* `stage='precheck'` +
`reason='precheck_duplicate_vs_done_ledger'` in the failure ledger (this
includes done-ledger entries and any other failure-ledger outcome, since
those proposals were novel even if later rejected on other grounds).
Denominator = total proposals in the window (done ledger entries + failure
ledger entries, distinct `cycle_id`, per "Conventions").

**Source.** Failure ledger `reason` field (#704) filtered to
`precheck_duplicate_vs_done_ledger`; done ledger entry count; both joined by
`cycle_id`/`ts` window bounds.

**Edge cases.** Empty window (zero proposals): metric is undefined — report
`null`/`n/a`, not `0` or `1`, and surface `n_window=0` so the report doesn't
imply health or failure from no data. Duplicate `cycle_id`: dedupe per
"Conventions" before counting. Partial cycles (a cycle_id present in
telemetry but with no terminal ledger write yet, e.g. bridge crashed
mid-cycle): excluded from both numerator and denominator — this metric only
counts cycles that reached a terminal outcome.

**Why it matters for #706.** This is the direct measure of the failure mode
that motivated #702: the old planner's proposals collapsed to already-done
titles. A healthy shadow run needs `genuinely_new_proposal_rate` sustained
well above the old planner's baseline (which, per #702 §5, repeatedly
produced runs at or near zero genuinely-new proposals) across the shadow
window — a value that decays toward zero over a multi-day window reproduces
the exact old failure mode in the new path and is a hard no-go signal.

---

## 2. `duplicate_rate`

**Definition.** The fraction of proposals in the window rejected by precheck
P2 as duplicate-vs-done-ledger — i.e. `1 - genuinely_new_proposal_rate`,
reported as its own named metric because it is the specific historical
failure signature #702 documents.

**Numerator / Denominator.** Numerator = count of distinct `cycle_id` in the
failure ledger with `stage='precheck'` and
`reason='precheck_duplicate_vs_done_ledger'`. Denominator = total proposals
in the window (same population as metric 1).

**Source.** Failure ledger `stage`+`reason` fields (#704); P2 precheck
contract (`docs/changes/703-safety-shell-invariants/precheck-contract.md`)
defines the exact reason string and that P2 is a *skip*, not an error,
recorded once.

**Edge cases.** Empty window: undefined, report `n/a` with `n_window=0`, same
as metric 1. Unclassified/malformed failure-ledger rows (missing `reason`):
excluded from the numerator, counted in the denominator, and separately
tallied under `report-spec.md`'s gate-fail reason breakdown as
"unclassified" so they are visible, not silently dropped.

**Why it matters for #706.** This is the *inverse presentation* of metric
1, kept as its own catalog entry because the old planner's failure was
specifically a collapse into this bucket — a monitoring dashboard or alert
threshold set directly on "duplicate rate creeping toward 100%" is a more
direct trip-wire than deriving it from the novelty rate every time. A
sustained high `duplicate_rate` (e.g. >50% over the shadow window) is a
no-go signal on its own, independent of other metrics.

---

## 3. `productive_spawn_rate`

**Definition.** The fraction of spawned subagent cycles that produced a real
commit — i.e. reached an integration attempt (the gate ran) with a
non-empty diff, regardless of whether the gate ultimately passed.

**Numerator / Denominator.** Numerator = count of distinct `cycle_id` in the
window that passed precheck (were spawned) and reached the gate with a
non-empty diff — operationally, any cycle whose outcome is a done-ledger
entry, OR a failure-ledger entry with `stage='gate'` (gate ran and failed on
a real diff), but *not* `stage='no_commit'` (subagent ran, produced no diff)
and *not* `stage='precheck'` (never spawned). Denominator = count of
distinct `cycle_id` that passed precheck and were spawned in the window =
total proposals minus failure-ledger `stage='precheck'` entries.

**Source.** Failure ledger `stage` field distinguishing `precheck` (never
spawned) from `gate`/`no_commit` (spawned); done ledger entries (spawned and
integrated). All three states are populated per #704's write-point mapping
onto `main()`'s existing `_rollback_reason`/`result_status` assignments.

**Edge cases.** Empty window: undefined, `n/a`. Zero spawned cycles
(denominator = 0, i.e. every proposal was precheck-rejected/skipped): metric
is undefined (`n/a`), and this condition should itself surface as a liveness
concern in `report-spec.md`'s watchdog, since it means no subagent turn was
spent on real work at all. A cycle whose subagent produced a diff but the
diff was entirely to a blocked-pattern file (rejected pre-gate as a secret):
counts as `stage='gate'`/reason `blocked_file_present` per #704 — since a
real diff existed, it still counts as "productive" in this metric's narrow
sense (there was work to judge), even though it will fail
`gate_pass_rate`; this deliberate separation is why metrics 3 and 4 are
tracked independently rather than collapsed into one.

**Why it matters for #706.** Distinguishes "the LLM proposes novel work" (metric
1) from "the subagent actually does something, not just contemplates and
gives up" (`no_commit`). A high novelty rate paired with a low productive
spawn rate would indicate the new proposal path escapes duplicate-collapse
but still fails to produce real diffs — a different but equally fatal
failure mode for #706's go/no-go.

---

## 4. `gate_pass_rate`

**Definition.** The fraction of cycles that reached the bounded gate (S1/S8
in `docs/changes/703-safety-shell-invariants/precheck-contract.md`'s
referenced safety-shell spec) and passed it, among cycles that reached the
gate at all.

**Numerator / Denominator.** Numerator = count of distinct `cycle_id` with a
done-ledger entry (done-ledger write only happens when
`rollback.integrated is True`, per #704, which requires the gate to have
passed — S1 green-only integration). Denominator = count of distinct
`cycle_id` that reached the gate = done-ledger entries + failure-ledger
entries with `stage='gate'`.

**Source.** Done ledger presence (gate passed, integration attempted and
succeeded); failure ledger `stage='gate'` (gate ran, integration did not
happen — `rollback.integrated is False`, `reason` e.g. `gate_failed`,
`mutation_surface_violation`, `blocked_file_present`, or a suite-shrink
trip). Both are `main()`'s existing integration-time outcomes per #704's
write-point mapping.

**Edge cases.** Denominator = 0 (no cycle ever reached the gate in the
window — e.g. every proposal was precheck-rejected): undefined, `n/a`;
flag prominently, since it means the safety shell's hard arbiter (S1) never
ran at all in the window, a stronger liveness concern than a low pass rate.
Gate timeout/harness-exception outcomes (S1 fail-safe paths per
`test-coverage-map.md`'s S1 row) are gate failures, not a separate stage —
they land in the denominator and not the numerator, consistent with
"green-only integration... fails safe."

**Why it matters for #706.** This is the direct signal on whether LLM-proposed
tasks are *gate-passable*, i.e. scoped and bounded well enough for the fixed
harness to judge them green. A `productive_spawn_rate` that is healthy but a
`gate_pass_rate` that is low indicates the new proposal path produces real
work that the safety shell correctly rejects — informative for #706's
decision on whether the LLM path needs tighter task-scoping guidance before
graduating, not just whether it clears a raw go/no-go bar.

---

## 5. `integration_rate`

**Definition.** The fraction of cycles that were integrated to `main`
(`rollback.integrated == True`), computed over cycles that reached the
gate — the same denominator as `gate_pass_rate`, because integration and
gate-pass are the same event under S1 (green-only integration): a cycle
that reaches the gate and passes is, by construction, integrated (barring a
push-time race, which #704's design does not treat as a distinct outcome).
This metric is retained as its own catalog entry, not merely an alias of
`gate_pass_rate`, because the report contract also surfaces it against a
*different* denominator (total spawned cycles, see below) to answer a
distinct question: "of all subagent turns spent, how many actually moved
`main`."

**Numerator / Denominator.** Two denominators are both reported, labeled
distinctly:
  - `integration_rate_of_gated` = done-ledger entries / (done-ledger entries
    + failure-ledger `stage='gate'` entries). Numerically identical to
    `gate_pass_rate` under S1; reported for symmetry with the other
    ratios in this window (i.e. so a reader does not have to infer it).
  - `integration_rate_of_spawned` = done-ledger entries / (all cycles that
    passed precheck and were spawned, i.e. `productive_spawn_rate`'s
    denominator). This is the more informative of the two for #706's
    go/no-go, because it also captures `no_commit` outcomes (subagent
    spawned, produced nothing) as a cost against integration, not just gate
    failures.

**Source.** Done ledger entry count (numerator, both forms); failure ledger
`stage='gate'` count (first denominator's second term); failure ledger
`stage in {'gate','no_commit'}` plus done-ledger count (second denominator).

**Edge cases.** Same as `gate_pass_rate` for the gated-denominator form
(undefined at zero-gated-cycles). For the spawned-denominator form: zero
spawned cycles → undefined, `n/a` (same condition flagged under
`productive_spawn_rate`).

**Why it matters for #706.** `integration_rate_of_spawned` is the single
number closest to "how often does a subagent turn spent by the new loop
actually land a change" — the bottom-line productivity metric #706's go/no-go
threshold should anchor on, since it nets out both duplicate-collapse
(already excluded — precheck-rejected cycles are never spawned) and
gate/no-commit attrition in one number.

---

## 6. `protected_surface_rejections`

**Definition.** The count and rate of proposals rejected by precheck P1 for
declaring `target_paths` outside the allowed mutable surface
(`_ALLOWED_PATH_PREFIXES`: `surfaces/`, `scripts/`, `memory/`, `lessons/`,
`docs/`, `tests/`, per
`docs/changes/703-safety-shell-invariants/precheck-contract.md`).

**Numerator / Denominator.** Count = distinct `cycle_id` in the failure
ledger with `stage='precheck'` and
`reason='precheck_mutation_surface_violation'`, over the window. Rate =
that count over total proposals in the window (same denominator as metric
1).

**Source.** Failure ledger `stage`+`reason` fields (#704); `target_paths`
field on the same failure-ledger row (#704's field list) gives the specific
offending path(s) for diagnosis, not just the count.

**Edge cases.** Empty window: count reports as `0` (not `n/a` — an absolute
count over an empty window is legitimately zero, unlike a ratio), rate is
undefined/`n/a` (denominator zero). A proposal whose `target_paths` is
`null` (not carried on the request, per precheck-contract.md's note that
"today's bridge does not carry this field") cannot be P1-checked at all;
such rows are out of scope for this metric until #707 adds the field to the
request schema — flagged as a **#707 dependency**, not approximated here.

**Why it matters for #706.** P1 is a precheck-time cost-saving approximation
of the gate's authoritative S2 mutation-surface check
(`_validate_mutation_surfaces`) — it exists so an obviously-out-of-scope
proposal is rejected before spending a subagent turn. A nonzero,
non-trivial `protected_surface_rejections` rate under the LLM proposal path
indicates the LLM is regularly proposing changes to protected surfaces
(core `nanobot/`, `.github/`, `pyproject.toml`, the bridge itself) — a
signal that the proposal prompt/context needs tighter scoping guidance
before #706 can conclude the path is well-behaved, independent of whether
raw integration throughput otherwise looks healthy.

---

## 7. `cost_per_integrated_change`

**Definition.** Two sub-metrics, both defined over the same window and both
denominated by count of integrated changes (done-ledger entries):

- `token_cost_per_integrated_change` — total LLM tokens consumed across all
  cycles in the window (proposal generation, subagent execution, repair
  turns — anything sharing a `cycle_id` with the window's cycles), divided
  by the number of integrated changes in the window.
- `wall_clock_cost_per_integrated_change` — total wall-clock duration across
  the same cycle set, divided by the number of integrated changes.

**Numerator / Denominator.**
  - Token numerator: sum of telemetry `total_tokens` (#675) across every
    telemetry row whose `cycle_id` belongs to the window's cycle set
    (proposals + spawned subagents + repair turns all share `component`
    values distinguishing them, but all roll up by `cycle_id`).
  - Wall-clock numerator: sum of telemetry `duration_ms` (#675) across the
    same row set, converted to a human unit (seconds/minutes) at report
    time.
  - Denominator (both): count of done-ledger entries in the window.

**Source.** Telemetry `total_tokens`/`duration_ms`/`cycle_id`/`component`
(#675, `nanobot/observability/llm_telemetry.py`, `record_llm_call`), summed
per `cycle_id`, then joined against the done ledger's `cycle_id` set to
attribute cost to the *window's total cycle activity* (not just the
integrated ones) while dividing by the integrated count — this
deliberately charges the cost of rejected/duplicate/failed cycles against
the changes that did land, since that is the real cost of running the loop.

**Edge cases.** Zero integrated changes in the window (denominator = 0):
both sub-metrics are undefined — report `n/a`/`inf`-guarded, explicitly
distinct from a healthy low-cost value, and this condition should itself
be flagged prominently (it overlaps with a `integration_rate` of zero).
Telemetry rows with a `cycle_id` that never appears in either ledger
(e.g. a crashed cycle with no terminal outcome, or a cycle whose window
boundary splits it — telemetry `ts` inside the window but the terminal
ledger write lands just after `t1`): included in the cost numerator (the
tokens/time were genuinely spent) but the window-boundary edge case must
be documented in the report as a source of slight over/under-attribution
at window edges, not silently reconciled.

**Why it matters for #706.** Directly answers "is the LLM-proposal path
affordable on the constrained eeepc host." A path that clears novelty and
integration-rate bars but at 10x the token/wall-clock cost per integrated
change of the current planner is a legitimate no-go on resource-constraint
grounds alone (`CLAUDE.md`'s "constrained host" framing) — this metric
gives #706 the number needed to make that call rather than an
impressionistic sense of "it feels slow."

---

## 8. `harvestable_upstream_ratio`

**Definition.** Of integrated changes in the window, the fraction classified
`general` (i.e. upstream-harvestable per the #672 product-vs-instance
generality filter), as opposed to `host_local` or `unclassified`.

**Numerator / Denominator.** Numerator = count of done-ledger entries with
`general_or_host_local == 'general'`. Denominator = total done-ledger
entries in the window (all integrated changes, regardless of
classification).

**Source.** Done ledger `general_or_host_local` field (#704) — an enum with
default `unclassified` "until a harvest pass or the proposing LLM assigns
one," per #704's design. This is the same field #672's harvest mechanism
consumes to select `commit_sha`-referenced candidates for upstream porting.

**Edge cases.** Zero integrated changes: undefined, `n/a`. `unclassified`
entries are **excluded from the numerator** but **included in the
denominator**, and reported as their own explicit third bucket alongside
the `general`/`host_local` split (i.e. the report shows all three counts,
not just a two-way ratio), because a high `unclassified` share means this
metric is not yet meaningful and should not be silently folded into
`host_local` (which would understate harvestability) or `general` (which
would overstate it). If a harvest pass (#672) has not yet run for the
window, expect most/all entries to read `unclassified` — the metric
catalog does not assume a harvest pass runs synchronously with
integration; that classification timing is a **#707 dependency** (whether
the proposing LLM assigns the tag at integration time or a separate
harvest pass backfills it later is unspecified by #704 and left to the
implementation).

**Why it matters for #706.** This is a secondary, non-blocking signal for
#706 (the core go/no-go is about novelty/productivity/cost/safety, not
upstream harvestability) but it is the metric that connects the loop-redesign
work back to the two-repos product-vs-instance flow (nanobot as installable
product vs. eeebot-self-evolving as one host's instance, per #672) — a
persistently near-zero `general` share across the shadow window would
indicate the new loop trends toward host-local-only value even though
harvestability was one of the original motivations named in #672, worth
noting in #706's writeup even if it does not gate the decision.

---

## 9. `human_intervention_needed`

**Definition.** The rate of cycles/incidents in the window that required
operator intervention outside the loop's own automated recovery — a
stop-guard trip, a stuck concurrency lock requiring manual clearing, or a
rollback/restore failure that left the shared checkout in a state the
bridge's own `_restore_to_main` could not repair.

**Numerator / Denominator.** Numerator = count of distinct incidents in the
window matching any of:
  - a failure-ledger `stage='precheck'` entry with
    `reason='precheck_lock_not_held'` or `reason='precheck_dirty_tree'`
    that **persists across repeated cycles** rather than self-resolving on
    the next cycle attempt (a single transient P3 abort that clears on the
    next cycle is normal S6/R27 behavior, not an intervention — see edge
    cases);
  - a stop-guard trip event (S7,
    `docs/changes/703-safety-shell-invariants/test-coverage-map.md`'s S7
    row — `stop_guards.revision_outcome` reaching its cap) that halted a
    cycle rather than the repair loop completing on its own;
  - any outcome where `_restore_to_main` (R27) itself failed, leaving `HEAD`
    off `main` at the start of a subsequent cycle — a condition the
    existing test suite already flags as a precondition-failure category
    (`TestHeadOnMainPrecondition.test_restore_failure_would_trigger_abort_guard`
    in `test-coverage-map.md`'s S6 row) but does not currently surface as a
    ledger row on its own (see edge cases / dependency below).
  Denominator = total cycles in the window (same population as metric 1's
  denominator).

**Source.** Failure ledger `stage='precheck'` + `reason` fields (#704) for
the lock/dirty-tree signals; stop-guard trip is **not yet a distinct
ledger-recorded event** under #704's current schema — today it is only
visible as a `stage='precheck'`/generic `reason` or as an absence of
further cycles (a stall), not a first-class "intervention" tag.

**Edge cases.** A single transient P3 precheck abort (lock momentarily
contended, tree momentarily dirty from a concurrent operator action) that
resolves on the very next cycle attempt is **not** counted — only a
signal that *persists* (the same `reason` recurring across N consecutive
cycles, N a report parameter) counts as requiring intervention, since the
existing shell already self-heals single-cycle transients (R26/R27) by
design. Zero cycles in the window: undefined, `n/a`. **Known gap, flagged
as a #707/#710 dependency:** #704's failure-ledger schema, as specified,
does not carry an explicit `intervention_required: bool` or a distinct
`reason` value for "stop-guard cap reached" versus a routine gate failure
— this metric's stop-guard and restore-failure components currently have
**no direct ledger field to read**, only indirect proxies (repeated
identical precheck-abort reasons, or a gap in cycle_id sequence numbers
suggesting the bridge halted). Implementing this component faithfully
requires either a new failure-ledger `reason` value (e.g.
`stop_guard_cap_reached`, `restore_to_main_failed`) emitted at the
existing `main()` sites per #704's write-point mapping, or an equivalent
signal — that addition is scoped to **#707** (ledger write points), and
the concrete report computation against it is scoped to **#710**; this
catalog only specifies what the metric *should* measure and names the gap
rather than inventing a proxy that isn't grounded in a real field.

**Why it matters for #706.** The shadow experiment must run unattended for
a meaningful window to produce a trustworthy go/no-go signal; if the
LLM-proposal path requires the operator to manually intervene at a materially
higher rate than the current planner's baseline, that alone is a no-go
regardless of how the other eight metrics read, because the #702 decision's
premise is a loop that sustains novelty *without* another round of manual
control-plane patching.
