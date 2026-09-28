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
)


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    # ADR-034 rule 3: should_propose/build_context/bridge.py's executor
    # gate all now hard-require a real release charter to proceed.
    _adr034_release_root = tmp_path / "_adr034_release_root"
    _adr034_release_root.mkdir(exist_ok=True)
    (_adr034_release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", _adr034_release_root)


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
    # #1222: the bridge resolves the active goal from goals/goal_text.json.
    (state_dir / "goals").mkdir(parents=True, exist_ok=True)
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": "goal-1", "text": "test goal"}),
        encoding="utf-8",
    )
    return state_dir


def _seed_plan(monkeypatch, task_title: str, target_path: str) -> None:
    """ADR-035 rule 1 (#1942): req/task are now built from the planning
    session's own plan, never from a rotation-picked queue file — stand in
    for a real planning session with a canned plan whose first line matches
    the desired task_title and whose body carries the ``Target path:`` line
    _extract_target_path reads (mirrors the shape llm_proposer.write_request
    used to produce for the retired queue-seeding path)."""
    plan_text = f"{task_title}\n\n{TASK_TEXT_TEMPLATE.format(target_path=target_path)}"

    async def _fake_planning_session(**_kwargs):
        return {
            'ran': True, 'iterations_used': 1, 'iterations_planned': 1,
            'tampered_files': [], 'plan': {'plan': plan_text, 'candidate_id': None},
        }

    monkeypatch.setattr(bridge, "_run_planning_session", _fake_planning_session)


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
        _seed_plan(monkeypatch, f"Implement and commit: {TITLE}", target_path)

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        results_dir = state_dir / "subagents" / "results"
        result_files = [p for p in results_dir.glob("result-*.json") if p.name != "result-req-prior.json"]
        assert len(result_files) == 1
        data = json.loads(result_files[0].read_text(encoding="utf-8"))
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
        _seed_plan(monkeypatch, f"Implement and commit: {TITLE}", target_path)

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [
            r for r in rows
            if r["phase"] == "outcome" and r.get("cycle_id") not in ("", None)
        ]
        assert len(outcome_rows) == 1
        # Proceeded to spawn (success), NOT suppressed off the prior skip row.
        assert outcome_rows[0]["outcome"] == "success"
