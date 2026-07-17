# 780 — Held-out verification pack (product-side, invisible to the instance)

story_id: docs/specs/subagent-bridge/spec.md (R42; R41 amended)
Issue: #780

## Problem

The instance writes its own tests and the integration gate runs exactly
those tests — a textbook reward-hacking surface. SpecBench (weco.ai,
2026-05) measured the visible-vs-held-out gap at up to 100pp on complex
tasks (their C-compiler case: 97% on visible tests via a pre-compiled
lookup table, 0% on held-out). Our live history rhymes: the #712-era
word-heuristic "successes", and the goal-gap era loop integrating code
judged only by tests it authored itself. #761's confirmed-serves chain is a
partial held-out signal but measures *consumption*, not *correctness*.

## Design

**Private half of the evaluation split.** A new product-side package
`nanobot/runtime/heldout/` (same placement rule as the scorecard, #603
invariant): a registry `checkers.CHECKERS` mapping instance-repo-relative
artifact path → behavioral checker, plus a sandboxed runner
`run_heldout(state_dir, selfevo_repo)`.

- **Behavioral checks, not unit tests.** Each checker targets an artifact's
  PUBLIC contract (the goal_text priority that created it) on fixtures
  generated at runtime inside an isolated tmpdir. Lenient on interface
  details, strict on the behavioral core: `archive_old_reports.py` dry-run
  must not modify the fixture tree (+ `--apply` must actually archive);
  `eeebot_dashboard.py` must degrade gracefully on empty state and render
  on a fixture ledger; `generate_system_map.py` must produce a
  `SYSTEM_MAP.md` naming the fixture scripts; `prune_failed_backlog.py` /
  `loop_health_report.py` (loose contracts) get lenient smoke checks.
- **Sandbox.** The script is COPIED into the tmpdir (repo-relative path
  preserved so cwd-relative `state/...` conventions hold) and run via
  `sys.executable`, `cwd=tmpdir`, 30s timeout, env stripped to a minimal
  PATH + tmpdir-only PYTHONPATH/HOME/TMPDIR — no state_dir, no secrets, no
  network assumptions, and the instance repo is never touched.
- **Fail-open.** Checker exception/timeout → `skip` with reason; a skip
  never counts against the instance and never becomes demand; the runner
  never raises into scorecard/demand.
- **Cadence.** HEAD+time watermark (`usage_evidence` pattern: rerun on
  instance HEAD move or 6h) + per-artifact content-hash verdict reuse.
  Invoked from the scorecard recompute path (already 30-min watermarked on
  the host loop) — no new scheduler hook.
- **Scorecard.** New `heldout` snapshot section
  (checked/passed/failed/skipped) + `heldout_gap` = failed/(passed+failed)
  (`None` at denominator 0); `_TARGETS` gains `heldout_gap` ≤ 0.2 (V1,
  direction max) → goal-gap demand on breach.
- **Demand.** Each `fail` result becomes a `defect` item — summary
  `held-out check failed: <artifact>`, evidence = the checker's evidence
  string: the loop is told WHAT is broken without ever seeing the checker.
  Deduped by artifact, bounded to 5.
- **Invisibility.** Nothing under the instance workspace; no reference from
  instance-facing prompt builders or goal_text; no fixture files on disk in
  the package; the deploy script never copies the pack into anything the
  instance reads. Regression-pinned in `tests/test_heldout.py`.

## Files

- `nanobot/runtime/heldout/__init__.py` — runner, watermark, results sidecar.
- `nanobot/runtime/heldout/checkers.py` — `CheckContext`, sandbox env, 5 checkers, `CHECKERS` registry.
- `nanobot/runtime/scorecard.py` — `heldout` section, `heldout_gap` target, recompute-path wiring.
- `nanobot/runtime/demand.py` — `_heldout_defect_items` in `collect_demand`.
- `scripts/loop_metrics_report.py` — heldout metrics in the scorecard block (renderer already tolerates absence).
- `docs/specs/subagent-bridge/spec.md` — new R42; R41 amended (heldout section/target, #603 sentence extended).
- `tests/test_heldout.py` — runner, sandbox, scorecard, demand, invisibility coverage.
