"""#1281: a subagent that edited files and THEN died on its LLM call.

The auto-commit safety net (#666, unconditional since #717) commits the
uncommitted edit, the gate decides, and the cycle can integrate — the path
cycle ``3cbcc1f77d25`` took on 2026-09-04. The decision recorded on #1281 is
to keep that path; what this file pins is (a) that it still exists after
#1282 made the no-work case terminal, and (b) that every firing is countable
from the ledger alone: the outcome row carries ``executor_llm_error: true``
even when the outcome is ``success``, so "integrated despite a dead
executor" no longer needs a telemetry-to-result join to measure.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.runtime import bridge, cycle_ledger
from tests.test_bridge_executor_llm_error import (
    TRANSPORT_ERROR,
    _result_for,
    _stub_planning_session,
    _wire,
)
from tests.test_cycle_ledger import _read_ledger, _seed_bridge_request


class _EditedThenDiedSubagentManager:
    """The 3cbcc1f77d25 shape: ``edit_file`` landed in the working tree, no
    ``git commit`` yet, and the next LLM call raised. ``_run_subagent`` wrote
    ``status: error`` telemetry; the edit stayed on disk."""

    task_id = "0652e0eb"

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace
        self._running_tasks: dict = {}

    async def spawn(self, **_kwargs):
        (self.workspace / "scripts").mkdir(exist_ok=True)
        (self.workspace / "scripts" / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
        telemetry_dir = bridge.STATE_DIR / "subagents"
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        (telemetry_dir / f"{self.task_id}.json").write_text(json.dumps({
            "task_id": self.task_id,
            "status": "error",
            "summary": TRANSPORT_ERROR,
            "result": TRANSPORT_ERROR,
            "started_at": "2026-09-04T02:47:09Z",
            "finished_at": "2026-09-04T03:20:13Z",
        }), encoding="utf-8")

        async def _done():
            return None

        self._running_tasks[self.task_id] = asyncio.ensure_future(_done())
        return "fake subagent spawned (edited, then LLM dead)"


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    # ADR-034 rule 3: should_propose/build_context/bridge.py's executor
    # gate all now hard-require a real release charter to proceed.
    _adr034_release_root = tmp_path / "_adr034_release_root"
    _adr034_release_root.mkdir(exist_ok=True)
    (_adr034_release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", _adr034_release_root)


def test_edit_then_dead_llm_becomes_interrupted_supply(tmp_path, monkeypatch):
    """Round 2 external re-check, item 1 (architect resolution
    2026-09-26): completion is POSITIVE-only, and the barrier applies to
    ANY commit the session made -- including the #666 residual auto-commit
    that rescued this edit. This REVOKES the #1281 decision this test used
    to pin ("the net fired, the gate passed, the cycle integrated"): a
    session whose own telemetry says ``status: error`` is not finished,
    however the commit landed, so the auto-committed edit must now become
    a pending open increment (classified ``interrupted_supply`` -- the
    TRANSPORT_ERROR text is a recognized supplier-outage pattern) instead
    of integrating.
    """
    title = "Add feature helper"
    state_dir = _wire(tmp_path, monkeypatch, _EditedThenDiedSubagentManager)
    _seed_bridge_request(state_dir, "req-edited", "cycle-edited", task_title=title)
    _stub_planning_session(monkeypatch, title)

    rc = asyncio.run(bridge._main_impl())
    assert rc == 0

    res = _result_for(state_dir)
    assert res["rollback"]["integrated"] is False
    assert res["rollback"]["reason"] == bridge.LLM_SUPPLIER_PAUSED_REASON
    assert res["result_status"] == "blocked"
    assert res["commits_pushed"] == 0
    assert any("did not finish" in s for s in res.get("key_learnings") or [])

    outcome = [r for r in _read_ledger(state_dir) if r["phase"] == "outcome"][-1]
    assert outcome["outcome"] == "failed"
    assert outcome["reason"] == bridge.LLM_SUPPLIER_PAUSED_REASON
    assert outcome["executor_llm_error"] is True

    from nanobot.runtime import open_increment

    pending = open_increment.pending_open_increment(state_dir)
    assert pending is not None, "the auto-committed edit must become a pending open increment, not integrate"
    assert pending["reason"] == "interrupted_supply"


class TestOutcomeRowFlag:
    def test_flag_is_written_when_set(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1", executor_llm_error=True,
        )
        row = _read_ledger(tmp_path)[0]
        assert row["executor_llm_error"] is True
        assert row["outcome"] == "success" and row["reason"] is None  # untouched

    def test_flag_is_absent_not_false_when_not_set(self, tmp_path):
        """Additive like #1118's verdict: the pre-#1281 row shape is byte-identical when the flag is off."""
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")
        cycle_ledger.record_cycle_outcome(tmp_path, "c2", "failed", "gate", [], None, executor_llm_error=False)
        rows = _read_ledger(tmp_path)
        assert "executor_llm_error" not in rows[0]
        assert "executor_llm_error" not in rows[1]
