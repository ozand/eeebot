"""ADR-035 §6: the rotation-queue "bulk-skip drains multiple duplicates in
one run" mechanism this file used to test is retired. The planning session
now produces exactly ONE candidate per cycle (ADR-035 rule 1, #1942); a
pre-spawn dedup match on that candidate ends the run as a single recorded
skip, never a reason to pick up a substitute candidate from a queue -- there
is no queue left to drain.

Reuses the bridge-integration harness from tests/test_cycle_ledger.py.
"""
from __future__ import annotations

import asyncio

import pytest

from nanobot.runtime import bridge
from tests.test_cycle_ledger import _FakeSubagentManager, _init_selfevo_repo, _read_ledger


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch, tmp_path):
    """Mirrors tests/test_cycle_ledger.py: point the bounded gate's
    core-smoke set at the one test file these fixtures create.
    """
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    # ADR-034 rule 3: should_propose/build_context/bridge.py's executor
    # gate all now hard-require a real release charter to proceed.
    _adr034_release_root = tmp_path / "_adr034_release_root"
    _adr034_release_root.mkdir(exist_ok=True)
    (_adr034_release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", _adr034_release_root)


class TestSingleCandidatePerCycle:
    def test_duplicate_plan_ends_run_no_second_attempt(self, tmp_path, monkeypatch):
        """A pre-spawn dedup match on the planner's ONE plan ends the run as
        a single recorded skip -- the planner is not invoked again, and no
        substitute candidate is picked up in the same run."""
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        (state_dir / "goals").mkdir(parents=True, exist_ok=True)
        (state_dir / "goals" / "goal_text.json").write_text(
            '{"schema_version": "goal-text-v1", "goal_id": "goal-1", "text": "test goal"}',
            encoding="utf-8",
        )
        _init_selfevo_repo(base)

        planning_calls: list[dict] = []

        async def _fake_planning_session(**kwargs):
            planning_calls.append(kwargs)
            return {
                'ran': True, 'iterations_used': 1, 'iterations_planned': 1,
                'tampered_files': [], 'plan': {'plan': 'a duplicate increment', 'candidate_id': None},
            }

        monkeypatch.setattr(bridge, "_run_planning_session", _fake_planning_session)
        # Force the very first dedup gate (tag-first, #721) to match -- the
        # cheapest deterministic way to trigger the skip path, independent
        # of title/keyword parsing.
        monkeypatch.setattr(bridge, "_cycle_tag_exists", lambda *_a, **_k: True)

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        # The planner runs exactly once per cycle -- there is no queue to
        # retry a substitute candidate from within the same run.
        assert len(planning_calls) == 1

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["outcome"] == "skipped-duplicate"

        results_dir = state_dir / "subagents" / "results"
        assert len(list(results_dir.glob("result-*.json"))) == 1
