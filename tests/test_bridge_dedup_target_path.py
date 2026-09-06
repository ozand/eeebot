"""Tests for _extract_target_path, _write_bridge_completed_result target_path
recording (#798), and skip-row isolation from _recent_failure_match (#798).

Note (#1333): the fuzzy git-log gate (_task_already_done / _task_already_done_for_path)
was retired. Tests that covered its target-path scoping behavior
(TestMissingTargetPathBypassesKeywordHeuristic, TestExistingTargetPathScopesKeywordHeuristic,
TestDemandVettedRequestBypassesAlreadyDone, TestSecondArchiveProposalIsBlocked,
TestNoTargetPathFallsBackUnchanged) are removed alongside the gate they tested.
Active tests below cover _extract_target_path and the still-live result/recent-failure
contract.

Reuses the bridge-integration harness from tests/test_cycle_ledger.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


# ─── _extract_target_path unit tests ──────────────────────────────────────────


class TestExtractTargetPath:
    def test_extracts_from_llm_proposer_shaped_request(self):
        req = {
            "task_title": "Implement and commit: Create a memory pressure checker",
            "task": (
                "Add a script that checks RAM and swap usage to detect memory "
                "pressure.\n\nTarget path: scripts/check_memory_pressure.py"
            ),
            "recommended_next_action": (
                "Implement and commit: Create a memory pressure checker "
                "(target: scripts/check_memory_pressure.py)"
            ),
        }
        assert bridge._extract_target_path(req) == "scripts/check_memory_pressure.py"

    def test_falls_back_to_recommended_next_action(self):
        req = {
            "task": "no target path marker here at all",
            "recommended_next_action": "Implement and commit: X (target: scripts/x.py)",
        }
        assert bridge._extract_target_path(req) == "scripts/x.py"

    def test_returns_none_when_absent(self):
        req = {"task": "just a plain task with no marker", "recommended_next_action": ""}
        assert bridge._extract_target_path(req) is None

    def test_returns_none_for_empty_request(self):
        assert bridge._extract_target_path({}) is None

    def test_fail_open_on_garbage_input(self):
        class _Weird:
            def get(self, *_a, **_k):
                raise RuntimeError("boom")

        # Must not raise — fail-open to None.
        assert bridge._extract_target_path(_Weird()) is None


# ─── pre-spawn dedup integration tests ────────────────────────────────────────


TITLE = "Create a script to check memory pressure levels"
TASK_TEXT_TEMPLATE = (
    "Add a script that checks RAM and swap usage to detect memory "
    "pressure.\n\nTarget path: {target_path}"
)


def _setup(base, monkeypatch):
    state_dir = base / "state"
    state_dir.mkdir()
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    return state_dir


class TestResultRecordsTargetPath:
    def test_result_file_records_request_target_path(self, tmp_path, monkeypatch):
        """#798: _write_bridge_completed_result stores the request's own
        target path in the result artifact, so _recent_failure_match can
        compare a new proposal's target against the historical entry's
        instead of chaining on shared verb vocabulary."""
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _init_selfevo_repo(base)

        target_path = "scripts/check_memory_pressure.py"  # does NOT exist in repo
        _seed_bridge_request(
            state_dir, "req-record", "cycle-record",
            task_title=f"Implement and commit: {TITLE}",
            task=TASK_TEXT_TEMPLATE.format(target_path=target_path),
            recommended_next_action=f"Implement and commit: {TITLE} (target: {target_path})",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        result_path = state_dir / "subagents" / "results" / "result-req-record.json"
        data = json.loads(result_path.read_text(encoding="utf-8"))
        assert data["target_path"] == target_path


class TestSkipRowsDoNotFeedRecentFailure:
    def test_prior_skip_row_does_not_block_new_proposal(self, tmp_path, monkeypatch):
        """#798 defect 2, end-to-end: a prior SKIP result row (the dedup
        branches write result_status='blocked' with a skip rollback.reason)
        must not become the 'recent failure' that suppresses the next
        same-vocabulary proposal — the live decay cascade shape."""
        base = tmp_path
        state_dir = _setup(base, monkeypatch)
        _init_selfevo_repo(base)

        results_dir = state_dir / "subagents" / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "result-req-prior.json").write_text(
            json.dumps({
                "request_id": "req-prior",
                "backlog_title": TITLE,
                "result_status": "blocked",
                "rollback": {"integrated": False, "reason": "existence_index_duplicate"},
            }),
            encoding="utf-8",
        )

        target_path = "scripts/check_memory_pressure.py"  # does NOT exist in repo
        _seed_bridge_request(
            state_dir, "req-after-skip", "cycle-after-skip",
            task_title=f"Implement and commit: {TITLE}",
            task=TASK_TEXT_TEMPLATE.format(target_path=target_path),
            recommended_next_action=f"Implement and commit: {TITLE} (target: {target_path})",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [
            r for r in rows
            if r.get("cycle_id") == "cycle-after-skip" and r["phase"] == "outcome"
        ]
        assert len(outcome_rows) == 1
        # Proceeded to spawn (success), NOT suppressed off the prior skip row.
        assert outcome_rows[0]["outcome"] == "success"
