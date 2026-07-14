"""Issue #716: pre-spawn suppression of recently-failed/rejected proposals.

#713's pre-spawn dedup (_task_already_done) only catches duplicates that
already landed as a real git commit. A proposal that was blocked, produced no
commit, or was rolled back is not in git log at all, so it could be
re-proposed and re-spawned every cycle. _recent_failure_match() closes that
gap with a bounded-recency scan of state/subagents/results/*.json, reusing
the same failure-proxy criteria as _recent_activity_context() and the same
keyword-overlap threshold as _task_already_done().
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
