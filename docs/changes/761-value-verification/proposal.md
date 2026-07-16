# Change: post-integration value verification — usage evidence, confirmed-serves, decay demand (#761)

- **change-id:** 761-value-verification
- **issue:** #761
- **capability:** `docs/specs/subagent-bridge` (adds R40; consumes R39's
  completed sidecar from #773)
- **Depends on:** #773 (completed sidecar — the ledger this confirms),
  #760 (demand kinds — decay is a new one), #751 (`serves` — the claim this
  verifies), #749 (HEAD+time watermark pattern).
- **Status:** implemented in this change.

## Problem

`serves` (#751) is self-declared by the same model that proposes the task,
and the completed sidecar (#773) proves a change was *integrated* — nothing
verifies it created *value*. The instance workspace accumulates scripts
(~75) whose usefulness is unknown; dead artifacts pollute the inventory and
existence index that future proposals are deduped against, and the loop
cannot learn which work matters.

**Binding scope constraint (AIDE² anti-reward-hacking, issue comment):**
the harness must never trust a subagent's *claimed* result. All evidence
here derives ONLY from harness-observable signals (file mtimes the
interpreter or a real consumer produced, gate/result artifacts) — never
from proposal or subagent text. AIDE² measured 63% false-positive
self-reported improvement claims; independent verification cut this to 34%.

## Intended change

1. **Usage-evidence collector** — `nanobot/runtime/usage_evidence.py`,
   deterministic (no LLM), fail-open. `refresh_usage(state_dir,
   selfevo_repo)` records per `scripts/*.py` artifact:
   - `last_used` from `__pycache__/<stem>.cpython-*.pyc` mtime
     (signal `pycache`) and from the mtime of an existing
     `state/...`/`docs/...` output artifact named in the FIRST 50 lines of
     the script (signal `output` — bounded extraction; the named file must
     exist, prose alone claims nothing);
   - `last_touched` from the artifact appearing in a recent subagent
     RESULT file's `files_changed` (bounded 50 files) — *modified*, tracked
     separately from *used*.
   Sidecar `<state_dir>/usage/last_used.json` (`usage-evidence-v1`);
   max-merge (never regress newer→older); full rescans gated by repo HEAD +
   6h watermark (#749 pattern). systemd/cron traces are unreachable from
   the state dir — deliberately skipped, never faked.
2. **Confirmed-serves tie-back** — `confirm_serves(state_dir,
   selfevo_repo)`: a `demand/completed.json` entry whose `files_changed`
   includes a `scripts/` artifact with `last_used` NEWER than the
   completion `ts` gains `"confirmed": true` + `confirmed_at` + `signal`.
   Additive only; `last_touched` never confirms; text claims never confirm.
3. **Decay demand kind** — `demand.collect_demand` gains kind `decay`,
   ordered LAST (priority > defect > hypothesis > decay): artifacts unused
   AND untouched > 14 days (`_DECAY_DAYS`), max 5 oldest
   (`_MAX_DECAY_ITEMS`), git last-commit date as fallback `last_touched`
   for never-observed artifacts, skip when nothing derivable. NEVER
   auto-deletes — decay items are archival *proposals* through the normal
   gate. `collect_demand` calls `refresh_usage` + `confirm_serves`
   (fail-open wrapped) before building decay items.
4. **Reporting** — `scripts/loop_metrics_report.py` renders a value-
   verification block: declared vs confirmed completed counts,
   decay-candidate count, oldest tracked artifact; missing sidecars → zeros.

## Acceptance

- [x] Sidecar tracks `last_used`/`last_touched` for `scripts/` artifacts
      from harness signals only; refresh is watermark-gated (HEAD + 6h)
      and max-merged (tests: signals, watermark no-op, no-regress merge,
      fail-open on unreadable repo/state).
- [x] `confirm_serves`: newer usage confirms; older does not; additive
      only; explicit regression pin that text claims alone NEVER confirm.
- [x] Decay: >14d unused+untouched → `decay` demand item ordered last,
      max 5 oldest, only `scripts/`; recently used/touched not flagged;
      git-fallback for never-observed; no-evidence-no-history skipped.
- [x] `loop_metrics_report` shows declared-vs-confirmed and decay counts;
      missing sidecars read as zeros (self-test pinned).
- [ ] At least one live decay proposal observed on eeepc (or an honest
      empty-decay report if everything is in use) — post-deploy check.

## Out of scope

- Auto-deletion or auto-archival of any artifact (decay only proposes).
- Execution-trace signals from systemd/cron journals (not reachable from
  the state dir; skipped rather than faked).
- Scorecard metric-movement confirmation (#765 integration) — `confirmed`
  here means consumption evidence; before/after metric tie-in is follow-up.
- Artifacts outside `scripts/*.py`.
