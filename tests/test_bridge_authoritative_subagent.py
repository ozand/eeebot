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
from tests.test_bridge_executor_llm_error import _stub_planning_session
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
def _core_smoke_set(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    # ADR-034 rule 3: should_propose/build_context/bridge.py's executor
    # gate all now hard-require a real release charter to proceed.
    _adr034_release_root = tmp_path / "_adr034_release_root"
    _adr034_release_root.mkdir(exist_ok=True)
    (_adr034_release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", _adr034_release_root)


class TestAuthoritativeSpawnEndToEnd:
    def test_repair_turn_that_succeeds_is_read_not_the_stale_primary(self, tmp_path, monkeypatch):
        state_dir = _wire(tmp_path, monkeypatch, _make_repair_manager("ok", REPAIR_TEXT_WITH_MARKER))
        _seed_bridge_request(state_dir, "req-repair-ok", "cycle-repair-ok")
        _stub_planning_session(monkeypatch, "add feature")

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
        _stub_planning_session(monkeypatch, "add feature")

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


def _make_committing_repair_manager(status: str, result: str):
    """Unlike ``_make_repair_manager`` above, this repair spawn also commits
    a REAL file change -- round 3 item 1 (architect resolution
    2026-09-26): "its commits integrate only if the repair session's OWN
    telemetry has status=ok. Otherwise the cycle is unfinished." Round 2's
    barrier only ever read the PRIMARY spawn's status; a repair session
    that commits real work but ends in anything other than ``ok`` must
    still block the whole cycle from integrating.
    """
    class _CommittingRepairManager:
        task_id = REPAIR_TASK_ID

        def __init__(self, *, workspace, **_kwargs):
            self.workspace = workspace
            self._running_tasks: dict = {}
            self._skill_reads_this_cycle: list = []

        async def spawn(self, **_kwargs):
            (self.workspace / "scripts").mkdir(exist_ok=True)
            (self.workspace / "scripts" / "repair_fix.py").write_text("def fix():\n    return True\n")
            _run(self.workspace, "add", "scripts/repair_fix.py")
            _run(self.workspace, "commit", "-m", "fix: repair turn commits a partial fix")
            _write_telemetry(bridge.STATE_DIR, self.task_id, status, result)

            async def _done():
                return None

            self._running_tasks[self.task_id] = asyncio.ensure_future(_done())
            return "fake repair spawned"

    return _CommittingRepairManager


class TestUnfinishedRepairNeverIntegrates:
    def test_repair_commit_with_non_ok_status_blocks_the_whole_cycle(self, tmp_path, monkeypatch):
        """Static execution path the re-check named: primary executor
        finishes normally (status ok) -> initial smoke fails -> repair
        commits a partial fix and ends with status 'error' -> smoke now
        passes -> integration must NOT be reachable, for either the
        repair's commit or the primary's own otherwise-finished work.
        """
        import subprocess

        repair_cls = _make_committing_repair_manager("error", "Error: repair turn crashed mid-fix")
        state_dir = _wire(tmp_path, monkeypatch, repair_cls)
        _seed_bridge_request(state_dir, "req-repair-unfinished", "cycle-repair-unfinished")
        _stub_planning_session(monkeypatch, "add feature")

        rc = asyncio.run(bridge._main_impl())
        assert rc == 0

        work = tmp_path / "eeebot-self-evolving"
        main_tree = subprocess.run(
            ["git", "-C", str(work), "ls-tree", "-r", "--name-only", "main"],
            capture_output=True, text=True,
        ).stdout
        assert "scripts/repair_fix.py" not in main_tree, (
            "an unfinished repair session's own commit must never integrate"
        )
        assert "scripts/feature.py" not in main_tree, (
            "the whole cycle must not integrate when the repair session never finished, "
            "even though the primary executor's own status was ok"
        )

        from nanobot.runtime import open_increment

        pending = open_increment.pending_open_increment(state_dir)
        assert pending is not None, "an unfinished repair must leave the branch as a pending open increment"


def _make_amending_repair_manager(status: str, result: str):
    """Round 4 P1-b (architect resolution 2026-09-26): a repair turn that
    runs ``git commit --amend`` changes the branch tip WITHOUT growing
    the commit count -- ``_repair_added_commits`` (round 3 item 1) keyed
    purely on ``rev-list --count`` growth, so this shape slipped past the
    barrier entirely: the amended tip could integrate even with a
    non-``ok`` repair status.
    """
    class _AmendingRepairManager:
        task_id = REPAIR_TASK_ID

        def __init__(self, *, workspace, **_kwargs):
            self.workspace = workspace
            self._running_tasks: dict = {}
            self._skill_reads_this_cycle: list = []

        async def spawn(self, **_kwargs):
            (self.workspace / "scripts" / "feature.py").write_text(
                "def feature():\n    return 43  # repaired\n"
            )
            _run(self.workspace, "add", "scripts/feature.py")
            _run(self.workspace, "commit", "--amend", "--no-edit")
            _write_telemetry(bridge.STATE_DIR, self.task_id, status, result)

            async def _done():
                return None

            self._running_tasks[self.task_id] = asyncio.ensure_future(_done())
            return "fake repair spawned"

    return _AmendingRepairManager


class TestUnfinishedAmendedRepairNeverIntegrates:
    def test_repair_amend_with_non_ok_status_blocks_the_whole_cycle(self, tmp_path, monkeypatch):
        """The repair turn amends the primary's OWN commit (same commit
        count, new tip sha) and ends with a non-'ok' status -- the
        barrier must still catch it, exactly as it does a repair that
        ADDS a new commit.
        """
        import subprocess

        repair_cls = _make_amending_repair_manager("error", "Error: repair turn crashed mid-fix")
        state_dir = _wire(tmp_path, monkeypatch, repair_cls)
        _seed_bridge_request(state_dir, "req-repair-amend-unfinished", "cycle-repair-amend-unfinished")
        _stub_planning_session(monkeypatch, "add feature")

        rc = asyncio.run(bridge._main_impl())
        assert rc == 0

        work = tmp_path / "eeebot-self-evolving"
        main_blob = subprocess.run(
            ["git", "-C", str(work), "show", "main:scripts/feature.py"],
            capture_output=True, text=True,
        )
        assert main_blob.returncode != 0 or "repaired" not in main_blob.stdout, (
            "an unfinished (amended) repair session's own commit must never integrate"
        )

        from nanobot.runtime import open_increment

        pending = open_increment.pending_open_increment(state_dir)
        assert pending is not None, (
            "an unfinished amended repair must leave the branch as a pending open increment"
        )


class TestUnfinishedAmendedRepairWithFailedTipReadNeverIntegrates:
    def test_one_failed_sha_read_still_blocks_an_unfinished_amend(self, tmp_path, monkeypatch):
        """Round 5 external re-check, item N1 (architect resolution
        2026-09-26): ``_current_tip_sha()`` returns ``''`` on a git
        failure or exception, and the old comparison
        (``bool(before) and bool(after) and before != after``) required
        BOTH reads to be truthy before it would even consider them
        different -- so a single failed read (before OR after) made
        ``_repair_tip_changed`` read False, i.e. "unchanged", exactly
        the same as a genuinely no-op repair. Combined with an amend
        (which never grows the commit count), the barrier never fires
        even though the repair's own status is not 'ok'. One of the two
        tip-sha reads is forced to fail here; the amend and the non-ok
        telemetry are both real, same as the ordinary amend test above.
        """
        import subprocess

        repair_cls = _make_amending_repair_manager("error", "Error: repair turn crashed mid-fix")
        state_dir = _wire(tmp_path, monkeypatch, repair_cls)
        _seed_bridge_request(state_dir, "req-repair-amend-tipreadfail", "cycle-repair-amend-tipreadfail")
        _stub_planning_session(monkeypatch, "add feature")

        _real_tip_sha = bridge._current_tip_sha
        _tip_calls = {"n": 0}

        def _flaky_tip_sha(repo):
            _tip_calls["n"] += 1
            if _tip_calls["n"] == 1:
                # Simulates a transient git failure on the "before" read --
                # the real repo state is untouched, only this ONE read fails.
                return ""
            return _real_tip_sha(repo)

        monkeypatch.setattr(bridge, "_current_tip_sha", _flaky_tip_sha)

        rc = asyncio.run(bridge._main_impl())
        assert rc == 0

        work = tmp_path / "eeebot-self-evolving"
        main_blob = subprocess.run(
            ["git", "-C", str(work), "show", "main:scripts/feature.py"],
            capture_output=True, text=True,
        )
        assert main_blob.returncode != 0 or "repaired" not in main_blob.stdout, (
            "a failed tip-sha read must never turn an unfinished amend into a verified no-op -- "
            "the unfinished repair must never integrate"
        )

        from nanobot.runtime import open_increment

        pending = open_increment.pending_open_increment(state_dir)
        assert pending is not None, (
            "an unverifiable (failed tip read) repair must leave the branch as a pending open increment, "
            "not be silently treated as unchanged"
        )


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
