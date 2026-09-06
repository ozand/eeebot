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

Note (#1333): the fuzzy git-log gate (_task_already_done) was retired; these
tests now use _recent_failure_match as the skip trigger for dup requests
(an active gate that exercises the same bulk-skip loop path).

Reuses the bridge-integration harness from tests/test_cycle_ledger.py (bare
"origin" + working clone standing in for the shared ``eeebot-self-evolving``
checkout).
"""
from __future__ import annotations

import asyncio
import importlib
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
        # Duplicates are any request whose task_title starts with "dup" — simulate
        # via _recent_failure_match (active gate; exercises the same bulk-skip path).
        monkeypatch.setattr(
            bridge, "_recent_failure_match",
            lambda title, *_a, **_k: title if title.startswith("dup") else None,
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
        # All requests are dup — simulate via _recent_failure_match (active gate).
        monkeypatch.setattr(bridge, "_recent_failure_match", lambda title, *_a, **_k: title or "dup")
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
        # All requests are dup — simulate via _recent_failure_match (active gate).
        monkeypatch.setattr(bridge, "_recent_failure_match", lambda title, *_a, **_k: title or "dup")

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


class TestMarkerWedgeDoesNotCapRunAtOneSkip:
    """Live-found #733 follow-up: find_pending_request can return a request
    whose handled marker exists under the SANITIZED request_id (the marker
    filename uses ``safe_id = request_id.replace('/', '_')``) while its own
    filter compares the RAW request_id/marker stem — so a request whose raw
    id contains sanitized characters slips the filter and is returned again
    and again. The old marker-exists branch did ``return 0``, so one wedged
    request near the queue head capped the whole run at one skip.
    """

    def test_wedged_request_stepped_over_via_continue(self, tmp_path, monkeypatch):
        """queue = [dup, wedged (marker exists, but find_pending_request still
        returns it), dup, novel] processed in ONE run: both dups skipped, the
        wedged request is stepped over via `continue` (not `return 0`), and
        the novel request spawns — matching the live-found bulk-skip
        expectation of at most one spawn per run.

        find_pending_request itself is stubbed here (not exercised) so this
        test isolates the loop's OWN defense-in-depth (the marker-exists
        `continue`) from the find_pending_request filter fix covered by
        TestFindPendingRequestSanitizedMarkerFilter below.
        """
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        bridge_state_dir = state_dir / "subagent_bridge"
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", bridge_state_dir)
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        monkeypatch.setattr(
            bridge, "_recent_failure_match",
            lambda title, *_a, **_k: title if title.startswith("dup") else None,
        )

        spawn_calls = []
        real_spawn = _FakeSubagentManager.spawn

        async def _counting_spawn(self, **kwargs):
            spawn_calls.append(1)
            return await real_spawn(self, **kwargs)

        monkeypatch.setattr(_FakeSubagentManager, "spawn", _counting_spawn)

        _seed_bridge_request(state_dir, "req-dup-1", "cycle-dup-1", task_title="dup task alpha")
        # Wedged request: raw request_id contains '/', its handled marker was
        # filed (by a hypothetical prior run) under the SANITIZED id — but a
        # future sanitization edge case could make find_pending_request return
        # it anyway. Content isn't relevant here since find_pending_request is
        # stubbed; the marker file existing is what the loop must catch.
        wedged_req_id = "wedge/task/one"
        wedged_cycle_id = "cycle-wedge-1"
        wedged_path = state_dir / "subagents" / "requests" / "request-wedge.json"
        wedged_path.parent.mkdir(parents=True, exist_ok=True)
        wedged_req = {
            "request_id": wedged_req_id,
            "cycle_id": wedged_cycle_id,
            "task_title": "mismatched marker task",
        }
        wedged_path.write_text(json.dumps(wedged_req), encoding="utf-8")
        bridge_state_dir.mkdir(parents=True, exist_ok=True)
        safe_wedged_id = wedged_req_id.replace("/", "_")[:120]
        (bridge_state_dir / f"handled_{safe_wedged_id}.txt").write_text(
            str(wedged_path), encoding="utf-8",
        )

        _seed_bridge_request(state_dir, "req-dup-2", "cycle-dup-2", task_title="dup task beta")
        _seed_bridge_request(state_dir, "req-novel", "cycle-novel", task_title="totally novel feature widget")

        dup1_path = state_dir / "subagents" / "requests" / "req-dup-1.json"
        dup2_path = state_dir / "subagents" / "requests" / "req-dup-2.json"
        novel_path = state_dir / "subagents" / "requests" / "req-novel.json"

        _sequence = [dup1_path, wedged_path, dup2_path, novel_path]
        _calls = {"n": 0}

        def _fake_find_pending_request():
            idx = _calls["n"]
            _calls["n"] += 1
            if idx >= len(_sequence):
                return None, {}
            path = _sequence[idx]
            req = json.loads(path.read_text(encoding="utf-8"))
            return path, req

        monkeypatch.setattr(bridge, "find_pending_request", _fake_find_pending_request)

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        # Exactly one spawn for the whole run (S6 invariant), for the novel request.
        assert len(spawn_calls) == 1

        rows = _read_ledger(state_dir)
        for cid in ("cycle-dup-1", "cycle-dup-2"):
            crows = _rows_for_cycle(rows, cid)
            assert crows[-1]["outcome"] == "skipped-duplicate"

        # The wedged request was stepped over (continue), not treated as a
        # run-ending event — no ledger row was written for it at all (the
        # marker-exists branch just prints and continues, same as before #733).
        wedged_rows = _rows_for_cycle(rows, wedged_cycle_id)
        assert wedged_rows == []

        novel_rows = _rows_for_cycle(rows, "cycle-novel")
        assert novel_rows[-1]["outcome"] == "success"


class TestSamePathReturnedTwiceEndsRunCleanly:
    def test_same_path_repeated_breaks_loop(self, tmp_path, monkeypatch):
        """If find_pending_request keeps returning the SAME request path
        (e.g. a future sanitization edge case the marker-exists branch alone
        doesn't catch), the loop must not spin forever — it ends the run
        cleanly the second time the same path comes back.
        """
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(
            bridge, "_recent_failure_match",
            lambda title, *_a, **_k: title if title.startswith("dup") else None,
        )

        _seed_bridge_request(state_dir, "req-dup-1", "cycle-dup-1", task_title="dup task alpha")
        dup1_path = state_dir / "subagents" / "requests" / "req-dup-1.json"
        dup1_req = json.loads(dup1_path.read_text(encoding="utf-8"))

        _calls = {"n": 0}

        def _fake_find_pending_request():
            _calls["n"] += 1
            return dup1_path, dup1_req

        monkeypatch.setattr(bridge, "find_pending_request", _fake_find_pending_request)

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        # find_pending_request was called exactly twice: once to get the
        # request, once more to discover it's the same path again — at which
        # point the run ends instead of looping forever.
        assert _calls["n"] == 2

        rows = _read_ledger(state_dir)
        crows = _rows_for_cycle(rows, "cycle-dup-1")
        assert crows[-1]["outcome"] == "skipped-duplicate"

        markers = list((state_dir / "subagent_bridge").glob("handled_*.txt"))
        assert len(markers) == 1


class TestFindPendingRequestSanitizedMarkerFilter:
    """Root-cause hardening: find_pending_request's real_handled filter now
    also compares the SANITIZED form of each candidate's request_id, matching
    how the bridge names its handled markers (``safe_id =
    request_id.replace('/', '_')[:120]``). A request whose marker exists
    under the sanitized id must not be returned at all, independent of the
    loop-level defenses above.
    """

    def test_marker_under_sanitized_id_filters_out_raw_id_request(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        bridge_state_dir = state_dir / "subagent_bridge"
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", bridge_state_dir)

        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        raw_request_id = "task/with/slash"
        req_path = req_dir / "request-x.json"
        req_path.write_text(
            json.dumps({"request_id": raw_request_id, "cycle_id": "cycle-x", "status": "queued"}),
            encoding="utf-8",
        )

        bridge_state_dir.mkdir(parents=True)
        safe_id = raw_request_id.replace("/", "_")[:120]
        (bridge_state_dir / f"handled_{safe_id}.txt").write_text(str(req_path), encoding="utf-8")

        result_path, result_req = bridge.find_pending_request()
        assert result_path is None
        assert result_req == {}
