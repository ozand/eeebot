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

import json
import os
import time
from pathlib import Path

from nanobot.runtime.bridge import _recent_failure_match


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
    ) is True


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
    ) is True


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
    ) is False


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
    ) is False


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
    ) is False
    # ...but it DOES match with a window wide enough to cover it.
    assert _recent_failure_match(
        "Wire host_metrics dashboard integration panel", state_dir, window_hours=100,
    ) is True


def test_empty_title_returns_false(tmp_path: Path):
    state_dir = tmp_path / "state"
    assert _recent_failure_match("", state_dir) is False


def test_missing_results_dir_fails_open(tmp_path: Path):
    state_dir = tmp_path / "does-not-exist"
    assert _recent_failure_match("some task title words", state_dir) is False


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
    ) is True
