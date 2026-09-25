"""Tests for session timing, progress watchdog, and max call gap recording (#1899).

Covers:
1. Wall clock safety margin: stops subagent before starting a new model call
   when remaining time is less than p99 call duration + final/gate budget (#1899).
2. Progress watchdog: aborts session when no model call or tool step completes
   for N minutes (default 10 min) (#1899).
3. Max call gap: tracks and writes the maximum gap between completed calls to the
   cycle ledger, making close misses observable (#1899).
"""
from __future__ import annotations

import asyncio
import json

from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime import cycle_ledger
from nanobot.runtime.session_clock import (
    DEFAULT_CALL_P99_SECS,
    DEFAULT_FINAL_BUDGET_SECS,
    DEFAULT_PROGRESS_TIMEOUT_SECS,
    ProgressWatchdog,
    compute_cycle_max_call_gap,
    get_call_p99_secs,
    get_final_budget_secs,
    get_progress_timeout_secs,
    get_wall_safety_margin_secs,
    should_stop_for_wall_clock,
)


class _MockProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse] | None = None, delay: float = 0.0):
        super().__init__()
        self.responses = list(responses or [])
        self.calls = 0
        self.delay = delay

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(content="done", tool_calls=[])


class TestWallClockSafetyMargin:
    """Requirement 1 (#1899): stop before starting model call when budget < p99 + final."""

    def test_defaults_derived_from_1899_measurement(self, monkeypatch):
        monkeypatch.delenv("NANOBOT_WALL_CALL_P99_SECS", raising=False)
        monkeypatch.delenv("NANOBOT_WALL_FINAL_BUDGET_SECS", raising=False)
        assert get_call_p99_secs() == DEFAULT_CALL_P99_SECS
        assert get_final_budget_secs() == DEFAULT_FINAL_BUDGET_SECS
        assert get_wall_safety_margin_secs() == DEFAULT_CALL_P99_SECS + DEFAULT_FINAL_BUDGET_SECS

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "120.0")
        monkeypatch.setenv("NANOBOT_WALL_FINAL_BUDGET_SECS", "180.0")
        assert get_call_p99_secs() == 120.0
        assert get_final_budget_secs() == 180.0
        assert get_wall_safety_margin_secs() == 300.0

    def test_should_stop_when_remaining_less_than_safety_margin(self):
        # Margin is 543s. If remaining time is 500s -> should stop.
        wall_deadline = 1000.0
        assert should_stop_for_wall_clock(wall_deadline, clock=lambda: 500.0) is True
        # If remaining time is 600s -> should NOT stop.
        assert should_stop_for_wall_clock(wall_deadline, clock=lambda: 400.0) is False

    def test_should_stop_none_deadline_returns_false(self):
        assert should_stop_for_wall_clock(None) is False

    async def test_subagent_stops_before_call_when_wall_budget_exhausted(self, tmp_path, monkeypatch):
        """Subagent stops with wall_clock_deadline without making call when margin reached (#1899)."""
        monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "50.0")
        monkeypatch.setenv("NANOBOT_WALL_FINAL_BUDGET_SECS", "50.0")
        # Safety margin is 100s.
        current_time = [100.0]

        def fake_clock():
            return current_time[0]

        # Deadline is 150s. At start time 100s, remaining = 50s < 100s margin.
        wall_deadline = 150.0

        provider = _MockProvider([LLMResponse(content="should not be called", tool_calls=[])])
        mgr = SubagentManager(
            provider=provider,
            workspace=tmp_path,
            bus=MessageBus(),
            max_iterations=10,
            wall_deadline=wall_deadline,
        )
        mgr._monotonic = fake_clock

        await mgr.spawn(task="test-task", label="wall_test")
        await asyncio.gather(*list(mgr._running_tasks.values()), return_exceptions=True)

        # Provider must NOT have been called
        assert provider.calls == 0

        # Telemetry must show wall_clock_deadline stop reason
        files = list((tmp_path / "state" / "subagents").glob("*.json"))
        assert files
        telem = json.loads(files[0].read_text(encoding="utf-8"))
        assert telem.get("stop_reason") == "wall_clock_deadline"
        assert telem["status"] == "bounded_stop"


class TestProgressWatchdog:
    """Requirement 2 (#1899): watchdog interrupts session when no progress for N minutes."""

    def test_env_defaults_and_overrides(self, monkeypatch):
        monkeypatch.delenv("NANOBOT_PROGRESS_TIMEOUT_SECS", raising=False)
        monkeypatch.delenv("NANOBOT_PROGRESS_TIMEOUT_MINUTES", raising=False)
        assert get_progress_timeout_secs() == DEFAULT_PROGRESS_TIMEOUT_SECS

        monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "300.0")
        assert get_progress_timeout_secs() == 300.0

        monkeypatch.delenv("NANOBOT_PROGRESS_TIMEOUT_SECS", raising=False)
        monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_MINUTES", "5")
        assert get_progress_timeout_secs() == 300.0

    def test_watchdog_tracks_steps_and_calls(self):
        clock_val = [10.0]
        wd = ProgressWatchdog(timeout_secs=60.0, clock=lambda: clock_val[0])

        assert not wd.is_stalled()
        assert wd.remaining_time() == 60.0

        # Advance 40s -> no stall
        clock_val[0] = 50.0
        assert not wd.is_stalled()
        assert wd.remaining_time() == 20.0

        # Tool step completes -> progress reset
        wd.record_step_completed()
        assert wd.remaining_time() == 60.0

        # Advance 50s -> call completes -> progress reset and gap recorded
        clock_val[0] = 100.0
        wd.record_call_completed()
        assert wd.remaining_time() == 60.0
        assert wd.max_call_gap_s is None  # only 1 call so far

        # Advance 25s -> second call completes -> gap is 25s
        clock_val[0] = 125.0
        wd.record_call_completed()
        assert wd.max_call_gap_s == 25.0

        # Advance 70s -> exceeds timeout (60s) -> stalled
        clock_val[0] = 195.0
        assert wd.is_stalled()
        assert wd.remaining_time() == 0.0

    async def test_subagent_aborts_on_progress_watchdog_timeout(self, tmp_path):
        """Subagent aborts with stop_reason=progress_watchdog_timeout when call stalls (#1899)."""
        # Very short timeout (0.05s), provider takes 0.2s
        provider = _MockProvider(delay=0.2)
        mgr = SubagentManager(
            provider=provider,
            workspace=tmp_path,
            bus=MessageBus(),
            max_iterations=10,
        )

        # Set watchdog timeout to 0.05s via env
        import os
        orig = os.environ.get("NANOBOT_PROGRESS_TIMEOUT_SECS")
        os.environ["NANOBOT_PROGRESS_TIMEOUT_SECS"] = "0.05"
        try:
            await mgr.spawn(task="test-task", label="watchdog_test")
            await asyncio.gather(*list(mgr._running_tasks.values()), return_exceptions=True)
        finally:
            if orig is not None:
                os.environ["NANOBOT_PROGRESS_TIMEOUT_SECS"] = orig
            else:
                os.environ.pop("NANOBOT_PROGRESS_TIMEOUT_SECS", None)

        files = list((tmp_path / "state" / "subagents").glob("*.json"))
        assert files
        telem = json.loads(files[0].read_text(encoding="utf-8"))
        assert telem.get("stop_reason") == "progress_watchdog_timeout"
        assert telem["status"] == "bounded_stop"
        assert "progress_watchdog_timeout" in telem["result"]


class TestMaxCallGapRecording:
    """Requirement 3 (#1899): write max call gap between completed calls to the ledger."""

    def test_compute_cycle_max_call_gap_from_llm_calls(self, tmp_path):
        state_dir = tmp_path / "state"
        llm_dir = state_dir / "llm_calls"
        llm_dir.mkdir(parents=True)

        cycle_id = "cycle-test-gap"
        # 3 calls with gaps 15s and 45s -> max gap is 45.0s
        records = [
            {"ts": "2026-09-25T10:00:00.000000Z", "cycle_id": cycle_id},
            {"ts": "2026-09-25T10:00:15.000000Z", "cycle_id": cycle_id},
            {"ts": "2026-09-25T10:01:00.000000Z", "cycle_id": cycle_id},
            {"ts": "2026-09-25T10:02:00.000000Z", "cycle_id": "other-cycle"},
        ]
        with (llm_dir / "2026-09-25.jsonl").open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        gap = compute_cycle_max_call_gap(state_dir, cycle_id)
        assert gap == 45.0

    def test_compute_cycle_max_call_gap_single_call_returns_fallback_or_none(self, tmp_path):
        state_dir = tmp_path / "state"
        llm_dir = state_dir / "llm_calls"
        llm_dir.mkdir(parents=True)

        cycle_id = "cycle-single-call"
        with (llm_dir / "2026-09-25.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-09-25T10:00:00Z", "cycle_id": cycle_id}) + "\n")

        assert compute_cycle_max_call_gap(state_dir, cycle_id) is None
        assert compute_cycle_max_call_gap(state_dir, cycle_id, fallback_gap=12.3) == 12.3

    def test_record_cycle_outcome_writes_max_call_gap_to_ledger(self, tmp_path):
        state_dir = tmp_path / "state"
        cycle_ledger.record_cycle_outcome(
            state_dir,
            "cycle-gap-ledger",
            "success",
            None,
            ["a.py"],
            "main",
            max_call_gap_s=78.9,
        )

        rows = cycle_ledger.read_events(state_dir)
        assert len(rows) == 1
        assert rows[0]["max_call_gap_s"] == 78.9

    def test_record_cycle_outcome_omits_key_when_none(self, tmp_path):
        state_dir = tmp_path / "state"
        cycle_ledger.record_cycle_outcome(
            state_dir,
            "cycle-gap-none",
            "success",
            None,
            ["a.py"],
            "main",
            max_call_gap_s=None,
        )

        rows = cycle_ledger.read_events(state_dir)
        assert len(rows) == 1
        assert "max_call_gap_s" not in rows[0]

    async def test_subagent_run_records_max_call_gap_in_telemetry(self, tmp_path):
        """Subagent telemetry records max_call_gap_s across turns (#1899)."""
        (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
        provider = _MockProvider(
            responses=[
                LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(id="call-1", name="read_file", arguments={"path": "a.txt"})],
                ),
                LLMResponse(content="done", tool_calls=[]),
            ]
        )
        mgr = SubagentManager(
            provider=provider,
            workspace=tmp_path,
            bus=MessageBus(),
            max_iterations=10,
        )
        await mgr.spawn(task="test-task", label="gap_test")
        await asyncio.gather(*list(mgr._running_tasks.values()), return_exceptions=True)

        assert mgr.last_max_call_gap_s is not None
        assert mgr.last_max_call_gap_s >= 0.0

        files = list((tmp_path / "state" / "subagents").glob("*.json"))
        assert files
        telem = json.loads(files[0].read_text(encoding="utf-8"))
        assert "max_call_gap_s" in telem
        assert telem["max_call_gap_s"] == round(mgr.last_max_call_gap_s, 1)
