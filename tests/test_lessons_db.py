"""Tests for nanobot.runtime.lessons — unified lessons/errors database."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nanobot.runtime.lessons import (
    LessonsDB,
    _filter_source_files,
    update_lessons_from_cycle,
)


# ---------------------------------------------------------------------------
# _filter_source_files
# ---------------------------------------------------------------------------

class TestFilterSourceFiles:
    def test_keeps_source_py(self):
        assert _filter_source_files(["scripts/foo.py"]) == ["scripts/foo.py"]

    def test_keeps_source_yaml(self):
        assert _filter_source_files(["nanobot/runtime/lessons.yaml"]) == [
            "nanobot/runtime/lessons.yaml"
        ]

    def test_drops_state_reports(self):
        assert _filter_source_files(["state/reports/cycle-abc.json"]) == []

    def test_drops_state_goals(self):
        assert _filter_source_files(["state/goals/current.json"]) == []

    def test_drops_state_subagents(self):
        assert _filter_source_files(["state/subagents/results/foo.json"]) == []

    def test_mixed_list(self):
        paths = [
            "scripts/eeebot_dashboard.py",
            "state/reports/cycle-abc.json",
            "nanobot/runtime/coordinator.py",
            "state/goals/history/cycle-xyz.json",
        ]
        assert _filter_source_files(paths) == [
            "scripts/eeebot_dashboard.py",
            "nanobot/runtime/coordinator.py",
        ]


# ---------------------------------------------------------------------------
# LessonsDB — record_error
# ---------------------------------------------------------------------------

class TestRecordError:
    def test_creates_error_entry(self, tmp_path):
        db = LessonsDB(tmp_path)
        eid = db.record_error(
            category="stale_report",
            title="Test error",
            description="desc",
            root_cause="cause",
            impact="impact",
            fix_applied="fix",
            prevention="prevent",
            task_id="test-task",
            cycle_id="cycle-abc",
        )
        assert eid.startswith("ERR-AUTO-stale-report")
        entries = db.load_errors()
        assert len(entries) == 1
        assert entries[0]["category"] == "stale_report"

    def test_deduplicates_same_category(self, tmp_path):
        db = LessonsDB(tmp_path)
        db.record_error(
            category="stale_report", title="T", description="d",
            root_cause="r", impact="i", fix_applied="f", prevention="p",
            task_id="t1", cycle_id="c1",
        )
        db.record_error(
            category="stale_report", title="T2", description="d2",
            root_cause="r2", impact="i2", fix_applied="f2", prevention="p2",
            task_id="t2", cycle_id="c2",
        )
        entries = db.load_errors()
        assert len(entries) == 1
        assert entries[0]["occurrences"] == 2

    def test_writes_card_file(self, tmp_path):
        db = LessonsDB(tmp_path)
        eid = db.record_error(
            category="network_timeout", title="Network fail", description="d",
            root_cause="r", impact="i", fix_applied="f", prevention="p",
            task_id="t1", cycle_id="c1",
        )
        card_files = list((tmp_path / "lessons" / "errors").glob("*.md"))
        assert len(card_files) == 1
        assert eid in card_files[0].name or card_files[0].name.startswith("ERR")


# ---------------------------------------------------------------------------
# LessonsDB — record_lesson
# ---------------------------------------------------------------------------

class TestRecordLesson:
    def test_creates_lesson_entry(self, tmp_path):
        db = LessonsDB(tmp_path)
        lid = db.record_lesson(
            task_id="exploit-successful-improvement-path",
            title="Test lesson",
            description="desc",
            impact="high",
            approach="do the thing",
            reusable_insight="insight",
            files_changed=["scripts/eeebot_dashboard.py"],
            cycle_id="cycle-abc",
        )
        assert lid == "LESS-AUTO-exploit-successful-improvement-path"
        entries = db.load_lessons()
        assert len(entries) == 1

    def test_filters_state_files_from_files_changed(self, tmp_path):
        db = LessonsDB(tmp_path)
        db.record_lesson(
            task_id="task-x",
            title="T", description="d", impact="i",
            approach="a", reusable_insight="r",
            files_changed=[
                "scripts/eeebot_dashboard.py",
                "state/reports/cycle-abc.json",
                "state/goals/history/cycle-xyz.json",
            ],
            cycle_id="cycle-abc",
        )
        entries = db.load_lessons()
        assert entries[0]["files_changed"] == ["scripts/eeebot_dashboard.py"]

    def test_deduplicates_by_task_id(self, tmp_path):
        db = LessonsDB(tmp_path)
        db.record_lesson(
            task_id="task-x", title="T", description="d", impact="i",
            approach="a", reusable_insight="r",
            files_changed=["scripts/foo.py"],
            cycle_id="cycle-1",
        )
        db.record_lesson(
            task_id="task-x", title="T2", description="d2", impact="i2",
            approach="a2", reusable_insight="r2",
            files_changed=["scripts/bar.py"],
            cycle_id="cycle-2",
        )
        entries = db.load_lessons()
        assert len(entries) == 1
        assert entries[0]["occurrences"] == 2
        # Both files accumulated
        assert "scripts/foo.py" in entries[0]["files_changed"]
        assert "scripts/bar.py" in entries[0]["files_changed"]


# ---------------------------------------------------------------------------
# LessonsDB — query_for_task
# ---------------------------------------------------------------------------

class TestQueryForTask:
    def test_returns_empty_when_nothing_matches(self, tmp_path):
        db = LessonsDB(tmp_path)
        result = db.query_for_task("nonexistent-task")
        assert result == {}

    def test_returns_relevant_lesson(self, tmp_path):
        db = LessonsDB(tmp_path)
        db.record_lesson(
            task_id="exploit-successful-improvement-path",
            title="Exploit lesson", description="d", impact="i",
            approach="a", reusable_insight="r", files_changed=[], cycle_id="c",
        )
        result = db.query_for_task("exploit-successful-improvement-path")
        assert "relevant_lesson" in result
        assert result["relevant_lesson"]["title"] == "Exploit lesson"

    def test_returns_relevant_error(self, tmp_path):
        db = LessonsDB(tmp_path)
        db.record_error(
            category="stale_report", title="Err title", description="d",
            root_cause="r", impact="i", fix_applied="f", prevention="p",
            task_id="stale_report", cycle_id="c",
        )
        result = db.query_for_task("stale_report")
        assert "relevant_error" in result


# ---------------------------------------------------------------------------
# update_lessons_from_cycle
# ---------------------------------------------------------------------------

class TestUpdateLessonsFromCycle:
    def _kwargs(self, **overrides):
        base = dict(
            workspace=None,  # must be overridden
            result_status="PASS",
            current_task_id="exploit-successful-improvement-path",
            summary="did something",
            artifact_paths=["scripts/eeebot_dashboard.py"],
            reward_signal={"value": 1.2},
            feedback_decision=None,
            cycle_id="cycle-abc",
            recorded_at="2026-06-14T12:00:00Z",
        )
        base.update(overrides)
        return base

    def test_records_lesson_on_pass_with_real_files(self, tmp_path):
        result = update_lessons_from_cycle(**self._kwargs(workspace=tmp_path))
        assert result["action"] == "recorded_lesson"
        assert "entry_id" in result

    def test_skips_pass_with_no_real_files_and_low_reward(self, tmp_path):
        result = update_lessons_from_cycle(**self._kwargs(
            workspace=tmp_path,
            artifact_paths=["state/reports/foo.json"],
            reward_signal={"value": 0.5},
        ))
        assert result["action"] == "skipped"

    def test_records_error_on_block(self, tmp_path):
        result = update_lessons_from_cycle(**self._kwargs(
            workspace=tmp_path,
            result_status="BLOCK",
            artifact_paths=[],
            reward_signal={"value": 0.0},
            feedback_decision={"repeat_block_failure_class": "stale_report"},
        ))
        assert result["action"] == "recorded_error"

    def test_skips_approval_gate_noise(self, tmp_path):
        result = update_lessons_from_cycle(**self._kwargs(
            workspace=tmp_path,
            result_status="BLOCK",
            artifact_paths=[],
            reward_signal={"value": 0.0},
            feedback_decision={"repeat_block_failure_class": "approval_gate:expired"},
        ))
        assert result["action"] == "skipped"

    def test_skips_internal_tasks(self, tmp_path):
        for task_id in ("record-reward", "inspect-pass-streak", "run-bounded-turn"):
            result = update_lessons_from_cycle(**self._kwargs(
                workspace=tmp_path,
                current_task_id=task_id,
            ))
            assert result["action"] == "skipped", f"expected skip for {task_id}"

    def test_skips_when_no_task_id(self, tmp_path):
        result = update_lessons_from_cycle(**self._kwargs(
            workspace=tmp_path,
            current_task_id=None,
        ))
        assert result["action"] == "skipped"

    def test_never_raises_on_bad_workspace(self):
        result = update_lessons_from_cycle(**self._kwargs(
            workspace=Path("/nonexistent/path/xyz"),
            result_status="PASS",
            artifact_paths=["scripts/foo.py"],
        ))
        # Should return action='error' not raise
        assert result["action"] in ("skipped", "error", "recorded_lesson")
