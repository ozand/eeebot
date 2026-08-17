# Change: closed hypothesis -> experiment -> verdict scientific loop (RSI stage 4)

- **change-id:** 878-hypothesis-loop
- **issue:** #878
- **capability:** self-evolving-runtime (demand collection #760, hypothesis
  backlog #751, LLM proposer novelty guards #707/#716/#834, goal-review #768,
  scorecard control-plane #865, held-out microbench #822, usage/value
  evidence #761/#773, fitness-sidecar integrity #789)
- **role / workstream:** RSI (recursive self-improvement) — closing an
  existing loop, not building a new one

## Strict product simplicity (by design constraint of this change)

No new demand lane, no report generator, no new daemon, no new state
machine. This is a thin closing layer over machinery that already exists:
hypotheses already become experiment demand, the proposer already picks
them up, a cycle running IS the experiment. What was missing was the other
half of the scientific method — a VERDICT on whether the experiment
confirmed the hypothesis, fed back into (a) never re-proposing a disproven
idea and (b) surfacing a proven one as a priority candidate.

## Problem

`hypothesis_backlog.reconcile` already answers WHETHER a hypothesis's
serving cycle integrated (`lifecycle.json` entry flips `status: "active"` ->
`"answered"` on a same-cycle `proposed` -> `outcome: success` ledger pair).
It never asked whether the hypothesis was actually TRUE. A hypothesis whose
experiment cycle "succeeded" (code merged) and one that later turns out
worthless look byte-identical in the lifecycle sidecar — the loop has no way
to (a) stop re-proposing a title it already spent a cycle disproving, or (b)
elevate a title it already spent a cycle proving into a follow-up priority.
The loop was running experiments with no read-out.

## Design

### 1. Verdict classification — `nanobot/runtime/hypothesis_verdict.py` (new)

`classify_hypothesis_verdict(state_dir, cycle_id, acceptance_text="") ->
(verdict, evidence)`, `verdict` in `{"supported", "refuted",
"inconclusive"}`. Tries two MEASURED, harness-trusted sources, first match
wins:

1. **microbench** (`heldout.microbench.load_microbench_entry`, #822) — a
   causal, isolated, harness-run before/after measurement. `improvement_pct
   >= 5.0` (`MICROBENCH_SUPPORTED_THRESHOLD_PCT`) -> `supported`; otherwise
   -> `refuted`.
2. **confirmed usage** (`demand/completed.json`, #773/#761) — the cycle's
   own completed entry (matched by `cycle_id`) touched a `scripts/`
   artifact: `confirmed is True` (set ONLY by
   `usage_evidence.confirm_serves` from an independently-observed usage
   signal) -> `supported`; still unconfirmed after `CONFIRM_WINDOW_DAYS`
   (14, matching the existing decay-window convention used by
   `demand`/`scorecard`/`goal_review`) since completion -> `refuted` (had a
   fair window, wasn't used); still within the window -> falls through.
3. **none** — no measured signal at all -> `inconclusive`.

`acceptance_text` is accepted for interface symmetry but is NEVER read for
the classification — the whole point is that a number the instance merely
CLAIMS in a hypothesis's own acceptance/evidence text must never move the
verdict (regression-pinned in `tests/test_hypothesis_verdict.py::TestTrustBoundary`).

`hypothesis_backlog.reconcile` calls this the SAME pass a candidate is
first marked `answered` (no separate hook), persisting `verdict` /
`verdict_evidence` / `verdict_at` onto the same lifecycle entry (additive —
`status`/`answered_evidence`/`answered_at` are untouched) and appending one
`{"phase": "hypothesis", "reason": "verdict", "hypothesis_ref", "verdict",
"cycle_id", "evidence"}` ledger row via `cycle_ledger.append_event`.

### 2. Refuted -> permanent novelty guard — `llm_proposer.py`

`_refuted_hypothesis_titles(state_dir)` reads `hypotheses/lifecycle.json`
directly for entries with `verdict == "refuted"`, returning their titles.
Wired into `_is_duplicate_proposal` as a THIRD source, checked
unconditionally (not gated on `_proposal_creates_new_file` like #834's
guard, since a hypothesis experiment need not have created a new file) and
PERMANENT — like #834's full-history built-subject guard, not windowed like
#716's `_recent_failed_titles`. `matched_against` is recorded as
`"refuted-hypothesis:<title>"` so a `proposer_reject` ledger row can
distinguish this source from the git-log/ledger-title sources at a glance
while still keeping the matched text for debugging. Fail-open: a
missing/corrupt `lifecycle.json` never blocks a proposal.

### 3. Supported -> goal-review evidence candidate

`hypothesis_backlog.supported_hypotheses(state_dir, n=3)` returns the
newest-`verdict_at`-first, top-3 `verdict == "supported"` entries as
`{title, evidence}` (evidence is the SAME `verdict_evidence` dict persisted
on the lifecycle entry — never instance-authored text). Wired into
`goal_review._collect_evidence` as an additional citable evidence line,
exactly the way decay/goal-gap evidence already is — the smallest correct
integration point (the task's own fallback suggestion), not a separate mint
path. A supported hypothesis still has to be CITED by id and pass
`goal_review.validate_priority`'s existing fail-closed checks (label
shape, body bound, `V1`/`V2` vector, evidence-in-inputs, dedup) before it
can ever become a `derived_priorities.json` entry — no new validation was
added or bypassed.

### 4. At most one active hypothesis experiment — `demand.py`

`hypothesis_backlog.has_in_flight_experiment(state_dir)`: true iff some
`active`-status candidate has a `'proposed'` ledger row (`serves:
hypothesis <ref>`) whose `cycle_id` has NOT YET produced a terminal
`'outcome'` row. `demand._hypothesis_items` applies exactly this rule:

- If an experiment is in flight -> mint ZERO hypothesis-kind items this
  pass (don't stack a second experiment on the running one).
- Otherwise -> cap the (possibly multi-candidate) result to the first ONE
  item, so the proposer never sees more than one hypothesis candidate per
  cycle either.

This is the whole rule — no new lifecycle status, no new state file.

### 5. Scorecard visibility — `control_plane.hypothesis_loop`

`hypothesis_backlog.lifecycle_counts(state_dir)` returns `{active,
answered, supported, refuted, inconclusive}` counts over every lifecycle
entry (`status` and `verdict` are independent fields on the same entry, so
an answered+supported entry counts toward both `answered` and `supported`).
Wired into `scorecard._control_plane_snapshot` as `hypothesis_loop`, same
leaf-dependency/fail-open-to-`{}` shape as the existing `evolution_tree`
key (#877). Visibility only — never fed into `_TARGETS`/gaps.

## Trust boundary (read before touching `hypothesis_verdict.py`)

`hypotheses/lifecycle.json`, `hypotheses/backlog.json` and
`research/hypotheses.json` are INSTANCE-writable state — nothing in them is
ever trusted for the verdict number itself. The verdict is computed
EXCLUSIVELY from two `scorecard.FITNESS_SIDECARS` members (#789
spawn-boundary tamper detection already protects both):
`heldout/microbench.json` (#822 — a sandboxed, harness-run causal
measurement) and `demand/completed.json` (#773/#761 — whose `confirmed`
field is written ONLY by `usage_evidence.confirm_serves` from an
independently observed usage signal, never a claim).

A verdict is STEERING, never a verification gate, symmetric with the
#819/#822 "only allowlisted sidecars are trusted" design:

- `refuted` only blocks a future re-proposal of the same title — at worst a
  wasted retry the proposer would have self-dedup-rejected anyway once it
  re-hit the theme.
- `supported` only injects a citable evidence line into goal-review's
  input — the resulting candidate still has to pass
  `validate_priority`'s full fail-closed checks and, if accepted, the
  entire cycle gate (smoke/deny-set/held-out) before it can integrate.

A forged/tampered sidecar therefore costs at worst churn (a spurious
"refuted" wastes a proposal retry) or a rejected priority candidate — it
can never fabricate an integration by itself. `hypothesis_verdict.py` is
added to `runtime_deny._RUNTIME_DENY_ALWAYS_FILES` (explicit entry, no
basename-token match applies) as cheap hardening, matching
`benchmark_evidence.py`/`usage_evidence.py`'s existing deny-set membership —
it is fitness-adjacent steering logic even though `lifecycle.json` itself
(pure data) is not.

## Files changed

- `nanobot/runtime/hypothesis_verdict.py` (new) — verdict classification
- `nanobot/runtime/hypothesis_backlog.py` — verdict computed on answer;
  `supported_hypotheses`, `lifecycle_counts`, `has_in_flight_experiment`
- `nanobot/runtime/llm_proposer.py` — `_refuted_hypothesis_titles` +
  `_is_duplicate_proposal` wiring
- `nanobot/runtime/demand.py` — `_hypothesis_items` <=1-active-experiment cap
- `nanobot/runtime/goal_review.py` — `_collect_evidence` supported-hypothesis line
- `nanobot/runtime/scorecard.py` — `control_plane.hypothesis_loop`
- `nanobot/runtime/runtime_deny.py` — deny-set entry for `hypothesis_verdict.py`
- `tests/test_hypothesis_verdict.py` (new), plus extensions to
  `tests/test_hypothesis_backlog.py`, `tests/test_llm_proposer.py`,
  `tests/test_demand.py`, `tests/test_goal_review.py`, `tests/test_scorecard.py`

## Test results

See the PR description for the verbatim final pytest lines (touched files
run explicitly by path, plus the full suite with
`--continue-on-collection-errors`; a pre-existing local site-packages
`tests` package shadow hides `test_llm_proposer.py`/`test_demand.py` from
default collection on this dev machine — worked around for verification by
pre-seeding `sys.modules["tests"]`, not committed to the repo).
