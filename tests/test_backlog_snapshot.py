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


def _seed_goal_text(state_dir: Path, *, goal_id: str = "goal-1") -> None:
    """#1222: the active goal id comes from the operator's goals/goal_text.json
    (goal_review.active_goal_id), not the coordinator's frozen registry.json."""
    goals_dir = state_dir / "goals"
    goals_dir.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "goal-text-v1", "goal_id": goal_id, "text": "test goal"}
    (goals_dir / "goal_text.json").write_text(json.dumps(payload), encoding="utf-8")


def _mark_handled(state_dir: Path, request_id: str) -> None:
    """Mirror bridge.py's own handled-marker write EXACTLY: `bridge.py`
    files `handled_{safe_id}.txt` under `subagent_bridge/` (safe_id =
    request_id.replace('/', '_')[:120]) with the request PATH as content —
    see bridge.py's `handled_marker.write_text(str(req_path))`. Content here
    is a stand-in path string; _handled_request_markers only needs the
    stem to match request_id, which is the primary match this exercises."""
    bridge_state_dir = state_dir / "subagent_bridge"
    bridge_state_dir.mkdir(parents=True, exist_ok=True)
    safe_id = request_id.replace("/", "_")[:120]
    marker = bridge_state_dir / f"handled_{safe_id}.txt"
    req_path = state_dir / "subagents" / "requests" / f"{request_id}.json"
    marker.write_text(str(req_path), encoding="utf-8")


class TestWriteBacklogSnapshot:
    def test_valid_state_writes_file_and_round_trips_through_reader(self, tmp_path):
        state_dir = tmp_path / "state"
        _seed_goal_text(state_dir)
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

    def test_nothing_is_preselected_every_candidate_is_backlog(self, tmp_path):
        """#1222: the coordinator's current_task_id (goals/registry.json) is
        gone; the bridge queue is FIFO, so no candidate is ever ``selected``."""
        state_dir = tmp_path / "state"
        _seed_goal_text(state_dir)
        _seed_request(state_dir, "req-1")
        _seed_request(state_dir, "req-2")

        write_backlog_snapshot(state_dir, None)

        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["selected_hypothesis_id"] is None
        assert data["goal_id"] == "goal-1"
        assert all(e["selected"] is False and e["selection_status"] == "backlog" for e in data["entries"])

    def test_no_requests_still_writes_empty_backlog(self, tmp_path):
        state_dir = tmp_path / "state"
        _seed_goal_text(state_dir)

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

    def test_corrupt_goal_text_does_not_crash_and_still_writes(self, tmp_path):
        state_dir = tmp_path / "state"
        goals_dir = state_dir / "goals"
        goals_dir.mkdir(parents=True)
        (goals_dir / "goal_text.json").write_text("not json {{{", encoding="utf-8")
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
        _seed_goal_text(state_dir)
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


class TestHandledRequestsExcluded:
    """#913 review (MAJOR): subagents/requests/ is an execution queue, not
    an archive — a request file stays on disk (request_status stays
    'queued') long after bridge has actually executed it; only a separate
    handled_*.txt marker under subagent_bridge/ records completion (see
    bridge.find_pending_request's real_handled check). Without excluding
    those, every already-executed request would resurface here as a
    'backlog' hypothesis candidate forever."""

    def test_only_unhandled_request_becomes_a_candidate(self, tmp_path):
        state_dir = tmp_path / "state"
        _seed_request(state_dir, "req-done-1")
        _seed_request(state_dir, "req-done-2")
        _seed_request(state_dir, "req-open")
        _mark_handled(state_dir, "req-done-1")
        _mark_handled(state_dir, "req-done-2")

        assert write_backlog_snapshot(state_dir, None) is True

        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["entry_count"] == 1
        assert data["entries"][0]["task_id"] == "req-open"
        assert data["entries"][0]["task_title"] == "Task req-open"

    def test_handled_and_queued_mix_renders_only_unhandled_titles_in_context_section(self, tmp_path):
        state_dir = tmp_path / "state"
        _seed_request(state_dir, "req-done-1")
        _seed_request(state_dir, "req-done-2")
        _seed_request(state_dir, "req-open")
        _mark_handled(state_dir, "req-done-1")
        _mark_handled(state_dir, "req-done-2")

        write_backlog_snapshot(state_dir, None)

        section = hypothesis_backlog.context_section(state_dir)
        assert "Task req-open" in section
        assert "Task req-done-1" not in section
        assert "Task req-done-2" not in section

    def test_handled_marker_matched_by_sanitized_stem_with_slash_in_request_id(self, tmp_path):
        """bridge.py sanitizes request_id -> safe_id (slashes -> underscores)
        for the marker filename itself — a raw request_id containing '/'
        must still match via the sanitized-stem comparison, mirroring
        bridge.find_pending_request's own safe_rid check (#733 wedge note)."""
        state_dir = tmp_path / "state"
        req_dir = state_dir / "subagents" / "requests"
        req_dir.mkdir(parents=True)
        raw_id = "lane/req-1"
        payload = {"request_id": raw_id, "task_title": "Task with slash id"}
        (req_dir / "lane_req-1.json").write_text(json.dumps(payload), encoding="utf-8")

        bridge_state_dir = state_dir / "subagent_bridge"
        bridge_state_dir.mkdir(parents=True)
        safe_id = raw_id.replace("/", "_")[:120]
        (bridge_state_dir / f"handled_{safe_id}.txt").write_text(raw_id, encoding="utf-8")

        write_backlog_snapshot(state_dir, None)

        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["entry_count"] == 0

    def test_non_queued_status_excluded_even_without_marker(self, tmp_path):
        """A request_status outside queued/pending is excluded on its own,
        independent of the handled-marker check."""
        state_dir = tmp_path / "state"
        _seed_request(state_dir, "req-blocked", request_status="blocked")
        _seed_request(state_dir, "req-open")

        write_backlog_snapshot(state_dir, None)

        data = json.loads((state_dir / "hypotheses" / "backlog.json").read_text(encoding="utf-8"))
        assert data["entry_count"] == 1
        assert data["entries"][0]["task_id"] == "req-open"
