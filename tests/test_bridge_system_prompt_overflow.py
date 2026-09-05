"""#1300: a cycle whose system prompt cannot hold every critical AGENTS.md
section is a failed cycle that spawns nothing — and says so where cycles are
read (result status + reason, ledger outcome, exit status → exit_streak). On
success, what the cap kept and dropped is a ledger row, not a journal line.

Drives ``bridge._main_impl`` end to end with fake SubagentManagers, the same
way tests/test_bridge_executor_llm_error.py does for #1280.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot import crash_record
from nanobot.agent.context import SystemPromptOverflowError
from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)

OVERFLOW = SystemPromptOverflowError(
    over_by=11_434, cap=24_000,
    sections={"identity": 1446, "bootstrap": 22986, "skills_catalogue": 6951, "memory": 4030},
    dropped=[{"section": "## Optional appendix", "chars": 512, "how": "declared-droppable"}],
)


class _OverflowingManager(_FakeSubagentManager):
    """The strict builder refuses; spawn must never be reached."""

    spawned = False

    def _build_subagent_prompt(self) -> str:
        self.last_prompt_fit = {"cap": 24_000, "chars": 35_434, "strict": True, "dropped": OVERFLOW.dropped}
        raise OVERFLOW

    async def spawn(self, **kwargs):
        type(self).spawned = True
        return await super().spawn(**kwargs)


class _FittingManager(_FakeSubagentManager):
    """A prompt that fits after one declared-droppable section went."""

    def _build_subagent_prompt(self) -> str:
        self.last_prompt_fit = {"cap": 24_000, "chars": 23_500, "strict": True,
                                "dropped": [{"section": "## Optional appendix", "chars": 900, "how": "declared-droppable"}]}
        return "system prompt"


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


def _wire(tmp_path, monkeypatch, manager_cls):
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    _init_selfevo_repo(tmp_path)
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", manager_cls)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    monkeypatch.setenv("SELFEVO_DUMP_PROMPTS", "0")
    return state_dir


def _result_for(state_dir, request_id):
    matches = list((state_dir / "subagents").rglob(f"result-{request_id}.json"))
    assert len(matches) == 1, matches
    return json.loads(matches[0].read_text(encoding="utf-8"))


def test_overflow_is_a_failed_unspawned_cycle_with_its_own_exit_code(tmp_path, monkeypatch):
    state_dir = _wire(tmp_path, monkeypatch, _OverflowingManager)
    _OverflowingManager.spawned = False
    _seed_bridge_request(state_dir, "req-over", "cycle-over", task_title="Extend a skill")

    rc = asyncio.run(bridge._main_impl())

    assert _OverflowingManager.spawned is False, "no executor runs on a prompt missing its standing instructions"
    res = _result_for(state_dir, "req-over")
    assert res["result_status"] == "blocked"
    assert res["rollback"]["reason"] == "system_prompt_overflow"
    assert res["commits_pushed"] == 0
    assert any("SYSTEM PROMPT OVERFLOW (#1300)" in s and "11434" in s for s in res.get("key_learnings") or [])

    rows = _read_ledger(state_dir)
    outcome = [r for r in rows if r["phase"] == "outcome"][-1]
    assert outcome["outcome"] == "failed" and outcome["reason"] == "system_prompt_overflow"
    fit_rows = [r for r in rows if r["phase"] == "system_prompt"]
    assert len(fit_rows) == 1
    assert fit_rows[0]["overflow"] is True and fit_rows[0]["over_by"] == 11_434 and fit_rows[0]["cap"] == 24_000
    assert fit_rows[0]["sections"]["bootstrap"] == 22_986
    assert fit_rows[0]["dropped"] == OVERFLOW.dropped

    # exit status → the streak the health dimension and the deploy gate read
    assert rc == bridge.EXIT_SYSTEM_PROMPT_OVERFLOW != 0
    assert bridge.EXIT_SYSTEM_PROMPT_OVERFLOW != bridge.EXIT_EXECUTOR_LLM_ERROR
    streak = crash_record.record_exit(state_dir, outcome="success" if rc == 0 else "failure", exit_status=rc)
    assert streak["consecutive_failures"] == 1 and streak["last_exit_status"] == bridge.EXIT_SYSTEM_PROMPT_OVERFLOW

    # the request is not retired and not counted as an LLM retry: it is not its fault
    bridge_state = state_dir / "subagent_bridge"
    assert not (bridge_state / "handled_req-over.txt").exists()
    assert not (bridge_state / "retry_req-over.json").exists()


def test_fitting_prompt_journals_what_the_cap_dropped(tmp_path, monkeypatch):
    state_dir = _wire(tmp_path, monkeypatch, _FittingManager)
    _seed_bridge_request(state_dir, "req-fit", "cycle-fit", task_title="Extend a skill")

    rc = asyncio.run(bridge._main_impl())

    assert rc == 0
    rows = _read_ledger(state_dir)
    fit_rows = [r for r in rows if r["phase"] == "system_prompt"]
    assert len(fit_rows) == 1
    assert fit_rows[0]["cycle_id"] == "cycle-fit"
    assert fit_rows[0]["chars"] == 23_500 and fit_rows[0]["cap"] == 24_000
    assert fit_rows[0]["dropped"] == [{"section": "## Optional appendix", "chars": 900, "how": "declared-droppable"}]
    assert "overflow" not in fit_rows[0]
    assert [r for r in rows if r["phase"] == "outcome"][-1]["outcome"] == "success"
