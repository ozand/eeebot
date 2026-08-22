"""Tests for nanobot.runtime.lessons_context (#912): re-close the lessons
loop by filling ``lessons_context`` for the executor prompt.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from nanobot.runtime.bridge import build_task
from nanobot.runtime.lessons_context import build_lessons_context


def _write_yaml(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(entries, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _repo_with_lessons(tmp_path: Path, errors: list[dict] | None = None,
                        lessons: list[dict] | None = None) -> Path:
    repo = tmp_path / "instance_repo"
    if errors is not None:
        _write_yaml(repo / "lessons" / "errors.yaml", errors)
    if lessons is not None:
        _write_yaml(repo / "lessons" / "lessons.yaml", lessons)
    return repo


class TestErrorMatching:
    def test_title_relevant_error_card_selected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-AUTO-timeout-guard",
                    "category": "timeout",
                    "title": "Subagent timeout guard misconfigured",
                    "root_cause": "Timeout value read from stale config default.",
                    "prevention": "Always read timeout from live config, not a cached default.",
                },
            ],
        )

        result = build_lessons_context(repo, "Fix subagent timeout guard misconfiguration")

        assert set(result.keys()) == {"relevant_error"}
        err = result["relevant_error"]
        # Bridge-compatible keys exactly.
        assert set(err.keys()) == {"id", "title", "root_cause", "prevention"}
        assert err["id"] == "ERR-AUTO-timeout-guard"
        assert err["title"] == "Subagent timeout guard misconfigured"

    def test_no_relevant_card_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-AUTO-timeout-guard",
                    "category": "timeout",
                    "title": "Subagent timeout guard misconfigured",
                    "root_cause": "Timeout value read from stale config default.",
                    "prevention": "Always read timeout from live config.",
                },
            ],
        )

        result = build_lessons_context(repo, "Document the ledger digest helper for operators")

        assert result == {}


class TestLessonMatching:
    def test_lesson_and_error_both_matched_from_separate_files(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-AUTO-dashboard-crash",
                    "category": "dashboard",
                    "title": "Dashboard render crash on empty ledger",
                    "root_cause": "Ledger digest helper assumed non-empty rows.",
                    "prevention": "Guard the digest helper against empty ledger input.",
                },
            ],
            lessons=[
                {
                    "id": "LESS-AUTO-dashboard-digest",
                    "category": "successful-improvement",
                    "title": "Dashboard ledger digest helper works well",
                    "approach": "Added a small digest helper summarizing ledger rows.",
                    "reusable_insight": "Digest helpers keep dashboards fast for large ledgers.",
                },
            ],
        )

        result = build_lessons_context(repo, "Improve the dashboard ledger digest helper")

        assert "relevant_error" in result
        assert "relevant_lesson" in result
        assert result["relevant_error"]["id"] == "ERR-AUTO-dashboard-crash"
        assert result["relevant_lesson"]["id"] == "LESS-AUTO-dashboard-digest"
        assert set(result["relevant_lesson"].keys()) == {
            "id", "title", "approach", "reusable_insight",
        }


class TestFailOpen:
    def test_missing_repo_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        missing_repo = tmp_path / "does-not-exist"

        assert build_lessons_context(missing_repo, "Any task title here") == {}

    def test_missing_files_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        repo = tmp_path / "instance_repo"
        repo.mkdir()

        assert build_lessons_context(repo, "Any task title here") == {}

    def test_corrupt_yaml_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        repo = tmp_path / "instance_repo"
        errors_path = repo / "lessons" / "errors.yaml"
        errors_path.parent.mkdir(parents=True)
        errors_path.write_text("title: [unterminated flow\n  - not valid yaml: [", encoding="utf-8")

        assert build_lessons_context(repo, "Fix the unterminated flow bug in the parser") == {}

    def test_none_repo_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        assert build_lessons_context(None, "Any task title here") == {}


class TestKillSwitch:
    def test_kill_switch_off_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_LESSONS_CONTEXT_ENABLED", "0")
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-AUTO-timeout-guard",
                    "category": "timeout",
                    "title": "Subagent timeout guard misconfigured",
                    "root_cause": "Timeout value read from stale config default.",
                    "prevention": "Always read timeout from live config.",
                },
            ],
        )

        result = build_lessons_context(repo, "Fix subagent timeout guard misconfiguration")

        assert result == {}

    def test_kill_switch_false_string_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_LESSONS_CONTEXT_ENABLED", "false")
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-AUTO-timeout-guard",
                    "category": "timeout",
                    "title": "Subagent timeout guard misconfigured",
                    "root_cause": "Timeout value read from stale config default.",
                    "prevention": "Always read timeout from live config.",
                },
            ],
        )

        assert build_lessons_context(repo, "Fix subagent timeout guard misconfiguration") == {}

    def test_kill_switch_unset_defaults_to_on(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-AUTO-timeout-guard",
                    "category": "timeout",
                    "title": "Subagent timeout guard misconfigured",
                    "root_cause": "Timeout value read from stale config default.",
                    "prevention": "Always read timeout from live config.",
                },
            ],
        )

        assert build_lessons_context(repo, "Fix subagent timeout guard misconfiguration") != {}


class TestCaps:
    def test_long_root_cause_truncated_to_400(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        long_root_cause = "stale config default value " * 30  # well over 400 chars
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-AUTO-timeout-guard",
                    "category": "timeout",
                    "title": "Subagent timeout guard misconfigured",
                    "root_cause": long_root_cause,
                    "prevention": "Always read timeout from live config.",
                },
            ],
        )

        result = build_lessons_context(repo, "Fix subagent timeout guard misconfiguration")

        assert len(result["relevant_error"]["root_cause"]) == 400
        assert result["relevant_error"]["root_cause"] == long_root_cause[:400]

    def test_long_title_truncated_to_200(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        long_title = "Subagent timeout guard misconfigured " * 10  # well over 200 chars
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-AUTO-timeout-guard",
                    "category": "timeout",
                    "title": long_title,
                    "root_cause": "Timeout value read from stale config default.",
                    "prevention": "Always read timeout from live config.",
                },
            ],
        )

        result = build_lessons_context(repo, "Fix subagent timeout guard misconfiguration")

        assert len(result["relevant_error"]["title"]) == 200


class TestBridgeIntegration:
    def test_non_empty_lessons_context_renders_known_pitfall_section(self):
        """Integration: a request with a populated lessons_context renders
        the '## Known pitfall' / '## Proven approach' sections via bridge's
        build_task — the bridge-side renderer (#912 recon) already existed
        unchanged; this proves the producer side now actually feeds it."""
        req = {
            "task_title": "some task",
            "request_id": "r1",
            "cycle_id": "c1",
            "goal_id": "g1",
            "lessons_context": {
                "relevant_error": {
                    "id": "ERR-AUTO-timeout-guard",
                    "title": "Subagent timeout guard misconfigured",
                    "root_cause": "Timeout value read from stale config default.",
                    "prevention": "Always read timeout from live config.",
                },
                "relevant_lesson": {
                    "id": "LESS-AUTO-dashboard-digest",
                    "title": "Dashboard ledger digest helper works well",
                    "approach": "Added a small digest helper summarizing ledger rows.",
                    "reusable_insight": "Digest helpers keep dashboards fast.",
                },
            },
        }

        task = build_task(req, "mission text", "report_source.json")

        assert "## Known pitfall for this task (from lessons/errors.yaml)" in task
        assert "ERR-AUTO-timeout-guard" in task
        assert "## Proven approach for this task (from lessons/lessons.yaml)" in task
        assert "LESS-AUTO-dashboard-digest" in task

    def test_empty_lessons_context_omits_sections(self):
        """No regression: an empty lessons_context (today's pre-#912
        behavior when nothing matches) renders no section at all."""
        req = {
            "task_title": "some task",
            "request_id": "r1",
            "cycle_id": "c1",
            "goal_id": "g1",
            "lessons_context": {},
        }

        task = build_task(req, "mission text", "report_source.json")

        assert "Known pitfall" not in task
        assert "Proven approach" not in task
