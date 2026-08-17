# Change: drop the obsolete archive_old_reports held-out contract

- **change-id:** 887-drop-stale-archive-checker
- **issue:** #887 (follow-up to #864, #798/#799/#800/#801/#802, #875, #876)
- **capability:** docs/specs/subagent-bridge (held-out verification pack, #780)
- **role / workstream:** role:developer / workstream:runtime-quality

## Problem

The held-out pack (`nanobot/runtime/heldout/checkers.py`) contracts
`scripts/archive_old_reports.py` via `check_archive_old_reports`: it
requires a loop-authored script that moves `state/reports/*.json` older
than 30 days into monthly tar.gz archives, with a strict dry-run-is-
side-effect-free core.

That script's job is now redundant. #864 added product-code pruning —
`coordinator.py`'s `_prune_stale_reports` with `REPORTS_RETENTION_KEEP=200`
— which bounds `evolution-*.json` report-file bloat every cycle,
unconditionally, without any loop-authored script. The decay system
(#798–#802) correctly identified `archive_old_reports.py` as unused
(nothing in the loop's own product surface calls it, and its function is
superseded) and retired it. But the held-out pack still contracts the
retired script's *path*, so the disabled instance copy keeps failing
`check_archive_old_reports` — that keeps the `heldout` scorecard section
RED and the `heldout_gap` metric over its V1 target, which blocks the
global promote gate (#875/#876) from ever going GREEN.

This is a genuine conflict, not a bug in either subsystem: a script is
either (a) still contracted by the held-out pack and therefore decay-
protected (per #884's `_heldout_contracted_paths()`), or (b) legitimately
retired because a superseding mechanism exists. It cannot be both
"decay says drop it" and "held-out says keep it" at once. Since (b) is
true here — #864 supersedes the script's entire purpose — the held-out
contract is the stale side of the conflict and must be dropped.

## Intended change

Remove the `check_archive_old_reports` checker and its
`"scripts/archive_old_reports.py"` entry from the `CHECKERS` registry in
`nanobot/runtime/heldout/checkers.py`. The other four checkers
(`eeebot_dashboard`, `generate_system_map`, `prune_failed_backlog`,
`loop_health_report`) and the `run_heldout` engine (result shape,
regression detection, flaky-run confirmation, defect-demand emission,
invisibility) are untouched.

`tests/test_heldout.py`'s engine-level coverage that previously used
`archive_old_reports.py` as its example fixture (good/bad pass-fail pairs,
regression flip detection, sandbox isolation) is repointed to use the
still-registered `eeebot_dashboard.py` (`GOOD_DASHBOARD` /
`CRASHING_DASHBOARD`) and `generate_system_map.py`
(`GOOD_SYSTEM_MAP` / new `BAD_SYSTEM_MAP`, a subtle-behavioral-defect
fixture that exits 0 but omits the required per-script entries) fixtures.
No engine assertion (result shape, regression list semantics, flaky
exclusion, defect-demand evidence, invisibility) is weakened — only the
example checker used to exercise them changes.

`tests/test_usage_evidence.py`'s #884 tests
(`test_heldout_contracted_script_never_flagged`,
`test_heldout_contracted_paths_helper_fail_open`) are updated to reference
a still-registered contracted script (`scripts/eeebot_dashboard.py`)
instead of the removed one, so they keep proving: (a) a `CHECKERS`-
registry script is never decay-flagged, and (b) the contracted-paths
helper returns the live registry set.

`tests/test_goal_text_priority_filter.py`'s references to
`archive_old_reports.py` are narrative goal-text priority strings
unrelated to the checker registry or decay behavior — left unchanged.

This durably restores held-out `GREEN` (the fix lives in product code
under `heldout/`, a runtime-deny-set package the loop cannot modify) and
unblocks organic promotion under #875/#876.

## Acceptance

- [x] `check_archive_old_reports` and its registry entry are removed from
      `nanobot/runtime/heldout/checkers.py`; the other four checkers are
      untouched.
- [x] `tests/test_heldout.py` passes, still exercising: results shape,
      good→pass, bad→fail (including a non-crash subtle-defect fail
      path), missing-artifact skip, timeout→skip, content-hash reuse,
      regression pass→fail / still-failing / new-only-failure / pass→pass
      semantics, flaky-run detection, sandbox isolation, scorecard
      integration, defect-demand emission, and the invisibility invariant
      — via the `eeebot_dashboard.py` / `generate_system_map.py` fixtures
      instead of the removed checker.
- [x] `tests/test_usage_evidence.py` passes, still proving a
      `CHECKERS`-registry script is never decay-flagged and that
      `_heldout_contracted_paths()` returns the live registry set.
- [x] `tests/test_goal_text_priority_filter.py` is unchanged (its
      `archive_old_reports.py` mentions are narrative goal-text content,
      not registry/decay assertions).
- [x] Full test suite shows no new failures beyond pre-existing
      Windows-only / site-packages-shadow collection issues.
- [x] `heldout/` remains in `runtime_deny` (no change to the deny-set).

## Out of scope

- No changes to `coordinator.py`'s `_prune_stale_reports` /
  `REPORTS_RETENTION_KEEP` (#864) — this change only removes the now-
  redundant held-out contract on the loop-authored equivalent.
- No changes to the other four checkers or the `run_heldout` engine's
  regression/flaky/defect-demand logic.
- No changes to `runtime_deny`, decay eligibility logic (#798–#802), or
  the #875/#876 promote-gate mechanics themselves — this change only
  removes the stale RED signal blocking them.
