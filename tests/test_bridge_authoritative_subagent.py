"""#1546: the citation scan must read whichever spawn's answer is actually
authoritative for a cycle — not always the primary spawn's own result.

``bridge.py`` captures ``_subagent_task_id`` once, right after the primary
spawn (#1118), and until this fix that value was also what the citation scan
read even when the cycle went through the repair/revision loop. A repair
spawn that actually fixes the failing gate is a later, more complete answer
than the primary's; a repair spawn that times out or errors is not the
cycle's real final answer at all. Two full ``bridge._main_impl()`` runs,
each forcing exactly one smoke failure then one repair turn, prove both
directions non-vacuously: the repair's answer is read when it is the one
that actually completed, and the primary's answer is read (not a cancelled
stub) when the repair did not.

``TestHelpers`` below unit-tests the resolution function directly, mirroring
``tests/test_bridge_executor_llm_error.py``'s ``TestHelpers`` pattern.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.runtime import bridge
from tests.test_cycle_ledger import _init_selfevo_repo, _run, _seed_bridge_request

PRIMARY_TASK_ID = "acf80d1f"
REPAIR_TASK_ID = "a6ca8f4c"
PRIMARY_TEXT = "Pruned directory traversal and added a pre-filter. Work is done; no lesson applied here."
REPAIR_TEXT_WITH_MARKER = "Fixed the failing test by widening the timeout. Applied [Lesson LESS-REPAIR]."
REPAIR_TEXT_CANCELLED = "Cancelled before completion."


def _write_telemetry(state_dir, task_id, status, result):
    telemetry_dir = state_dir / "subagents"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    (telemetry_dir / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "status": status, "result": result}),
        encoding="utf-8",
    )


class _PrimaryManager:
    """Primary spawn: commits a real change (so the gate has something to
    evaluate) and writes a substantive, non-citing ``status: ok`` result.
    """

    task_id = PRIMARY_TASK_ID

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace
        self._running_tasks: dict = {}
        self._skill_reads_this_cycle: list = []

    async def spawn(self, **_kwargs):
        (self.workspace / "scripts").mkdir(exist_ok=True)
        (self.workspace / "scripts" / "feature.py").write_text("def feature():\n    return 42\n")
        _run(self.workspace, "add", "scripts/feature.py")
        _run(self.workspace, "commit", "-m", "feat: add feature")
        _write_telemetry(bridge.STATE_DIR, self.task_id, "ok", PRIMARY_TEXT)

        async def _done():
            return None

        self._running_tasks[self.task_id] = asyncio.ensure_future(_done())
        return "fake primary spawned"


def _make_repair_manager(status: str, result: str):
    class _RepairManager:
        task_id = REPAIR_TASK_ID

        def __init__(self, *, workspace, **_kwargs):
            self.workspace = workspace
            self._running_tasks: dict = {}
            self._skill_reads_this_cycle: list = []

        async def spawn(self, **_kwargs):
            _write_telemetry(bridge.STATE_DIR, self.task_id, status, result)

            async def _done():
                return None

            self._running_tasks[self.task_id] = asyncio.ensure_future(_done())
            return "fake repair spawned"

    return _RepairManager


def _fail_once_then_pass():
    calls = {"n": 0}

    def _fake(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (False, "pytest timed out")
        return (True, "")

    return _fake


def _wire(tmp_path, monkeypatch, repair_manager_cls):
    base = tmp_path
    state_dir = base / "state"
    state_dir.mkdir()
    _init_selfevo_repo(base)
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", _PrimaryManager)
    # #1546: the repair loop imports SubagentManager fresh from
    # nanobot.agent.subagent (`from nanobot.agent.subagent import
    # SubagentManager as _SM2`) — patching bridge.SubagentManager alone does
    # NOT reach it.
    import nanobot.agent.subagent as subagent_module
    monkeypatch.setattr(subagent_module, "SubagentManager", repair_manager_cls)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    monkeypatch.setattr(bridge, "_run_smoke_tests_with_shrink_guard", _fail_once_then_pass())
    return state_dir


def _last_scan_row(state_dir):
    path = state_dir / "lesson_usage" / "scans.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(lines[-1])


@pytest.fixture(autouse=True)
def _core_smoke_set(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


class TestAuthoritativeSpawnEndToEnd:
    def test_repair_turn_that_succeeds_is_read_not_the_stale_primary(self, tmp_path, monkeypatch):
        state_dir = _wire(tmp_path, monkeypatch, _make_repair_manager("ok", REPAIR_TEXT_WITH_MARKER))
        _seed_bridge_request(state_dir, "req-repair-ok", "cycle-repair-ok")

        rc = asyncio.run(bridge._main_impl())

        assert rc == 0
        row = _last_scan_row(state_dir)
        assert row["status"] == "complete"
        assert row["marker_count"] == 1
        assert row["lesson_ids"] == ["LESS-REPAIR"]
        assert row["scanned_chars"] == len(REPAIR_TEXT_WITH_MARKER)

    def test_repair_turn_that_fails_falls_back_to_the_primary_not_its_stub(self, tmp_path, monkeypatch):
        state_dir = _wire(tmp_path, monkeypatch, _make_repair_manager("cancelled", REPAIR_TEXT_CANCELLED))
        _seed_bridge_request(state_dir, "req-repair-cancelled", "cycle-repair-cancelled")

        rc = asyncio.run(bridge._main_impl())

        assert rc == 0
        row = _last_scan_row(state_dir)
        # The line to hold (#1546): a substantive final answer with
        # genuinely no citation must still read complete/marker_count 0 —
        # scanned from the PRIMARY's real text, not the cancelled repair's
        # 28-character stub.
        assert row["status"] == "complete"
        assert row["marker_count"] == 0
        assert row["scanned_chars"] == len(PRIMARY_TEXT)
        assert row["scanned_chars"] != len(REPAIR_TEXT_CANCELLED)


class TestHelpers:
    def test_authoritative_resolution_prefers_latest_ok_repair(self, tmp_path):
        _write_telemetry(tmp_path, "primary", "ok", PRIMARY_TEXT)
        _write_telemetry(tmp_path, "repair-1", "cancelled", REPAIR_TEXT_CANCELLED)
        _write_telemetry(tmp_path, "repair-2", "ok", REPAIR_TEXT_WITH_MARKER)
        assert bridge._authoritative_subagent_task_id(
            tmp_path, "primary", ["repair-1", "repair-2"],
        ) == "repair-2"

    def test_authoritative_resolution_falls_back_to_primary_when_no_repair_ok(self, tmp_path):
        _write_telemetry(tmp_path, "primary", "ok", PRIMARY_TEXT)
        _write_telemetry(tmp_path, "repair-1", "error", "Error: boom")
        _write_telemetry(tmp_path, "repair-2", "cancelled", REPAIR_TEXT_CANCELLED)
        assert bridge._authoritative_subagent_task_id(
            tmp_path, "primary", ["repair-1", "repair-2"],
        ) == "primary"

    def test_authoritative_resolution_with_no_repairs_is_the_primary(self, tmp_path):
        _write_telemetry(tmp_path, "primary", "ok", PRIMARY_TEXT)
        assert bridge._authoritative_subagent_task_id(tmp_path, "primary", []) == "primary"

    def test_authoritative_resolution_unreadable_repair_status_is_not_ok(self, tmp_path):
        _write_telemetry(tmp_path, "primary", "ok", PRIMARY_TEXT)
        (tmp_path / "subagents").mkdir(parents=True, exist_ok=True)
        (tmp_path / "subagents" / "repair-1.json").write_text("{not json", encoding="utf-8")
        assert bridge._authoritative_subagent_task_id(
            tmp_path, "primary", ["repair-1"],
        ) == "primary"

    def test_subagent_own_status_reads_only_the_status_field(self, tmp_path):
        _write_telemetry(tmp_path, "ok-task", "ok", PRIMARY_TEXT)
        assert bridge._subagent_own_status(tmp_path, "ok-task") == "ok"
        assert bridge._subagent_own_status(tmp_path, "missing") == ""
        assert bridge._subagent_own_status(tmp_path, None) == ""
