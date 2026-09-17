"""Tests for nanobot.runtime.lessons_context (#912): re-close the lessons
loop by filling ``lessons_context`` for the executor prompt.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from nanobot.runtime.bridge import _error_condition_title, _write_structured_error, build_task
from nanobot.runtime.lessons_context import (
    _MAX_FILE_BYTES,
    _best_card,
    _best_card_with_provenance,
    _capped_entries,
    _extract_words,
    _normalize_entry,
    _safe_load_yaml,
    build_lessons_context,
    selection_provenance,
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


class TestRecurrenceRanking:
    def test_recurrence_bonus_is_small_and_requires_independent_days(self):
        from nanobot.runtime.lessons_context import _recurrence_bonus

        assert _recurrence_bonus({"seen_count": 1, "distinct_days": 1}) == 0
        assert _recurrence_bonus({"seen_count": 4, "distinct_days": 1}) == 0
        assert _recurrence_bonus({"seen_count": 2, "distinct_days": 2}) == 1
        assert _recurrence_bonus({"seen_count": 100, "distinct_days": 100}) == 2
        assert _recurrence_bonus({"seen_count": "bad", "distinct_days": None}) == 0

    def test_recurrence_breaks_equal_lexical_match(self):
        task_words = _extract_words("Fix timeout guard")
        one_off = {"id": "one", "title": "Timeout guard", "root_cause": "", "seen_count": 1, "distinct_days": 1}
        repeated = {"id": "repeated", "title": "Timeout guard", "root_cause": "", "seen_count": 2, "distinct_days": 2}

        assert _best_card([one_off, repeated], task_words, "root_cause") is repeated

    def test_recurrence_cannot_crowd_out_stronger_lexical_match(self):
        task_words = _extract_words("Add skill markdown test validation")
        specific = {
            "id": "specific", "title": "Skill test validation", "root_cause": "markdown",
            "seen_count": 1, "distinct_days": 1,
        }
        recurrent = {
            "id": "recurrent", "title": "Document verification process", "root_cause": "",
            "seen_count": 3, "distinct_days": 3,
        }

        assert _best_card([specific, recurrent], task_words, "root_cause") is specific

    def test_recurrence_cannot_break_shared_word_specificity_tie(self):
        task_words = _extract_words("Document cycle test verification proposals")
        specific = {
            "id": "specific", "title": "Cycle test duplicate proposals", "approach": "verification",
            "seen_count": 1, "distinct_days": 1,
        }
        recurrent = {
            "id": "recurrent", "title": "Document test verification", "approach": "verification",
            "seen_count": 3, "distinct_days": 3,
        }

        # Both cards score 7, but the specific card shares four distinct words
        # versus three. Recurrence must not overturn that more-specific match.
        assert _best_card([specific, recurrent], task_words, "approach") is specific

    def test_recurrence_does_not_break_lexical_score_tie_with_more_shared_words(self):
        task_words = _extract_words("Alpha beta gamma")
        specific = {
            "id": "specific", "title": "Alpha", "approach": "beta gamma",
            "seen_count": 1, "distinct_days": 1,
        }
        recurrent = {
            "id": "recurrent", "title": "Beta gamma", "approach": "",
            "seen_count": 3, "distinct_days": 3,
        }

        # Both lexical scores are 4, but the specific card shares three
        # distinct words versus two. Recurrence is only consulted after
        # shared-word specificity, so it cannot win.
        assert _best_card([specific, recurrent], task_words, "approach") is specific

    def test_provenance_selection_matches_live_selector_with_recurrence(self):
        entries = [
            {"id": "one", "title": "Timeout guard", "root_cause": "", "seen_count": 1, "distinct_days": 1},
            {"id": "repeated", "title": "Timeout guard", "root_cause": "", "seen_count": 2, "distinct_days": 2},
        ]
        words = _extract_words("Fix timeout guard")
        live = _best_card(entries, words, "root_cause")
        instrumented, provenance = _best_card_with_provenance(entries, words, "root_cause")

        assert instrumented["id"] == live["id"] == "repeated"
        assert provenance["selected_id"] == live["id"]


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

    def test_normalize_entry_extracts_real_hypothesis_title(self):
        normalized = _normalize_entry({
            "id": "L1",
            "hypothesis": 'Implementing "Optimize scripts/verify_imports.py import resolution to avoid harness timeout" improves operator value.',
        })
        assert normalized["title"] == "Optimize scripts/verify_imports.py import resolution to avoid harness timeout"

    def test_normalize_entry_without_quotes_keeps_usable_fallback(self):
        normalized = _normalize_entry({
            "id": "L2",
            "hypothesis": "Improve dashboard ledger digest helper for faster reads",
        })
        assert normalized["title"] == "Improve dashboard ledger digest helper for faster reads"

    def test_different_quoted_hypotheses_produce_different_titles(self):
        first = _normalize_entry({"hypothesis": 'Implementing "Optimize scripts/verify_imports.py import resolution" improves operator value.'})
        second = _normalize_entry({"hypothesis": 'Implementing "Add target path comparison to check_last_cycle_repeat.py" improves operator value.'})
        assert first["title"] != second["title"]

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
            "id": "L3",
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


def test_error_condition_title_prefers_check_detail_over_machine_reason():
    title = _error_condition_title(
        reason="mutation_surface_violation",
        violated_check="mutation_surface_violation: touched scripts/unsafe.py",
        backlog_title="Unrelated task wording",
    )
    assert title == "Mutation-surface policy rejected a change outside the allowed files: touched scripts/unsafe.py"
    assert "mutation_surface_violation" not in title


def test_error_condition_title_uses_task_when_check_has_no_detail():
    title = _error_condition_title(
        reason="gate_failed",
        violated_check="gate_failed",
        backlog_title="Add bounded import validation",
    )
    assert title == "Verification gate rejected the proposed change while attempting: Add bounded import validation"


def test_write_structured_error_records_matchable_title_and_category(tmp_path):
    repo = tmp_path / "instance_repo"
    repo.mkdir()
    wrote = _write_structured_error(
        repo_root=repo,
        cycle_id="cycle-title123456789",
        reason="gate_failed",
        violated_check="gate_failed",
        backlog_title="Add bounded import validation",
    )
    assert wrote["status"] == "created"
    written = yaml.safe_load((repo / "lessons" / "errors.yaml").read_text(encoding="utf-8"))[0]
    assert written["title"] == "Verification gate rejected the proposed change while attempting: Add bounded import validation"
    assert written["category"] == "gate_failed"
    assert written["hypothesis"] == "Cycle failed due to gate_failed."


def test_write_structured_error_second_call_same_cycle_is_already_recorded(tmp_path):
    """#1710: an executor retry re-running the same cycle_id/date computes the
    same deterministic error_id. The second call must not write again and
    must not read as write_failed -- it is proof the card is already there."""
    repo = tmp_path / "instance_repo"
    repo.mkdir()
    cycle_id = "cycle-retryabc12345"
    reason = "executor_llm_error"
    backlog_title = "Retry same cycle twice"

    first = _write_structured_error(
        repo_root=repo, cycle_id=cycle_id, reason=reason,
        violated_check=reason, backlog_title=backlog_title,
    )
    assert first["status"] == "created"
    error_id = first["error_id"]

    second = _write_structured_error(
        repo_root=repo, cycle_id=cycle_id, reason=reason,
        violated_check=reason, backlog_title=backlog_title,
    )
    assert second["status"] == "already_recorded"
    assert second["error_id"] == error_id
    assert second["error"] is None

    # No duplicate entry, no second write.
    entries = yaml.safe_load((repo / "lessons" / "errors.yaml").read_text(encoding="utf-8"))
    assert len([e for e in entries if e["id"] == error_id]) == 1


def test_write_structured_error_raises_reports_write_failed_with_exception(tmp_path, monkeypatch):
    """#1710 acceptance: a write that raises names the exception class and
    path in `error` -- a bare write_failed with nothing to chase is not
    diagnosable."""
    repo = tmp_path / "instance_repo"
    repo.mkdir()

    from pathlib import Path as _Path

    real_write_text = _Path.write_text

    def _boom(self, *args, **kwargs):
        if self.name == "errors.yaml":
            raise PermissionError("locked")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "write_text", _boom)

    result = _write_structured_error(
        repo_root=repo,
        cycle_id="cycle-boom12345678",
        reason="gate_failed",
        violated_check="gate_failed",
        backlog_title="Write raises",
    )
    assert result["status"] == "write_failed"
    assert "PermissionError" in result["error"]
    assert "errors.yaml" in result["error"]


def test_selector_provenance_reports_candidates_and_tie_break(tmp_path, monkeypatch):
    monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
    repo = _repo_with_lessons(tmp_path, errors=[
        {"id": "ERR-new", "category": "timeout", "title": "Timeout guard fails today", "root_cause": "x", "prevention": "y"},
        {"id": "ERR-old", "category": "timeout", "title": "Timeout guard fails again", "root_cause": "x", "prevention": "y"},
    ])
    provenance = selection_provenance(repo, "Fix timeout guard fails issue")
    data = provenance["errors"]
    assert data["status"] == "present"
    assert data["candidates"][0]["id"] == "ERR-new"
    assert data["selected_id"] == "ERR-new"
    assert data["tie_count"] == 2
    assert data["tie_discarded"] is True


def test_selector_provenance_distinguishes_empty_and_below_threshold(tmp_path, monkeypatch):
    monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
    empty = selection_provenance(tmp_path, "Fix timeout")
    assert empty["errors"]["status"] == "empty_corpus"
    repo = _repo_with_lessons(tmp_path, errors=[{"id": "ERR", "title": "Unrelated", "root_cause": "different", "prevention": "y"}])
    below = selection_provenance(repo, "Fix timeout")
    assert below["errors"]["status"] == "below_threshold"


def test_instrumented_selector_matches_baseline_on_real_corpus(tmp_path, monkeypatch):
    import copy
    monkeypatch.delenv("SELFEVO_LESSONS_CONTEXT_ENABLED", raising=False)
    repo = tmp_path / "repo"
    lessons = repo / "lessons"
    lessons.mkdir(parents=True)
    errors = [{"id": "E-REAL", "category": "runtime", "title": "Bridge timeout", "root_cause": "timeout"}]
    lessons.write_text if False else None
    _write_yaml(lessons / "errors.yaml", errors)
    from nanobot.runtime.lessons_context import _capped_entries
    entries = _capped_entries(lessons / "errors.yaml")
    words = _extract_words("Fix bridge timeout")
    baseline = copy.deepcopy(_best_card(entries, words, "root_cause"))
    instrumented, provenance = _best_card_with_provenance(entries, words, "root_cause")
    assert instrumented == baseline
    assert provenance["selected_id"] == baseline["id"]


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

