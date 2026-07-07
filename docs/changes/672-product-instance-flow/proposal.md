# 672 — Product↔instance flow: downstream core-sync + upstream harvest

## Context

`ozand/eeebot` (the `nanobot/` package) is the installable **product** we
develop. `eeebot-self-evolving` is **one host's working repo** — the eeepc
instance's own checkout, which runs autonomous cycles and self-evolves
additively (`scripts/`, `state/`, goal artifacts, `lessons/`). Drift between
product and instance is **by design**: the instance is allowed to diverge in
its additive layers between deliberate syncs. The relationship is
product → deploy → instance, not two peer forks.

Two things were already true before this change and are not being revisited
here:

- The eeepc host **engine** (what actually runs the cycle loop) is current
  product, delivered via `host/eeepc/scripts/deploy_release.sh`, which
  archives product HEAD and symlinks it in as `current`. The engine has never
  drifted.
- The instance's own **working checkout** — the `eeebot-self-evolving` repo
  the autonomous loop commits into and runs its own test suite against — is a
  stale fork. It branched pre-simplification and never caught up.

#672's discovery step (see issue comments) measured this fork precisely:
Bucket B (instance-originated innovation inside `nanobot/` core) is **empty**
— every differing core file traces to a specific product removal or refactor
commit (coordinator split 4,509→1,034 lines, dead channels/providers
trimmed, `hermes_pi_qwen`/`ayga.tech` hardcodes removed, `tool_harness` /
`stop_guards` / bridge / archive / scorer / probes added, pre-#619
`from eeebot.*` imports). Two narrow, general improvements in `scripts/`
(`cleanup_subagent_queue.py`, `cycle_logger.py`) were the only real
instance-originated value, and those were already harvested and merged as
PR #673.

That measurement changes the shape of the problem: there is nothing to
merge, only a stale core to replace. But the instance is also live and
working — it just achieved its first autonomous integration behind the #678
mutation-surface hardening. Replacing `nanobot/` + `tests/` under a running
system is a real operation with real failure modes, not a config edit.

## Why now

- The harvest direction already paid off once (#673): proof the
  instance→product flow has value and a working shape.
- The fork-porting friction (product fixes not reaching the instance's own
  gate until manually ported) is real today, but it is *shrinking* as
  self-evolving dev pace on the core stabilizes — most new work now lands in
  the additive surfaces (`scripts/`, `surfaces/`, `memory/`, `lessons/`,
  `docs/`, `tests/`) that #678 already restricts subagents to.
- #678 changed the risk profile for a future re-seed: core `nanobot/` is no
  longer a subagent-writable mutation surface
  (`_ALLOWED_PATH_PREFIXES = ('surfaces/', 'scripts/', 'memory/', 'lessons/',
  'docs/', 'tests/')` in `nanobot/runtime/bridge.py`
  `_validate_mutation_surfaces`), so a re-seed today would not immediately
  re-diverge the way an unprotected core would have.

This document designs both flows so future execution (the re-seed itself,
and each harvest pass) has a written procedure to follow, without deciding
the re-seed's timing — that decision belongs to the operator (see
`design.md`).

## Goals

- Design a precise, low-risk procedure for a **downstream core-sync**
  (product → instance): what gets replaced, what is preserved, the risks
  specific to this instance, and a rollback path.
- Design a repeatable, product-simple **upstream harvest** mechanism
  (instance → product) that generalizes the manual pass already done for
  #673, without building unneeded automation up front.
- Give the operator the information needed to decide whether to execute the
  one-time re-seed now or defer it.

## Non-goals

- Not collapsing `ozand/eeebot` and `eeebot-self-evolving` into one repo —
  they remain two repos with a deploy relationship between them.
- Not executing the core-sync migration in this document — this is the
  design; execution is separate follow-up work (its own PR/branch/gate run),
  gated on the operator decision this doc surfaces.
- Not changing how the instance self-evolves day to day — it keeps adding
  to `scripts/`, `state/`, goal artifacts, and `lessons/` additively, exactly
  as the #678 protect-list already allows.
- Not building a harvest-scoring subagent (v2) up front — v1 stays manual,
  matching the #673 precedent, until manual passes prove too slow or too
  frequent to be worth doing by hand.

## Sequencing / preconditions

1. This design merges to `docs/changes/672-product-instance-flow/` first
   (this PR).
2. Flow 1 (core-sync) execution is a **separate** task: create its own
   `docs/changes/<id>/proposal.md` referencing this design, only after the
   operator decides to proceed (see "Decision needed" in `design.md`).
3. Flow 2 (harvest) can start on its existing manual cadence immediately —
   it needs no new tooling, only the checklist in `design.md` §Flow 2.
4. Neither flow touches `docs/specs/*` yet — specs update when a flow's
   *execution* PR lands, per the standard change-folder lifecycle
   (`docs/changes/README.md`).
