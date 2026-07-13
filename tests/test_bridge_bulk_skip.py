"""Tests for #733: bulk-skip pre-spawn duplicates in a single bridge run.

Live finding from the #707 canary: the bridge processed ONE request per timer
run, so each stale duplicate minted by the deterministic planner burned a full
10-minute slot as a cheap pre-spawn skip — fresh (LLM-proposed) requests
waited 30-90 minutes behind the stale tail. ``_main_impl`` now loops over
pending requests: a pre-spawn duplicate does its full bookkeeping (handled
marker, result file, ledger rows, post tag) then continues to the next
request in the same run, until a non-duplicate spawns (still at most ONE
spawn per run), the queue empties, or ``SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN``
(default 10) is hit.

Reuses the bridge-integration harness from tests/test_cycle_ledger.py (bare
"origin" + working clone standing in for the shared ``eeebot-self-evolving``
checkout).
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from nanobot.runtime import bridge, llm_proposer
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    """Mirrors tests/test_cycle_ledger.py: point the bounded gate's
    core-smoke set at the one test file these fixtures create.
    """
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


def _rows_for_cycle(rows: list[dict], cycle_id: str) -> list[dict]:
    return [r for r in rows if r.get("cycle_id") == cycle_id]


class TestBulkSkipDrainsInOneRun:
    def test_three_duplicates_then_novel_spawns_same_run(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        # Duplicates are any request whose task_title starts with "dup"; the
        # novel request's title does not match, so it proceeds to spawn.
        monkeypatch.setattr(
            bridge, "_task_already_done",
            lambda title, _repo: title.startswith("dup"),
        )

        spawn_calls = []
        real_spawn = _FakeSubagentManager.spawn

        async def _counting_spawn(self, **kwargs):
            spawn_calls.append(1)
            return await real_spawn(self, **kwargs)

        monkeypatch.setattr(_FakeSubagentManager, "spawn", _counting_spawn)

        _seed_bridge_request(state_dir, "req-dup-1", "cycle-dup-1", task_title="dup task alpha")
        _seed_bridge_request(state_dir, "req-dup-2", "cycle-dup-2", task_title="dup task beta")
        _seed_bridge_request(state_dir, "req-dup-3", "cycle-dup-3", task_title="dup task gamma")
        _seed_bridge_request(state_dir, "req-novel", "cycle-novel", task_title="totally novel feature widget")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        # Exactly one spawn for the whole run (S6 invariant), for the novel request.
        assert len(spawn_calls) == 1

        rows = _read_ledger(state_dir)
        for cid in ("cycle-dup-1", "cycle-dup-2", "cycle-dup-3"):
            crows = _rows_for_cycle(rows, cid)
            phases = [r["phase"] for r in crows]
            assert phases == ["started", "dedup", "outcome"], f"{cid}: {phases}"
            assert crows[-1]["outcome"] == "skipped-duplicate"

        novel_rows = _rows_for_cycle(rows, "cycle-novel")
        novel_phases = [r["phase"] for r in novel_rows]
        assert novel_phases[0] == "started"
        assert "gate" in novel_phases
        assert novel_phases[-1] == "outcome"
        assert novel_rows[-1]["outcome"] == "success"

        # Each skipped cycle got its own handled marker (idempotency preserved).
        markers = list((state_dir / "subagent_bridge").glob("handled_*.txt"))
        assert len(markers) == 4

    def test_cap_enforced_remainder_stays_queued(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "_task_already_done", lambda *_a, **_k: True)
        monkeypatch.setattr(bridge, "MAX_SKIPS_PER_RUN", 2)

        propose_calls = []
        monkeypatch.setattr(
            bridge, "_maybe_propose_after_skip",
            lambda *a, **k: propose_calls.append((a, k)),
        )

        for i in range(5):
            _seed_bridge_request(state_dir, f"req-dup-{i}", f"cycle-dup-{i}", task_title=f"dup task {i}")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        markers = list((state_dir / "subagent_bridge").glob("handled_*.txt"))
        assert len(markers) == 2

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert len(outcome_rows) == 2
        assert all(r["outcome"] == "skipped-duplicate" for r in outcome_rows)

        # Proposer invoked exactly once — when the cap ends the run, not per-skip.
        assert len(propose_calls) == 1

        remaining = list((state_dir / "subagents" / "requests").glob("*.json"))
        assert len(remaining) == 5  # requests themselves are never deleted

    def test_all_duplicates_drain_then_no_request_proposer_fires(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "_task_already_done", lambda *_a, **_k: True)

        calls = []
        monkeypatch.setattr(
            llm_proposer, "maybe_propose", lambda *a, **k: calls.append((a, k)) or False
        )

        for i in range(3):
            _seed_bridge_request(state_dir, f"req-dup-{i}", f"cycle-dup-{i}", task_title=f"dup task {i}")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        # All 3 skipped in one run (well under the default cap of 10), then the
        # queue is empty — the existing no-pending-request proposer hook fires,
        # exactly once (not once per skip).
        markers = list((state_dir / "subagent_bridge").glob("handled_*.txt"))
        assert len(markers) == 3
        assert len(calls) == 1


class TestMaxSkipsEnvParsing:
    def _reload_with_env(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN", raising=False)
        else:
            monkeypatch.setenv("SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN", value)
        importlib.reload(bridge)
        return bridge.MAX_SKIPS_PER_RUN

    def test_unset_defaults_to_ten(self, monkeypatch):
        try:
            assert self._reload_with_env(monkeypatch, None) == 10
        finally:
            importlib.reload(bridge)

    def test_invalid_value_defaults_to_ten(self, monkeypatch):
        try:
            assert self._reload_with_env(monkeypatch, "not-a-number") == 10
        finally:
            importlib.reload(bridge)

    def test_negative_value_defaults_to_ten(self, monkeypatch):
        try:
            assert self._reload_with_env(monkeypatch, "-3") == 10
        finally:
            importlib.reload(bridge)

    def test_zero_defaults_to_ten(self, monkeypatch):
        try:
            assert self._reload_with_env(monkeypatch, "0") == 10
        finally:
            importlib.reload(bridge)

    def test_valid_value_honored(self, monkeypatch):
        try:
            assert self._reload_with_env(monkeypatch, "3") == 3
        finally:
            importlib.reload(bridge)
