"""Tests for nanobot.runtime.lessons_context (#912): re-close the lessons
loop by filling ``lessons_context`` for the executor prompt.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from nanobot.runtime.bridge import _write_structured_lesson, build_task
from nanobot.runtime.lessons_context import (
    _MAX_FILE_BYTES,
    _capped_entries,
    _normalize_entry,
    _safe_load_yaml,
    build_lessons_context,
)


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


class TestLiveWriterRoundTrip:
    """#912 review (MAJOR): the reader must be able to read what the LIVE
    per-cycle writer actually produces. ``bridge._write_structured_lesson``
    (bridge.py ~3807) writes ``lessons.yaml`` as a top-level dict
    (``{'lessons': [...]}``) with entries shaped
    hypothesis/result/generalized_insight/task_id — NOT
    title/category/approach/reusable_insight. This exercises the real
    writer function directly (its keyword-only signature is small and
    self-contained: repo_root/cycle_id/backlog_title/files_changed/
    commits_pushed/artifact_data/budget_used) rather than a hand-copied
    fixture, so a future change to the writer's on-disk shape breaks this
    test instead of silently reintroducing the same read/write mismatch.
    """

    def test_round_trip_through_real_bridge_writer(self, tmp_path):
        repo = tmp_path / "instance_repo"
        repo.mkdir()

        # Plain artifact without reusable insight returns False and does not write
        skipped = _write_structured_lesson(
            repo_root=repo,
            cycle_id="cycle-plain123456",
            backlog_title="Improve dashboard ledger digest helper",
            files_changed=["scripts/eeebot_dashboard.py"],
            commits_pushed=1,
            artifact_data={
                "hypothesis": "Improve dashboard ledger digest helper for faster reads",
            },
        )
        assert skipped is False
        assert not (repo / "lessons" / "lessons.yaml").exists()

        wrote = _write_structured_lesson(
            repo_root=repo,
            cycle_id="cycle-abc123def456",
            backlog_title="Improve dashboard ledger digest helper",
            files_changed=["scripts/eeebot_dashboard.py"],
            commits_pushed=1,
            artifact_data={
                "hypothesis": "Improve dashboard ledger digest helper for faster reads",
                "reusable_insight": "Digest ledger lines iteratively to avoid high memory spikes",
            },
        )
        # #1071: meaningful content alone is not a delta trigger.
        assert wrote is False

        (repo / "lessons").mkdir(parents=True, exist_ok=True)
        (repo / "lessons" / "errors.yaml").write_text(
            yaml.dump([{"task_id": "Improve dashboard ledger digest helper", "reason": "prior failure"}]),
            encoding="utf-8",
        )
        wrote = _write_structured_lesson(
            repo_root=repo,
            cycle_id="cycle-resolved123456",
            backlog_title="Improve dashboard ledger digest helper",
            files_changed=["scripts/eeebot_dashboard.py"],
            commits_pushed=1,
            artifact_data={
                "hypothesis": "Improve dashboard ledger digest helper for faster reads",
                "reusable_insight": "Digest ledger lines iteratively to avoid high memory spikes",
            },
        )
        assert wrote is True

        # Sanity: confirm the writer really did produce the dict-wrapped,
        # title-less shape this test is meant to guard against silently
        # regressing (fails loudly here, not just inside build_lessons_context).
        written = yaml.safe_load((repo / "lessons" / "lessons.yaml").read_text(encoding="utf-8"))
        assert isinstance(written, dict) and "lessons" in written
        raw_entry = written["lessons"][0]
        assert raw_entry["schema_version"] == 2
        assert raw_entry["problem"] and raw_entry["solution"] and raw_entry["tags"]
        assert "tool_calls" not in raw_entry
        assert "elapsed_seconds" not in raw_entry
        assert "hypothesis" in raw_entry and "generalized_insight" in raw_entry
        assert raw_entry["generalized_insight"] == "Digest ledger lines iteratively to avoid high memory spikes"

        # Static rule boilerplate function must be absent (#1070)
        import nanobot.runtime.bridge as bridge_mod
        assert not hasattr(bridge_mod, "_derive_insight")

        result = build_lessons_context(
            repo, "Improve dashboard ledger digest helper for faster reads"
        )

        assert "relevant_lesson" in result
        less = result["relevant_lesson"]
        assert {"id", "title", "approach", "reusable_insight"} <= set(less.keys())
        assert less["id"]
        assert less["title"]  # non-blank: derived from hypothesis
        assert "dashboard ledger digest helper" in less["title"].lower()
        assert less["approach"]  # non-blank: derived from result
        assert less["reusable_insight"] == "Digest ledger lines iteratively to avoid high memory spikes"

    def test_write_structured_error_records_in_errors_yaml(self, tmp_path):
        """#1041 Part 2: _write_structured_error records gate failure in lessons/errors.yaml."""
        from nanobot.runtime.bridge import _write_structured_error

        repo = tmp_path / "instance_repo"
        repo.mkdir()

        wrote = _write_structured_error(
            repo_root=repo,
            cycle_id="cycle-err123456789",
            reason="mutation_surface_violation",
            violated_check="mutation_surface_violation: touched /etc/shadow",
            budget_used={"tool_calls": 3, "elapsed_seconds": 15},
            backlog_title="Update core credentials logic",
        )
        assert wrote is True

        errors_file = repo / "lessons" / "errors.yaml"
        assert errors_file.exists()
        written = yaml.safe_load(errors_file.read_text(encoding="utf-8"))
        assert isinstance(written, list)
        assert len(written) == 1

        entry = written[0]
        assert entry["cycle_id"] == "cycle-err123456789"
        assert entry["reason"] == "mutation_surface_violation"
        assert entry["violated_check"] == "mutation_surface_violation: touched /etc/shadow"
        assert "mutation_surface_violation" in entry["generalized_insight"]
        assert entry["id"].startswith("ERR-")

    def test_write_structured_error_preserves_bare_list_schema_and_appends_without_data_loss(self, tmp_path):
        """_write_structured_error preserves pre-existing bare-list schema without resetting or losing entries."""
        from nanobot.runtime.bridge import _write_structured_error

        repo = tmp_path / "instance_repo"
        lessons_dir = repo / "lessons"
        lessons_dir.mkdir(parents=True)
        errors_file = lessons_dir / "errors.yaml"

        pre_existing = [
            {
                "id": "ERR-20260614-001",
                "date": "2026-06-14",
                "cycle_id": "cycle-old111111111",
                "hypothesis": "Old error hypothesis.",
                "reason": "gate_timeout",
            },
            {
                "id": "ERR-20260614-002",
                "date": "2026-06-14",
                "cycle_id": "cycle-old222222222",
                "hypothesis": "Second old error.",
                "reason": "compile_failed",
            },
        ]
        errors_file.write_text(yaml.dump(pre_existing, sort_keys=False), encoding="utf-8")

        wrote = _write_structured_error(
            repo_root=repo,
            cycle_id="cycle-new333333333",
            reason="test_failed",
            violated_check="pytest returned 1",
            budget_used={"tool_calls": 5, "elapsed_seconds": 20},
            backlog_title="Improve error handling",
        )
        assert wrote is True

        written = yaml.safe_load(errors_file.read_text(encoding="utf-8"))
        assert isinstance(written, list)
        assert len(written) == 3
        # Newly written item is prepended at index 0
        assert written[0]["cycle_id"] == "cycle-new333333333"
        assert written[0]["reason"] == "test_failed"
        # Pre-existing items are completely preserved without data loss
        assert written[1]["id"] == "ERR-20260614-001"
        assert written[1]["reason"] == "gate_timeout"
        assert written[2]["id"] == "ERR-20260614-002"
        assert written[2]["reason"] == "compile_failed"

    def test_write_structured_error_accepts_dict_wrapper_if_encountered(self, tmp_path):
        """_write_structured_error accepts dict-wrapped errors.yaml and preserves the wrapper key."""
        from nanobot.runtime.bridge import _write_structured_error

        repo = tmp_path / "instance_repo"
        lessons_dir = repo / "lessons"
        lessons_dir.mkdir(parents=True)
        errors_file = lessons_dir / "errors.yaml"

        pre_existing = {
            "errors": [
                {
                    "id": "ERR-20260614-001",
                    "cycle_id": "cycle-old111111111",
                    "reason": "gate_timeout",
                }
            ]
        }
        errors_file.write_text(yaml.dump(pre_existing, sort_keys=False), encoding="utf-8")

        wrote = _write_structured_error(
            repo_root=repo,
            cycle_id="cycle-new444444444",
            reason="mutation_surface_violation",
        )
        assert wrote is True

        written = yaml.safe_load(errors_file.read_text(encoding="utf-8"))
        assert isinstance(written, dict) and "errors" in written
        assert len(written["errors"]) == 2
        assert written["errors"][0]["cycle_id"] == "cycle-new444444444"
        assert written["errors"][1]["id"] == "ERR-20260614-001"


class TestOnDiskShapes:
    def test_safe_load_yaml_accepts_bare_list(self, tmp_path):
        path = tmp_path / "errors.yaml"
        _write_yaml(path, [{"id": "E1", "title": "x"}])

        assert _safe_load_yaml(path) == [{"id": "E1", "title": "x"}]

    def test_safe_load_yaml_accepts_lessons_dict_wrapper(self, tmp_path):
        path = tmp_path / "lessons.yaml"
        path.write_text(
            yaml.dump({"lessons": [{"id": "L1", "hypothesis": "x"}]}, sort_keys=False),
            encoding="utf-8",
        )

        assert _safe_load_yaml(path) == [{"id": "L1", "hypothesis": "x"}]

    def test_safe_load_yaml_accepts_errors_dict_wrapper(self, tmp_path):
        path = tmp_path / "errors.yaml"
        path.write_text(
            yaml.dump({"errors": [{"id": "E1", "title": "x"}]}, sort_keys=False),
            encoding="utf-8",
        )

        assert _safe_load_yaml(path) == [{"id": "E1", "title": "x"}]

    def test_safe_load_yaml_unrecognized_dict_returns_empty(self, tmp_path):
        path = tmp_path / "lessons.yaml"
        path.write_text(yaml.dump({"something_else": [1, 2]}), encoding="utf-8")

        assert _safe_load_yaml(path) == []

    def test_normalize_entry_fills_gaps_without_overwriting(self):
        live = _normalize_entry({
            "id": "L1",
            "hypothesis": "Do the thing",
            "result": "Did it",
            "generalized_insight": "It worked",
            "task_id": "fallback-id",
        })
        assert live["title"] == "Do the thing"
        assert live["approach"] == "Did it"
        assert live["reusable_insight"] == "It worked"

        legacy = _normalize_entry({
            "id": "L2",
            "title": "Already has a title",
            "approach": "Already has an approach",
            "reusable_insight": "Already has an insight",
            "hypothesis": "should be ignored",
        })
        assert legacy["title"] == "Already has a title"
        assert legacy["approach"] == "Already has an approach"
        assert legacy["reusable_insight"] == "Already has an insight"

    def test_normalize_entry_id_falls_back_to_task_id(self):
        normalized = _normalize_entry({"task_id": "some-task", "hypothesis": "x"})
        assert normalized["id"] == "some-task"


class TestNewestFirstOrdering:
    """#912 review (MINOR): both writers prepend (insert(0, ...)), so the
    file is newest-first. A large file must be capped by keeping the HEAD
    slice, and tied scores must resolve to the earliest (newest) entry."""

    def test_capped_entries_keeps_head_slice_when_over_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr("nanobot.runtime.lessons_context._MAX_CARDS_SCANNED", 3)
        path = tmp_path / "errors.yaml"
        # Index 0 is newest per both writers' insert(0, ...) convention.
        entries = [{"id": f"E{i}", "title": f"card {i}"} for i in range(5)]
        _write_yaml(path, entries)

        capped = _capped_entries(path)

        assert [e["id"] for e in capped] == ["E0", "E1", "E2"]

    def test_tied_score_prefers_earliest_newest_entry(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        repo = _repo_with_lessons(
            tmp_path,
            errors=[
                {
                    "id": "ERR-2-newest",
                    "category": "timeout",
                    "title": "Timeout guard fails again",
                    "root_cause": "x",
                    "prevention": "y",
                },
                {
                    "id": "ERR-1-older",
                    "category": "timeout",
                    "title": "Timeout guard fails today",
                    "root_cause": "x",
                    "prevention": "y",
                },
            ],
        )

        result = build_lessons_context(repo, "Fix timeout guard fails issue")

        assert result["relevant_error"]["id"] == "ERR-2-newest"


class TestSizeGuard:
    def test_oversized_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
        repo = tmp_path / "instance_repo"
        errors_path = repo / "lessons" / "errors.yaml"
        errors_path.parent.mkdir(parents=True)
        # A single valid-YAML entry padded past the size cap.
        padding = "x" * (_MAX_FILE_BYTES + 1024)
        _write_yaml(errors_path, [{
            "id": "ERR-1",
            "category": "timeout",
            "title": "Timeout guard fails",
            "root_cause": padding,
            "prevention": "y",
        }])
        assert errors_path.stat().st_size > _MAX_FILE_BYTES

        assert build_lessons_context(repo, "Fix timeout guard fails issue") == {}


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

    # Plain protocol fields (title/hypothesis/backlog instructions) inside next_bounded_candidate
    # do not count as lessons, but explicit concrete_improvement_statement or key_insight inside
    # containers or top-level count.
    def test_has_meaningful_lesson_predicate(self):
        """#1070: Plain integrated success cycles without explicit insight payload

        must not be classified as meaningful lessons. Explicit insights must match.
        """
        from nanobot.runtime.bridge import _has_meaningful_lesson

        # None or empty dict
        assert _has_meaningful_lesson(None) is False
        assert _has_meaningful_lesson({}) is False

        # Protocol-only / empty structures
        assert _has_meaningful_lesson({"hypothesis": "Some hypothesis for standard run"}) is False
        assert _has_meaningful_lesson({"next_bounded_candidate": {"title": "some task"}}) is False
        assert _has_meaningful_lesson({"next_bounded_candidate": {"title": "some task", "hypothesis": ""}}) is False
        assert _has_meaningful_lesson({"next_bounded_candidate": {"title": "some task", "hypothesis": "none"}}) is False
        assert _has_meaningful_lesson({"next_bounded_candidate": {"title": "some task", "hypothesis": "N/A"}}) is False

        # Genuine explicit insights
        assert _has_meaningful_lesson({
            "reusable_insight": "Refactored cache to reduce duplicate I/O.",
        }) is True
        assert _has_meaningful_lesson({
            "concrete_improvement_statement": "Refactored cache to reduce duplicate I/O.",
        }) is True
        assert _has_meaningful_lesson({
            "lesson": {
                "reusable_insight": "Early filtering prevents quadratic memory consumption.",
            }
        }) is True
        assert _has_meaningful_lesson({
            "structured_lesson": {
                "generalized_insight": "Early filtering prevents quadratic memory consumption.",
            }
        }) is True
        assert _has_meaningful_lesson({
            "key_insight": "Atomic replacements avoid corruption during sudden terminations."
        }) is True

