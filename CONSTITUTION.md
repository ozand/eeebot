# CONSTITUTION

The principles that govern how **we develop the eeebot product**. This is the
top-level "why and guardrails" layer; the operational "how" lives in
[`AGENTS.md`](AGENTS.md), and the current product truth lives in
[`docs/specs/`](docs/specs/).

> Scope note: this document is about **our engineering process** — how the
> operator and AI development sessions build, document, and ship eeebot. It is
> *not* the operating contract for the autonomous runtime that runs on the eeepc
> host; that product behavior is described under `docs/specs/`.

## Mission

eeebot is a resource-aware, self-evolving agent system designed to live on weak,
real hardware (the `eeepc` host) and improve itself under controlled, reviewable
conditions. We are not building the biggest autonomous agent — we are building
the most effective agent we can on constrained hardware, using disciplined
engineering, bounded autonomy, and continuous self-improvement, without ever
losing operator oversight, evidence, or recovery.

## Principles

### 1. Simplicity and transparency
Every part of the system is explained with the smallest accurate model, and every
claim about behavior maps to a durable artifact someone can read. Prefer simple
mechanisms and observable processes over clever-but-opaque ones. If a change
cannot be observed after the fact, it has not earned its place.

### 2. One source of truth per fact
A fact lives in exactly one place. Tasks/status live in GitHub Issues + status labels.
Current product truth lives in `docs/specs/`. Principles live here. We do not keep
a second backlog, a shadow status, or duplicated policy. When two places disagree,
that is a bug to fix, not a fact to reconcile by hand.

### 3. Current truth is separate from changes-in-flight
What is true *now* (a capability spec) is kept apart from a change we are *making*
(a proposal/design). Changes are proposed, reviewed, implemented, then **archived** —
they never accumulate as permanent top-level documents. This is what keeps the
documentation small and honest over time.

### 4. Running behavior is the tie-breaker
When docs and code disagree, follow running behavior and executable config first
(`pyproject.toml`, CI, runtime code, git state), then update the docs deliberately
in the same task. Trust executable sources over prose.

### 5. Complete logical changes, scoped to intent
Prefer one complete logical change over many micro-increments, but keep refactors
scoped to the task. Avoid opportunistic rename churn or cleanup outside task scope.

### 6. Isolation and reviewability
One task = one branch. Never work directly on `main`. Keep changes small enough to
review, with unrelated edits isolated. Hard-to-reverse or outward-facing actions
are confirmed before they happen.

### 7. Canonical repo is the durable source of truth
`ozand/eeebot` holds the durable product code. Work is not "done" if it lives only
in a sibling/staging repo.

### 8. Evidence and recovery over claims
Report outcomes faithfully — failing tests are reported with their output, skipped
steps are named, and "done" is stated plainly only when verified. Preserve the
ability to roll back and to reconstruct why a change was made.

### 9. Security defaults are preserved
Never commit secrets, tokens, auth state, or runtime files. Preserve the
protections in `SECURITY.md` unless a task explicitly and reviewably changes them.

## RSI maturity ladder

We measure the self-evolving runtime against the four-level recursive
self-improvement framework from weco.ai ("4 Levels of Recursive
Self-Improvement", 2026). The levels:

- **L0 Delegation** — the loop runs end-to-end autonomously (demand → proposal
  → gate → integration → confirmed) but improves the system more slowly than
  operator + dev R&D would.
- **L1 Net Positive** — the instance produces sustained, confirmed-value
  improvements faster than the operator effort invested, within a fixed budget.
- **L2 Ignition** — the system improves its own ability to improve; each
  generation of improver outpaces the last.
- **L3 Inflection** — compounding self-improvement dominates all external input.

**Honest current state: L0.** No higher level is claimed without evidence.

**L1 criteria** (all must hold, verified from durable state, issue #782):
(a) at least one confirmed integration (usage-evidence-confirmed, #761) per day
for 7 consecutive days, sourced from non-operator demand kinds (defect /
goal-gap / decay / hypothesis — NOT `priority`); (b) zero operator interventions
in that window (no manual goal_text seeds, no state surgery); (c) within a
declared LLM token budget per day (the constant lives in
`scripts/loop_metrics_report.py`; the operator owns the number); (d)
`heldout_gap` at target (≤ 0.2) throughout — no test-gaming.

**Standing invariant: L2+ is out of scope by design.** The outer improver
remains the dev loop; the gate, harness, and fitness function stay outside the
instance's mutable surface (#603). We do not build a system that rewrites its
own evaluator. This covers the fitness *input data*, not just the computation
(#789, live reward-hack 2026-07-17): the instance must never write fitness
inputs (the `state/` sidecars the scorecard, confirmed-serves, and held-out
readers consume); harness readers trust only harness-authored signals when
reading them; any foreign write is detected and recorded as an integrity
incident (ledger `phase: "integrity"`), repaired at read, and turned into
defect demand — never silently accepted. All L1 criteria — (d) in particular —
presume untampered inputs: a metric moved by writing its input data instead of
doing real work does not count toward any maturity claim.

## How the layers fit

| Layer | Where | Holds |
|---|---|---|
| Principles | `CONSTITUTION.md` (this file) | why we build, guardrails |
| Operational how | `AGENTS.md` | dev workflow, commands, task-tracking rules |
| Current product truth | `docs/specs/<capability>/spec.md` | what is true now |
| Changes in flight | `docs/changes/<id>/` → `archive/` | proposal/design per change |
| Tasks & status | GitHub Issues + status labels | the backlog (single source of truth) |
| Archived/superseded | git history | `.legacy/` removed 2026-07-05 (#613); recoverable via `git log` |
