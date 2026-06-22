# Plan: Bridge Feedback Loop Fix + Priority 18 (Scorer Weights)

## Overview

- Three structural bugs in `scripts/eeepc_self_evolving_subagent_bridge.py` silently prevent the repair loop from ever activating and misreport `commits_pushed=0` even when subagents commit successfully.
- **Bug 1:** `_get_previous_attempts()` matches by keywords in `summary` — but `summary` is always a generic string (`"Bridge subagent ran but produced no new commits."`). Result: `## Previous attempts` is never injected into any subagent prompt.
- **Bug 2:** `commits_pushed` is computed as `git rev-list origin/main..HEAD` *after* execution. When the subagent pushes itself, HEAD == origin/main → `ahead=0` → bridge reports 0 commits even though 1+ were pushed.
- **Bug 3 (consequence of Bug 1):** No `backlog_title` stored in result JSON, so future matching has no durable anchor. Fixed by making the matcher read `source_artifact` → `nbc.title` from the result file itself (schema stays unchanged).
- **Priority 18:** `scorer.py` exposes `score_cycle(weights_path=None)`; coordinator passes path via `SELFEVO_SURFACES_DIR` env var; subagents can propose weight changes in `surfaces/score_weights.json` but they only take effect when the operator sets the env var — frozen scorer invariant preserved.
- All four changes ship in one PR because they share the same data flow (spawn → commit count → result → next prompt).

## Expected behavior

- After fix, when a subagent completes with 0 commits, the **next** bridge invocation for the same task injects `## Previous attempts: 1 attempt, 0 commits → MUST commit`.
- `commits_pushed` correctly reflects the number of commits between `pre_spawn_sha` and `HEAD`, regardless of whether the subagent or the bridge auto-push wrote them.
- `state/bridge_pre_spawn.sha` is written before every spawn and cleaned up after the result is written.
- `_get_previous_attempts()` matches results by reading `source_artifact` → `next_bounded_candidate.title` from the result file; falls back to `summary` keyword matching if the artifact file is missing.
- One-time migration backfills `backlog_title` into the 21 existing `bridge_llm_execution` result files on host.
- `score_cycle(weights_path=Path("…/score_weights.json"))` loads `WEIGHT_COMMITS`, `WEIGHT_MODE`, `WEIGHT_STATUS` from the file; values outside `[0.01, 0.99]` or that don't sum to `≈1.0` are rejected and hardcoded defaults are used.
- `SELFEVO_SURFACES_DIR` env var unset → scorer behaves identically to today (frozen defaults).
- Coordinator logs `scorer_weights_source: surfaces` or `scorer_weights_source: hardcoded` in cycle history JSON.
- All existing 15 scorer tests continue to pass unchanged.

## Implementation plan

### `scripts/eeepc_self_evolving_subagent_bridge.py`

- **`_get_previous_attempts(state_dir, backlog_title, cycle_id, max_attempts=3)`**
  - New primary match: read `data["source_artifact"]`; if the file exists, parse `nbc = json["next_bounded_candidate"]`; compare `nbc["title"]` to `backlog_title` (≥3 word matches, same logic as `_task_already_done`).
  - Existing `summary` keyword match kept as fallback (when artifact file missing/unreadable).
  - Exact `cycle_id` match kept as tertiary fallback.
  - Reject results where `materialized_from != "bridge_llm_execution"` (unchanged).

- **`_capture_pre_spawn_sha(selfevo_repo, sha_file)`** — new helper
  - Runs `git rev-parse HEAD` in `selfevo_repo`.
  - Writes SHA to `sha_file` (`state/bridge_pre_spawn.sha`). Overwrites unconditionally.
  - Returns SHA string or `""` on error.

- **`_count_commits_since(selfevo_repo, pre_spawn_sha)`** — new helper
  - Runs `git rev-list --count <pre_spawn_sha>..HEAD`.
  - Returns `int`; returns `0` on any error or if `pre_spawn_sha` is empty.

- **`main()` — spawn section**
  - Before `await mgr.spawn(...)`: call `_capture_pre_spawn_sha(...)`.
  - After subagent completes: replace existing `rev-list origin/main..HEAD` block with `_count_commits_since(...)`.
  - Auto-push logic unchanged; `commits_pushed` now counts *new* commits whether pushed by subagent or by bridge.
  - Delete `bridge_pre_spawn.sha` after result is written (best-effort cleanup).

- **`_migrate_backlog_title_in_results(results_dir)`** — new one-time helper
  - Iterates `state/subagents/results/*.json` where `materialized_from == "bridge_llm_execution"` and `backlog_title` key absent.
  - Reads `source_artifact` → `nbc["title"]`; writes `backlog_title` field back to result file.
  - Called once at bridge startup (idempotent — skips files that already have the field).

### `nanobot/runtime/scorer.py`

- **`_load_weights(weights_path: Path | None) -> tuple[float, float, float]`** — new private helper
  - Returns `(WEIGHT_COMMITS, WEIGHT_MODE, WEIGHT_STATUS)`.
  - If `weights_path` is `None` or file missing: returns module-level hardcoded defaults.
  - Validates: all three values are floats in `[0.01, 0.99]`; sum within `±0.05` of `1.0`. Any violation → log warning, return hardcoded defaults.
  - Does **not** mutate module-level constants (frozen invariant preserved).

- **`score_cycle(fd, budget, commits_pushed, result_status, weights_path=None) -> ScoringResult`**
  - Calls `_load_weights(weights_path)` to get `w_commits, w_mode, w_status`.
  - Uses those instead of module-level `WEIGHT_COMMITS` etc.
  - `ScoringResult` gains optional field `weights_source: str` — `"surfaces"` or `"hardcoded"`.

### `nanobot/runtime/coordinator.py`

- **`_derive_reward_signal()`** — update `score_cycle` call site
  - Read `SELFEVO_SURFACES_DIR` env var; if set, pass `weights_path = Path(env) / "surfaces" / "score_weights.json"`.
  - Add `scorer_weights_source` to the returned `reward_signal` dict from `scorer_result.weights_source`.

### `scripts/migrate_backlog_title.py` — new standalone script

- One-time migration script (also usable on host to backfill existing results).
- Accepts `--results-dir` arg (default: `STATE_DIR/subagents/results`).
- Prints count of files updated.

### Tests

- **`tests/test_lessons_feedback_loop.py`** — update `TestPreviousAttempts`
  - `test_matches_by_summary_keyword`: update fixture to write `source_artifact` → artifact file with matching `nbc.title`; verify match succeeds via new primary path.
  - Add `test_matches_by_source_artifact_title`: result has `source_artifact` pointing to artifact with `nbc.title = "Restructure MEMORY.md"`; backlog_title same → match.
  - Add `test_falls_back_to_summary_when_artifact_missing`: artifact file deleted → falls back to summary keyword.
  - Add `test_no_match_when_both_miss`: artifact title and summary both differ → empty result.

- **`tests/test_commits_pushed.py`** — new file
  - `test_capture_pre_spawn_sha_writes_file(tmp_path)`: mock `subprocess.run` → SHA written to file.
  - `test_count_commits_since_returns_int(tmp_path)`: mock returns `"3\n"` → returns 3.
  - `test_count_commits_since_zero_on_error(tmp_path)`: subprocess fails → returns 0.
  - `test_count_commits_since_empty_sha(tmp_path)`: `pre_spawn_sha=""` → returns 0.
  - `test_integration_real_git(tmp_path)`: create real git repo, make 2 commits after capturing SHA, verify count=2.

- **`tests/test_frozen_scorer.py`** — extend (15 existing tests unchanged)
  - `test_load_weights_hardcoded_when_no_path()`: `_load_weights(None)` → returns `(0.40, 0.30, 0.30)`.
  - `test_load_weights_from_file(tmp_path)`: write valid `score_weights.json` → returns overridden values.
  - `test_load_weights_rejects_bad_sum(tmp_path)`: weights sum to 0.5 → returns hardcoded defaults.
  - `test_load_weights_rejects_out_of_range(tmp_path)`: one weight = 1.5 → returns hardcoded defaults.
  - `test_score_cycle_weights_source_field()`: `score_cycle(..., weights_path=None).weights_source == "hardcoded"`.
  - `test_score_cycle_with_surfaces_weights(tmp_path)`: valid file → `weights_source == "surfaces"`.

## Implementation phases

### Phase 1 — Fix `_get_previous_attempts` matching (Bug 1 + Bug 3)
- Update `_get_previous_attempts()` to read `source_artifact` → `nbc.title`.
- Add `_migrate_backlog_title_in_results()` called at bridge startup.
- Add 4 new tests in `test_lessons_feedback_loop.py`.
- **Checkpoint:** `## Previous attempts` now appears in subagent prompt after 0-commit session.

### Phase 2 — Fix `commits_pushed` (Bug 2)
- Add `_capture_pre_spawn_sha()` and `_count_commits_since()`.
- Update `main()` spawn section.
- Add `tests/test_commits_pushed.py` (5 tests).
- **Checkpoint:** bridge correctly reports `commits_pushed=1` when subagent self-pushes.

### Phase 3 — Priority 18: scorer weight loading
- Add `_load_weights()` and update `score_cycle()` signature in `scorer.py`.
- Update coordinator call site to pass `weights_path` from env.
- Add 6 new tests in `test_frozen_scorer.py`.
- **Checkpoint:** `SELFEVO_SURFACES_DIR` unset → behavior identical to today; set → weights from file.

### Phase 4 — Deploy and verify on host
- Deploy as new release via `deploy_release.sh`.
- Run `scripts/migrate_backlog_title.py` once on host to backfill 21 existing results.
- Trigger one coordinator cycle; verify next bridge invocation injects `## Previous attempts`.
- Verify `commits_pushed > 0` in result after subagent self-push.

## Testing strategy

- **Unit tests (mock subprocess):** `test_lessons_feedback_loop.py` (4 new), `test_commits_pushed.py` (4 new).
- **Integration test (real git):** `test_commits_pushed.py::test_integration_real_git` — creates tmp git repo, makes commits after SHA capture, checks count.
- **Scorer unit tests:** 6 new in `test_frozen_scorer.py`; 15 existing must pass unchanged.
- **Regression:** full suite `python -m pytest tests/ -x -q` must pass before deploy.
- **Host smoke:** after deploy, check `state/bridge_pre_spawn.sha` written; check `commits_pushed` in newest result; check `## Previous attempts` in bridge journal log for next 0-commit cycle.

## Open questions

- Should `bridge_pre_spawn.sha` be per-request (include `cycle_id` in filename) to support concurrent bridge invocations? Currently bridge is single-instance (systemd oneshot), so shared file is safe.
- Should `_migrate_backlog_title_in_results()` run at every bridge startup or only once (guarded by a sentinel file)? Every-startup is idempotent but adds a small fs scan (~21 files today).
- Should `SELFEVO_SURFACES_DIR` default to the known host path when unset, or remain strictly opt-in? Strict opt-in is safer for the frozen invariant.
