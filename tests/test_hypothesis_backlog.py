"""Tests for #751: the hypotheses -> priorities reader.

Covers reading candidates from the primary source
(``hypotheses/backlog.json``, ``cycle_persist._build_hypothesis_backlog_snapshot``'s
shape) and the secondary source (``research/hypotheses.json``,
``cycle_planning._write_research_feed``'s append-only shape), the bounded
``context_section`` rendering, and the lifecycle reconciliation
(active -> answered/stale), including that unknown fields in a lifecycle
entry survive a rewrite.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import cycle_ledger, hypothesis_backlog


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    return state_dir


def _write_backlog(state_dir: Path, entries: list[dict]) -> None:
    backlog_dir = state_dir / "hypotheses"
    backlog_dir.mkdir(parents=True, exist_ok=True)
    (backlog_dir / "backlog.json").write_text(
        json.dumps({"entries": entries}), encoding="utf-8"
    )


def _write_research(state_dir: Path, snapshots: list[dict]) -> None:
    research_dir = state_dir / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    (research_dir / "hypotheses.json").write_text(json.dumps(snapshots), encoding="utf-8")


def _write_lifecycle(state_dir: Path, entries: dict) -> None:
    backlog_dir = state_dir / "hypotheses"
    backlog_dir.mkdir(parents=True, exist_ok=True)
    (backlog_dir / "lifecycle.json").write_text(
        json.dumps({"schema_version": "hypothesis-lifecycle-v1", "entries": entries}),
        encoding="utf-8",
    )


def _read_lifecycle(state_dir: Path) -> dict:
    path = state_dir / "hypotheses" / "lifecycle.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestPrimarySource:
    def test_top_candidates_reads_backlog_primary_source(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        entries = [
            {"hypothesis_id": f"hypothesis-h{i}", "task_title": f"Title {i}"} for i in range(7)
        ]
        _write_backlog(state_dir, entries)

        candidates = hypothesis_backlog.top_candidates(state_dir)

        assert len(candidates) == hypothesis_backlog.TOP_N
        assert candidates[0] == {"key": "hypothesis-h0", "title": "Title 0", "source": "backlog"}

    def test_context_section_format(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(
            state_dir,
            [{"hypothesis_id": "hypothesis-h1", "task_title": "Investigate flaky test X"}],
        )

        section = hypothesis_backlog.context_section(state_dir)
        assert section == "- [hypothesis-h1] Investigate flaky test X"

    def test_corrupt_backlog_file_is_omitted(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        backlog_dir = state_dir / "hypotheses"
        backlog_dir.mkdir(parents=True)
        (backlog_dir / "backlog.json").write_text("not json {{{", encoding="utf-8")

        assert hypothesis_backlog.top_candidates(state_dir) == []
        assert hypothesis_backlog.context_section(state_dir) == ""

    def test_missing_files_are_fail_open(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert hypothesis_backlog.top_candidates(state_dir) == []
        assert hypothesis_backlog.context_section(state_dir) == ""

    def test_entries_without_title_or_id_are_skipped(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(
            state_dir,
            [
                {"hypothesis_id": "", "task_title": ""},
                {"hypothesis_id": "hypothesis-h1", "task_title": "Valid title"},
                "not a dict",
            ],
        )
        candidates = hypothesis_backlog.top_candidates(state_dir)
        assert candidates == [{"key": "hypothesis-h1", "title": "Valid title", "source": "backlog"}]


class TestSecondarySource:
    def test_research_hypotheses_used_when_no_backlog(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_research(
            state_dir,
            [
                {
                    "date": "2026-07-01",
                    "cycle_id": "cycle-a",
                    "candidates": [{"title": "Research candidate one", "acceptance": "..."}],
                }
            ],
        )

        candidates = hypothesis_backlog.top_candidates(state_dir)
        assert len(candidates) == 1
        assert candidates[0]["title"] == "Research candidate one"
        assert candidates[0]["key"].startswith("slug-")
        assert candidates[0]["source"] == "research"

    def test_backlog_takes_precedence_and_dedups_by_key(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Primary title"}])
        _write_research(
            state_dir,
            [{"date": "2026-07-01", "cycle_id": "cycle-a", "candidates": [{"title": "Secondary title"}]}],
        )

        candidates = hypothesis_backlog.top_candidates(state_dir)
        titles = [c["title"] for c in candidates]
        assert "Primary title" in titles
        assert "Secondary title" in titles
        assert titles[0] == "Primary title"

    def test_corrupt_research_file_is_omitted(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        research_dir = state_dir / "research"
        research_dir.mkdir(parents=True)
        (research_dir / "hypotheses.json").write_text("not json", encoding="utf-8")
        assert hypothesis_backlog.top_candidates(state_dir) == []


class TestLifecycleReconciliation:
    def test_answered_marking_on_success_outcome_with_serves_hypothesis(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        cycle_ledger.append_event(
            state_dir, {"phase": "outcome", "cycle_id": "c1", "outcome": "success"}
        )

        hypothesis_backlog.reconcile(state_dir)

        lifecycle = _read_lifecycle(state_dir)
        entry = lifecycle["entries"]["hypothesis-h1"]
        assert entry["status"] == "answered"
        assert entry["answered_evidence"] == "c1"

        # Answered candidates no longer surface as context candidates.
        assert hypothesis_backlog.top_candidates(state_dir) == []
        assert hypothesis_backlog.context_section(state_dir) == ""

    def test_referenced_but_not_yet_successful_stays_active(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        cycle_ledger.append_event(
            state_dir, {"phase": "outcome", "cycle_id": "c1", "outcome": "failed"}
        )

        hypothesis_backlog.reconcile(state_dir)

        lifecycle = _read_lifecycle(state_dir)
        assert lifecycle["entries"]["hypothesis-h1"]["status"] == "active"
        assert len(hypothesis_backlog.top_candidates(state_dir)) == 1

    def test_stale_demotion_by_age_excluded_from_context(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Old idea"}])
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat().replace("+00:00", "Z")
        _write_lifecycle(
            state_dir,
            {"hypothesis-h1": {"status": "active", "first_seen": old_ts, "cycles_untouched": 0}},
        )

        hypothesis_backlog.reconcile(state_dir)

        lifecycle = _read_lifecycle(state_dir)
        assert lifecycle["entries"]["hypothesis-h1"]["status"] == "stale"
        assert hypothesis_backlog.top_candidates(state_dir) == []

    def test_stale_demotion_by_untouched_cycles_excluded_from_context(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Old idea"}])
        recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_lifecycle(
            state_dir,
            {
                "hypothesis-h1": {
                    "status": "active",
                    "first_seen": recent_ts,
                    "cycles_untouched": hypothesis_backlog.STALE_AFTER_UNTOUCHED_CYCLES - 1,
                }
            },
        )

        hypothesis_backlog.reconcile(state_dir)

        lifecycle = _read_lifecycle(state_dir)
        assert lifecycle["entries"]["hypothesis-h1"]["status"] == "stale"
        assert hypothesis_backlog.top_candidates(state_dir) == []

    def test_unknown_fields_preserved_after_rewrite(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_lifecycle(
            state_dir,
            {
                "hypothesis-h1": {
                    "status": "active",
                    "first_seen": recent_ts,
                    "cycles_untouched": 0,
                    "custom_note": "operator annotation — keep me",
                }
            },
        )

        hypothesis_backlog.reconcile(state_dir)

        lifecycle = _read_lifecycle(state_dir)
        assert lifecycle["entries"]["hypothesis-h1"]["custom_note"] == "operator annotation — keep me"

    def test_reconcile_is_fail_open_on_unreadable_state(self, tmp_path):
        # No exception even when nothing exists at all.
        hypothesis_backlog.reconcile(tmp_path / "does-not-exist")
