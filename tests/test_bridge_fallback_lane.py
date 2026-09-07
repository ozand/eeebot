"""Tests for #1411: the bridge-side wiring of the executor-always-runs
fallback lane.

Covers the four trigger routes enumerated in the issue (no_demand,
already_done_tag, recent_duplicate_failure, existence_index_duplicate —
plus proposer_reject, which lands on the same code path as no_demand since
both are "llm_proposer.maybe_propose returned nothing this cycle"), that a
cycle with usable demand never touches the fallback lane, that a fallback
proposal still passes through the SAME duplicate-suppression gates a
demand-driven request does, that a surviving fallback proposal actually
spawns an executor in the SAME bridge run, that at most one fallback
attempt happens per run regardless of which/how-many trigger routes fire,
and that the label survives to the terminal outcome row.

Reuses the bridge-integration harness from tests/test_cycle_ledger.py, same
as tests/test_bridge_bulk_skip.py.
"""
from __future__ import annotations

import asyncio
import json

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
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


def _fallback_request(state_dir, cycle_id="fallback-abc123def456", task_title="fallback task widget"):
    """Build a (path, req) pair shaped like propose_fallback's real return
    value — a real file on disk, same as write_request(..., lane='fallback')
    would produce, so the bridge's own req_path.write_text(handled marker)
    behaves identically to production."""
    req_dir = state_dir / "subagents" / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    req = {
        "schema_version": "subagent-request-v1",
        "cycle_id": cycle_id,
        "request_id": f"llm-proposer-{cycle_id}",
        "task_title": task_title,
        "task": f"Fallback-proposed: {task_title}",
        "request_status": "queued",
        "lane": "fallback",
    }
    path = req_dir / f"request-{cycle_id}.json"
    path.write_text(json.dumps(req), encoding="utf-8")
    return path, req


def _rows_for_cycle(rows: list[dict], cycle_id: str) -> list[dict]:
    return [r for r in rows if r.get("cycle_id") == cycle_id]


def _base_setup(base, monkeypatch, *, with_repo: bool = False):
    state_dir = base / "state"
    state_dir.mkdir()
    if with_repo:
        _init_selfevo_repo(base)
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    # _main_impl_body returns 0 at "no_active_goal" before ever reaching the
    # bulk-skip loop unless an active goal is configured — every scenario
    # here needs to get past that gate, seeded requests or not.
    (state_dir / "goals").mkdir(parents=True, exist_ok=True)
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": "goal-1", "text": "test goal"}),
        encoding="utf-8",
    )
    return state_dir


class TestDemandPresentNeverTouchesFallback:
    def test_novel_request_spawns_without_fallback_attempt(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch, with_repo=True)
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        calls = []
        monkeypatch.setattr(
            llm_proposer, "propose_fallback",
            lambda *a, **k: calls.append(1) or None,
        )

        _seed_bridge_request(state_dir, "req-novel", "cycle-novel", task_title="totally novel feature widget")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        assert calls == []
        rows = _read_ledger(state_dir)
        novel_rows = _rows_for_cycle(rows, "cycle-novel")
        assert novel_rows[-1]["outcome"] == "success"
        assert "lane" not in novel_rows[-1]


class TestTriggerRoutes:
    """Each test drives the bridge into exactly one of the enumerated
    no-executor-run routes and asserts the fallback lane was attempted."""

    def test_no_demand_route(self, tmp_path, monkeypatch):
        """Empty queue at the very start (find_pending_request returns
        None) AND llm_proposer.maybe_propose has nothing to queue (the
        real default: SELFEVO_LLM_PROPOSER_ENABLED is off) — should_propose
        never even fires an LLM call. bridge.py:2318's `if not req_path`."""
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch)

        calls = []
        monkeypatch.setattr(
            llm_proposer, "propose_fallback",
            lambda *a, **k: calls.append(1) or None,
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(calls) == 1

    def test_proposer_reject_route_lands_on_the_same_branch(self, tmp_path, monkeypatch):
        """A demand-driven proposer that ran an LLM call and rejected its
        own output (sizing/self-dedup) also returns None from
        maybe_propose — same `if not req_path` branch as no_demand."""
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch)

        maybe_propose_calls = []
        fallback_calls = []
        monkeypatch.setattr(
            llm_proposer, "maybe_propose",
            lambda *a, **k: maybe_propose_calls.append(1) or None,
        )
        monkeypatch.setattr(
            llm_proposer, "propose_fallback",
            lambda *a, **k: fallback_calls.append(1) or None,
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(maybe_propose_calls) == 1
        assert len(fallback_calls) == 1

    def test_already_done_tag_route(self, tmp_path, monkeypatch):
        """bridge.py's exact-tag dedup gate: a request whose cycle_id
        already carries a `cycle-<id>-success` tag. Skip cap set to 1 so
        the single skip immediately ends the run at the fallback hook."""
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch, with_repo=True)
        monkeypatch.setattr(bridge, "MAX_SKIPS_PER_RUN", 1)
        monkeypatch.setattr(bridge, "_cycle_tag_exists", lambda *_a, **_k: True)

        calls = []
        monkeypatch.setattr(
            llm_proposer, "propose_fallback",
            lambda *a, **k: calls.append(1) or None,
        )
        monkeypatch.setattr(bridge, "_maybe_propose_after_skip", lambda *a, **k: None)

        _seed_bridge_request(state_dir, "req-done", "cycle-done", task_title="already done task")

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(calls) == 1

        rows = _read_ledger(state_dir)
        crows = _rows_for_cycle(rows, "cycle-done")
        assert crows[-1]["outcome"] == "skipped-duplicate"
        assert crows[-1]["reason"] == "already_done_tag"

    def test_recent_duplicate_failure_route(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch, with_repo=True)
        monkeypatch.setattr(bridge, "MAX_SKIPS_PER_RUN", 1)
        monkeypatch.setattr(bridge, "_recent_failure_match", lambda title, *_a, **_k: title or "dup")

        calls = []
        monkeypatch.setattr(
            llm_proposer, "propose_fallback",
            lambda *a, **k: calls.append(1) or None,
        )
        monkeypatch.setattr(bridge, "_maybe_propose_after_skip", lambda *a, **k: None)

        _seed_bridge_request(state_dir, "req-dup", "cycle-dup", task_title="dup task alpha")

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(calls) == 1

        rows = _read_ledger(state_dir)
        crows = _rows_for_cycle(rows, "cycle-dup")
        assert crows[-1]["outcome"] == "skipped-duplicate"
        assert crows[-1]["reason"] == "recent_duplicate_failure"

    def test_existence_index_duplicate_route(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch, with_repo=True)
        monkeypatch.setattr(bridge, "MAX_SKIPS_PER_RUN", 1)
        monkeypatch.setattr(bridge, "_recent_failure_match", lambda *_a, **_k: None)
        monkeypatch.setattr(bridge, "find_duplicate_script", lambda *_a, **_k: "scripts/existing.py")

        calls = []
        monkeypatch.setattr(
            llm_proposer, "propose_fallback",
            lambda *a, **k: calls.append(1) or None,
        )
        monkeypatch.setattr(bridge, "_maybe_propose_after_skip", lambda *a, **k: None)

        _seed_bridge_request(state_dir, "req-existing", "cycle-existing", task_title="near duplicate script")

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(calls) == 1

        rows = _read_ledger(state_dir)
        crows = _rows_for_cycle(rows, "cycle-existing")
        assert crows[-1]["outcome"] == "skipped-duplicate"
        assert crows[-1]["reason"] == "existence_index_duplicate"


class TestFallbackOutputStillSuppressed:
    def test_fallback_proposal_matching_recent_failure_is_suppressed_labelled(self, tmp_path, monkeypatch):
        """The fallback lane invents a task; it does not bypass the gates.
        Its output re-enters the SAME bulk-skip loop and can itself be
        caught by _recent_failure_match — recorded skipped-duplicate, with
        lane="fallback" surviving to the outcome row."""
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch, with_repo=True)

        fallback_path, fallback_req = _fallback_request(state_dir, task_title="dup fallback task")
        monkeypatch.setattr(
            llm_proposer, "propose_fallback",
            lambda *a, **k: (fallback_path, fallback_req),
        )
        # Everything titled "dup..." is a recent-failure match (same
        # convention as test_bridge_bulk_skip.py).
        monkeypatch.setattr(bridge, "_recent_failure_match", lambda title, *_a, **_k: title if title.startswith("dup") else None)

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        crows = _rows_for_cycle(rows, fallback_req["cycle_id"])
        assert crows[-1]["outcome"] == "skipped-duplicate"
        assert crows[-1]["reason"] == "recent_duplicate_failure"
        assert crows[-1]["lane"] == "fallback"


class TestFallbackOutputSpawnsWhenNovel:
    def test_surviving_fallback_proposal_spawns_executor_same_run(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch, with_repo=True)
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        monkeypatch.setattr(bridge, "_recent_failure_match", lambda *_a, **_k: None)
        monkeypatch.setattr(bridge, "find_duplicate_script", lambda *_a, **_k: None)

        fallback_path, fallback_req = _fallback_request(state_dir, task_title="novel fallback feature")
        monkeypatch.setattr(
            llm_proposer, "propose_fallback",
            lambda *a, **k: (fallback_path, fallback_req),
        )

        spawn_calls = []
        real_spawn = _FakeSubagentManager.spawn

        async def _counting_spawn(self, **kwargs):
            spawn_calls.append(1)
            return await real_spawn(self, **kwargs)

        monkeypatch.setattr(_FakeSubagentManager, "spawn", _counting_spawn)

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(spawn_calls) == 1

        rows = _read_ledger(state_dir)
        crows = _rows_for_cycle(rows, fallback_req["cycle_id"])
        assert crows[-1]["outcome"] == "success"
        assert crows[-1]["lane"] == "fallback"


class TestAtMostOneFallbackProposalPerCycle:
    def test_fallback_attempt_capped_at_one_even_when_its_own_output_hits_the_same_dead_end(self, tmp_path, monkeypatch):
        """The queue starts empty (no_demand route fires, attempt #1). The
        fallback proposal it produces is ITSELF a recent-failure duplicate,
        so the run ends at a second dead end — but propose_fallback must
        not be called again."""
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch, with_repo=True)

        fallback_path, fallback_req = _fallback_request(state_dir, task_title="dup fallback task")
        calls = []

        def _fake_propose_fallback(*a, **k):
            calls.append(1)
            return (fallback_path, fallback_req)

        monkeypatch.setattr(llm_proposer, "propose_fallback", _fake_propose_fallback)
        monkeypatch.setattr(bridge, "_recent_failure_match", lambda title, *_a, **_k: title if title.startswith("dup") else None)

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(calls) == 1

    def test_fallback_attempt_capped_at_one_across_skip_cap_and_empty_queue(self, tmp_path, monkeypatch):
        """One real duplicate request hits the skip-cap trigger (attempt
        would happen there); if that attempt's own output ALSO gets
        skipped and the queue then drains to empty, the no_demand branch
        must not attempt a second time."""
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch, with_repo=True)
        monkeypatch.setattr(bridge, "MAX_SKIPS_PER_RUN", 1)
        monkeypatch.setattr(bridge, "_maybe_propose_after_skip", lambda *a, **k: None)
        monkeypatch.setattr(bridge, "_recent_failure_match", lambda title, *_a, **_k: title if title.startswith("dup") else None)

        fallback_path, fallback_req = _fallback_request(state_dir, task_title="dup fallback task")
        calls = []

        def _fake_propose_fallback(*a, **k):
            calls.append(1)
            return (fallback_path, fallback_req)

        monkeypatch.setattr(llm_proposer, "propose_fallback", _fake_propose_fallback)

        _seed_bridge_request(state_dir, "req-dup", "cycle-dup", task_title="dup task alpha")

        result = asyncio.run(bridge._main_impl())
        assert result == 0
        assert len(calls) == 1


class TestFallbackLaneDisabled:
    def test_kill_switch_off_never_calls_propose_fallback(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = _base_setup(base, monkeypatch)
        monkeypatch.setenv(llm_proposer.FALLBACK_LANE_ENABLED_ENV, "0")

        def _boom(*_a, **_k):
            raise AssertionError("propose_fallback must not run any LLM path when disabled")

        # Not patching propose_fallback itself here — exercising the real
        # function's own kill-switch short-circuit, called from the bridge.
        monkeypatch.setattr(llm_proposer, "build_context", _boom)
        monkeypatch.setattr(llm_proposer, "propose", _boom)

        result = asyncio.run(bridge._main_impl())
        assert result == 0
