"""Issue #716: pre-spawn suppression of recently-failed/rejected proposals.

The retired fuzzy git-log pre-spawn dedup only caught duplicates that
already landed as a real git commit. A proposal that was blocked, produced no
commit, or was rolled back is not in git log at all, so it could be
re-proposed and re-spawned every cycle. _recent_failure_match() closes that
gap with a bounded-recency scan of state/subagents/results/*.json, reusing
the same failure-proxy criteria as _recent_activity_context() and the same
the established keyword-overlap threshold.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from nanobot.runtime import bridge
from nanobot.runtime.bridge import _recent_failure_match
from tests.test_cycle_ledger import (
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)


def _write_result(results_dir: Path, name: str, **fields) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / name
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def test_matches_recent_blocked_result_by_keyword_overlap(tmp_path: Path):
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title="Wire host_metrics dashboard integration panel",
        result_status="blocked",
    )

    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir,
    ) == "Wire host_metrics dashboard integration panel"


def test_out_of_band_result_does_not_suppress(tmp_path: Path):
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title="Refactor coordinator materializer split logic",
        result_status="blocked",
        rollback={"reason": "out_of_band_main_detected"},
    )
    assert _recent_failure_match(
        "Refactor coordinator materializer split logic", state_dir,
    ) is None


def test_matches_recent_result_via_rollback_reason(tmp_path: Path):
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title="Refactor coordinator materializer split logic",
        rollback={"reason": "mutation_surface_violation"},
    )

    assert _recent_failure_match(
        "Refactor coordinator materializer split logic", state_dir,
    ) == "Refactor coordinator materializer split logic"


def test_non_matching_title_returns_false(tmp_path: Path):
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title="Completely unrelated task about widget sprockets",
        result_status="blocked",
    )

    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir,
    ) is None


def test_completed_result_does_not_suppress(tmp_path: Path):
    """A result with no rollback.reason and a non-failure status must not match,
    even with strong keyword overlap — only failed/rejected results suppress.
    """
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title="Wire host_metrics dashboard integration panel",
        result_status="completed",
    )

    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir,
    ) is None


def test_bounded_window_excludes_stale_failure(tmp_path: Path):
    """Proves the suppression is not permanent: a matching failed result older
    than window_hours must NOT suppress — legitimately-retryable work isn't
    blocked forever.
    """
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    path = _write_result(
        results_dir,
        "r1.json",
        backlog_title="Wire host_metrics dashboard integration panel",
        result_status="blocked",
    )
    # Age the file well past a 1-hour window (72h old).
    stale_time = time.time() - 72 * 3600
    os.utime(path, (stale_time, stale_time))

    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir, window_hours=1,
    ) is None
    # ...but it DOES match with a window wide enough to cover it.
    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir, window_hours=100,
    ) == "Wire host_metrics dashboard integration panel"


def test_empty_title_returns_false(tmp_path: Path):
    state_dir = tmp_path / "state"
    assert _recent_failure_match("", state_dir) is None


def test_missing_results_dir_fails_open(tmp_path: Path):
    state_dir = tmp_path / "does-not-exist"
    assert _recent_failure_match("some task title words", state_dir) is None


# ─── #757: intent-keyed precision (no theme cascade) ─────────────────────────


def test_skipped_test_suite_does_not_suppress_tests_for_other_script(tmp_path: Path):
    """#757 live evidence (2026-07-14 15:45→16:49Z): one skipped 'Create test
    suite for X script' suppressed every later 'Create unit tests for Y
    script' — they share the generic word bag (create/unit/tests/script).
    Different derived (action, target) must NOT match."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title="Create test suite for approval truth normalization script",
        result_status="blocked",
    )

    assert _recent_failure_match(
        "Create unit tests for backlog health script", state_dir,
    ) is None
    # Same with a tests/ target path on the proposal side.
    assert _recent_failure_match(
        "Create unit tests for backlog health script", state_dir,
        target_path="tests/test_backlog_health.py",
    ) is None


def test_retry_of_same_test_target_is_still_suppressed(tmp_path: Path):
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    historical = "Create test suite for approval truth normalization script"
    _write_result(results_dir, "r1.json", backlog_title=historical, result_status="blocked")

    # Identical wording — same derived (test-for, subject) target.
    assert _recent_failure_match(historical, state_dir) == historical
    # Different wording, same subject — intent keying still catches it.
    assert _recent_failure_match(
        "Create unit tests for approval truth script", state_dir,
        target_path="tests/test_approval_truth.py",
    ) == historical


def test_returns_matched_historical_title_not_proposal_title(tmp_path: Path):
    """#757: the return value is what the ledger records as matched_against —
    it must be the HISTORICAL title, not an echo of the proposal's own."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    historical = "Wire host_metrics dashboard integration panel improvements"
    _write_result(results_dir, "r1.json", backlog_title=historical, result_status="blocked")

    matched = _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir,
    )
    assert matched == historical


def test_unreadable_result_file_is_skipped_not_fatal(tmp_path: Path):
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "bad.json").write_text("{not valid json", encoding="utf-8")
    _write_result(
        results_dir,
        "good.json",
        backlog_title="Wire host_metrics dashboard integration panel",
        result_status="blocked",
    )

    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir,
    ) == "Wire host_metrics dashboard integration panel"


# ─── #798: skips are not failures; cross-target intent precision ─────────────


def test_skip_bookkeeping_rows_do_not_suppress(tmp_path: Path):
    """#798 defect 2 (live 2026-07-18 16:20–23:14Z): the dedup skip branches
    themselves write result rows (result_status='blocked' with a
    rollback.reason naming the skip). Counting those as failure history let
    one false-positive skip suppress EVERY later decay proposal off the
    previous skip's title — suppressions are bookkeeping, not failures."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r-skip-existence.json",
        backlog_title="Archive unused collect_telegram_live_proof script",
        result_status="blocked",
        rollback={"integrated": False, "reason": "existence_index_duplicate"},
    )
    _write_result(
        results_dir,
        "r-skip-recent.json",
        backlog_title="Archive unused memory_archiver deprecated script",
        result_status="blocked",
        rollback={"integrated": False, "reason": "recent_duplicate_failure"},
    )

    # Exact-title retries of either skipped proposal are NOT suppressed...
    assert _recent_failure_match(
        "Archive unused collect_telegram_live_proof script", state_dir,
    ) is None
    assert _recent_failure_match(
        "Archive unused memory_archiver deprecated script", state_dir,
    ) is None
    # ...nor is a later same-vocabulary proposal (the cascade shape).
    assert _recent_failure_match(
        "Archive unused eeepc_privileged_rollout_preflight script", state_dir,
    ) is None


def test_skipped_result_status_does_not_suppress(tmp_path: Path):
    """Belt for the same class (#798): any 'skipped*' result_status row is
    bookkeeping too, never failure history — regardless of rollback."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title="Wire host_metrics dashboard integration panel",
        result_status="skipped-duplicate",
        rollback={"integrated": False, "reason": "some_future_skip_marker"},
    )

    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir,
    ) is None


def test_real_failure_same_intent_same_path_still_blocked(tmp_path: Path):
    """#798 acceptance: narrowing must not weaken the gate — a GENUINE
    failure still suppresses a reworded retry naming the same concrete
    target path (both intents derive ("change", <same path>))."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    historical = "Archive unused collect_telegram_live_proof script"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title=historical,
        result_status="blocked",
        target_path="scripts/collect_telegram_live_proof.py",
        rollback={"integrated": False, "reason": "smoke_gate_failed"},
    )

    assert _recent_failure_match(
        "Remove the unused collect_telegram_live_proof helper script",
        state_dir,
        target_path="scripts/collect_telegram_live_proof.py",
    ) == historical


def test_same_vocabulary_different_targets_do_not_match(tmp_path: Path):
    """#798 defect 3: decay titles share the archive/unused/script
    vocabulary (>=3 overlapping words), so the word bag alone chains across
    DIFFERENT target scripts. When both sides carry a concrete target_path
    and they differ, the entry is never a match — and the word-bag fallback
    is not consulted for that pair."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title="Archive unused collect_telegram_live_proof script",
        result_status="blocked",
        target_path="scripts/collect_telegram_live_proof.py",
        rollback={"integrated": False, "reason": "smoke_gate_failed"},
    )

    assert _recent_failure_match(
        "Archive unused memory_archiver script", state_dir,
        target_path="scripts/memory_archiver.py",
    ) is None


def test_word_bag_fallback_preserved_when_historical_row_lacks_target(tmp_path: Path):
    """Fail-open unchanged (#798): a pre-#798 result row that recorded no
    target_path still suppresses via the word bag — the different-target
    cut only applies when BOTH sides carry a concrete path."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    historical = "Archive unused collect_telegram_live_proof script"
    _write_result(
        results_dir,
        "r1.json",
        backlog_title=historical,
        result_status="blocked",
    )

    assert _recent_failure_match(
        historical, state_dir,
        target_path="scripts/collect_telegram_live_proof.py",
    ) == historical


# ─── bridge integration: truthful ledger matched_against (#757) ──────────────


class _ExplodingSubagentManager:
    """Fails the test if a subagent is ever spawned — proves the
    recent-failure suppression skips BEFORE any spawn attempt."""

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace

    async def spawn(self, **_kwargs):
        raise AssertionError(
            "subagent should not have been spawned — recent-failure suppression should have skipped"
        )


@pytest.fixture()
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


def test_ledger_matched_against_is_historical_title(
    tmp_path, monkeypatch, _core_smoke_set_matches_fixture_repo,
):
    """#757 defect 3: the skipped_recent_failure ledger row used to record the
    proposal's OWN title as matched_against; it must be the historical title
    the suppression actually matched."""
    base = tmp_path
    state_dir = base / "state"
    state_dir.mkdir()
    _origin, _work = _init_selfevo_repo(base)

    historical = "Wire host_metrics dashboard integration panel improvements"
    _write_result(
        state_dir / "subagents" / "results",
        "r-historical.json",
        request_id="r-historical",
        backlog_title=historical,
        result_status="blocked",
    )

    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", _ExplodingSubagentManager)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

    _seed_bridge_request(
        state_dir,
        "req-recent-failure",
        "cycle-recent-failure",
        task_title="Wire host_metrics dashboard integration panel",
        task="Wire host_metrics dashboard integration panel.\n",
    )

    result = asyncio.run(bridge._main_impl())
    assert result == 0

    rows = _read_ledger(state_dir)
    dedup_rows = [r for r in rows if r["phase"] == "dedup"]
    assert [r["decision"] for r in dedup_rows] == ["skipped_recent_failure"]
    assert dedup_rows[0]["matched_against"] == historical


def test_matches_a_failure_that_lives_only_in_the_archive(tmp_path: Path):
    """#1176: results/ empties into archive/ within the hour, the window is 24h.

    Measured on the host 2026-09-02: 6 artifacts in results/ covering ~1 hour,
    3,059 in archive/, and of the 9 failures inside the 24h window the live
    directory held 1. A results-only scan therefore returned "no recent
    failure" for 89% of the history it believed it was reading — and that
    answer is indistinguishable from there genuinely being none.
    """
    state_dir = tmp_path / "state"
    archive_dir = state_dir / "subagents" / "archive"
    (state_dir / "subagents" / "results").mkdir(parents=True, exist_ok=True)
    _write_result(
        archive_dir,
        "archived-failure.json",
        backlog_title="Wire host_metrics dashboard integration panel",
        result_status="blocked",
    )

    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel again",
        state_dir,
    ) == "Wire host_metrics dashboard integration panel"


def test_failure_is_selected_before_the_max_scan_bound(tmp_path: Path):
    """#1176: bound the failures, not the candidates.

    `candidates[:max_scan]` ran before the failure filter. With ~6 live files
    that never mattered; against the archive's 3,000+ the newest `max_scan`
    entries are overwhelmingly successes, so the bound alone would report "no
    recent failure" every time — the same silence the gate exists to break.
    """
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    old = time.time() - 3600.0
    _write_result(
        results_dir,
        "the-failure.json",
        backlog_title="Wire host_metrics dashboard integration panel",
        result_status="blocked",
    )
    os.utime(results_dir / "the-failure.json", (old, old))
    # Twenty newer successes bury it well past max_scan=10.
    for i in range(20):
        p = _write_result(
            results_dir,
            f"success-{i:02d}.json",
            backlog_title=f"Unrelated completed chore number {i}",
            result_status="completed",
        )
        os.utime(p, (old + 60 + i, old + 60 + i))

    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel again",
        state_dir,
    ) == "Wire host_metrics dashboard integration panel"
