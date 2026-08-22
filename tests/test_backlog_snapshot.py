"""Tests for #913: bridge-native hypothesis backlog snapshot.

``nanobot.runtime.backlog_snapshot.write_backlog_snapshot`` regenerates
``state_dir/hypotheses/backlog.json`` from live bridge state (the pending
subagent request queue + goals/registry.json), replacing the frozen
coordinator-only writer (``cycle_persist._build_hypothesis_backlog_snapshot``).
Its consumer contract is ``nanobot.runtime.hypothesis_backlog`` (round-tripped
here via its public ``top_candidates``/``context_section`` functions) — see
that module's own test file (``tests/test_hypothesis_backlog.py``, required
to keep passing unmodified) for the full reader-side contract.
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import hypothesis_backlog
from nanobot.runtime.backlog_snapshot import write_backlog_snapshot


def _seed_request(state_dir: Path, request_id: str, **extra) -> None:
    req_dir = state_dir / "subagents" / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    payload = {"request_id": request_id, "task_title": f"Task {request_id}", **extra}
    (req_dir / f"{request_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_goal_registry(state_dir: Path, *, active_goal_id: str = "goal-1", current_task_id: str | None = None) -> None:
    goals_dir = state_dir / "goals"
    goals_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"active_goal_id": active_goal_id}
    if current_task_id:
        payload["current_task_id"] = current_task_id
    (goals_dir / "registry.json").write_text(json.dumps(payload), encoding="utf-8")


class TestWriteBacklogSnapshot:
    def test_valid_state_writes_file_and_round_trips_through_reader(self, tmp_path):
        state_dir = tmp_path / "state"
        _seed_goal_registry(state_dir, current_task_id="req-1")
        _seed_request(state_dir, "req-1", acceptance="`pytest -q` passes")
        _seed_request(state_dir, "req-2", evidence="benchmark shows 2x speedup")

        assert write_backlog_snapshot(state_dir, None) is True

        backlog_path = state_dir / "hypotheses" / "backlog.json"
        assert backlog_path.is_file()
        data = json.loads(backlog_path.read_text(encoding="utf-8"))
        assert data["entry_count"] == 2
        titles = {e["task_title"] for e in data["entries"]}
        assert titles == {"Task req-1", "Task req-2"}

        # Round-trip through the real consumer contract (hypothesis_backlog's
        # public read path) — this is the shape guarantee #913 requires.
        candidates = hypothesis_backlog.top_candidates(state_dir)
        candidate_titles = {c["title"] for c in candidates}
        assert candidate_titles == {"Task req-1", "Task req-2"}

        section = hypothesis_backlog.context_section(state_dir)
        assert "Task req-1" in section
        assert "Task req-2" in section

    def test_selected_task_marked_from_current_task_id(self, tmp_path):
        state_dir = tmp_path / "state"
        _seed_goal_registry(state_dir, current_task_id="req-1")
        _seed_request(state_dir, "req-1")
        _seed_request(state_dir, "req-2")

        write_backlog_snapshot(state_dir, None)

        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["selected_hypothesis_id"] == "req-1"
        by_id = {e["task_id"]: e for e in data["entries"]}
        assert by_id["req-1"]["selected"] is True
        assert by_id["req-1"]["selection_status"] == "selected"
        assert by_id["req-2"]["selected"] is False
        assert by_id["req-2"]["selection_status"] == "backlog"

    def test_no_requests_still_writes_empty_backlog(self, tmp_path):
        state_dir = tmp_path / "state"
        _seed_goal_registry(state_dir)

        assert write_backlog_snapshot(state_dir, None) is True

        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["entries"] == []
        assert data["entry_count"] == 0

    def test_missing_state_dir_entirely_still_writes(self, tmp_path):
        # No goals/, no subagents/ at all — every input source is absent.
        state_dir = tmp_path / "state"

        assert write_backlog_snapshot(state_dir, None) is True
        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["entries"] == []
        assert data["goal_id"] == ""

    def test_corrupt_registry_does_not_crash_and_still_writes(self, tmp_path):
        state_dir = tmp_path / "state"
        goals_dir = state_dir / "goals"
        goals_dir.mkdir(parents=True)
        (goals_dir / "registry.json").write_text("not json {{{", encoding="utf-8")
        _seed_request(state_dir, "req-1")

        assert write_backlog_snapshot(state_dir, None) is True
        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        # Corrupt registry -> goal_id absent, but the request candidate still
        # comes through (each input source fails open independently).
        assert data["goal_id"] == ""
        assert data["entry_count"] == 1

    def test_corrupt_request_file_is_skipped_not_fatal(self, tmp_path):
        state_dir = tmp_path / "state"
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "bad.json").write_text("not json {{{", encoding="utf-8")
        _seed_request(state_dir, "req-good")

        assert write_backlog_snapshot(state_dir, None) is True
        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["entry_count"] == 1
        assert data["entries"][0]["task_id"] == "req-good"

    def test_request_without_title_is_skipped(self, tmp_path):
        state_dir = tmp_path / "state"
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        (req_dir / "notitle.json").write_text(json.dumps({"request_id": "notitle"}), encoding="utf-8")
        _seed_request(state_dir, "req-good")

        write_backlog_snapshot(state_dir, None)
        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["entry_count"] == 1
        assert data["entries"][0]["task_id"] == "req-good"

    def test_repeated_calls_are_idempotent(self, tmp_path):
        state_dir = tmp_path / "state"
        _seed_goal_registry(state_dir)
        _seed_request(state_dir, "req-1")

        write_backlog_snapshot(state_dir, None)
        first = (state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8")
        first_data = json.loads(first)

        write_backlog_snapshot(state_dir, None)
        second_data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))

        # Same input state -> same entries/counts (generated_at_utc is the
        # only field allowed to differ between calls).
        assert first_data["entries"] == second_data["entries"]
        assert first_data["entry_count"] == second_data["entry_count"]
        assert first_data["goal_id"] == second_data["goal_id"]

    def test_evidence_gated_entry_passes_demand_evidence_check(self, tmp_path):
        """entries carry evidence/acceptance so demand._hypothesis_items'
        evidence gate (a live consumer of this same file) can actually admit
        them — not just hypothesis_backlog's own (looser) reader."""
        from nanobot.runtime.demand import _hypothesis_has_evidence

        state_dir = tmp_path / "state"
        _seed_request(state_dir, "req-1", evidence="microbenchmark: 30% faster")
        write_backlog_snapshot(state_dir, None)

        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        entry = data["entries"][0]
        assert _hypothesis_has_evidence(entry, None) is True

    def test_bounded_request_scan_caps_candidate_count(self, tmp_path, monkeypatch):
        from nanobot.runtime import backlog_snapshot

        monkeypatch.setattr(backlog_snapshot, "_MAX_REQUEST_CANDIDATES", 3)
        state_dir = tmp_path / "state"
        for i in range(10):
            _seed_request(state_dir, f"req-{i}")

        write_backlog_snapshot(state_dir, None)
        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["entry_count"] == 3
