"""Tests for #750: FTS5 existence index — semantic near-duplicate task dedup.

Covers the standalone ``nanobot.runtime.existence_index`` module (schema
creation, rebuild-on-corrupt, incremental reindex, the acceptance
positive/negative pair, hypothesis/ledger corpora, the kill switch, and
fail-open behavior) plus a light bridge-integration check that a semantic
near-duplicate is caught and recorded in the ledger with an
``existence-index:`` prefix, reusing the bridge-integration harness from
``tests/test_cycle_ledger.py``.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from nanobot.runtime import bridge
from nanobot.runtime import existence_index as ei
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _run,
    _seed_bridge_request,
)


def _write_script(repo: Path, rel: str, docstring: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'"""{docstring}"""\n')
    return path


class _ExplodingSubagentManager:
    """Fails the test if a subagent is ever spawned — proves the existence-
    index dedup path skips BEFORE any spawn attempt."""

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace

    async def spawn(self, **_kwargs):
        raise AssertionError("subagent should not have been spawned — existence-index dedup should have skipped")


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


# ─── schema / storage ───────────────────────────────────────────────────────


class TestSchemaAndRebuild:
    def test_reindex_creates_schema(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        repo.mkdir()

        counts = ei.reindex(state_dir, repo)

        assert "error" not in counts
        db_path = state_dir / "existence_index" / "index.sqlite"
        assert db_path.exists()
        con = sqlite3.connect(str(db_path))
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        assert {"content", "documents", "docs_fts"} <= tables
        con.close()

    def test_rebuild_on_corrupt_db(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_script(repo / "scripts", "foo.py", "does foo things.")
        (repo / "scripts").mkdir(exist_ok=True)

        db_dir = state_dir / "existence_index"
        db_dir.mkdir(parents=True)
        (db_dir / "index.sqlite").write_bytes(b"not a real sqlite database, definitely corrupt garbage bytes")

        counts = ei.reindex(state_dir, repo)

        assert "error" not in counts
        hits = ei.find_similar(state_dir, "foo things", limit=5)
        assert any(h["path"].endswith("foo.py") for h in hits)


class TestIncrementalReindex:
    def test_unchanged_hash_skips(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/foo.py", "does foo things.")

        counts1 = ei.reindex(state_dir, repo)
        assert counts1["scripts_indexed"] == 1
        assert counts1["scripts_unchanged"] == 0

        counts2 = ei.reindex(state_dir, repo)
        assert counts2["scripts_indexed"] == 0
        assert counts2["scripts_unchanged"] == 1

    def test_changed_content_reindexes(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        path = _write_script(repo, "scripts/foo.py", "does foo things.")

        ei.reindex(state_dir, repo)
        path.write_text('"""does bar things now."""\n')
        counts2 = ei.reindex(state_dir, repo)

        assert counts2["scripts_indexed"] == 1
        hits = ei.find_similar(state_dir, "bar things", limit=5)
        assert any(h["path"].endswith("foo.py") for h in hits)

    def test_deleted_file_marked_inactive_and_removed_from_fts(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        path = _write_script(repo, "scripts/foo.py", "does foo things.")

        ei.reindex(state_dir, repo)
        path.unlink()
        counts2 = ei.reindex(state_dir, repo)

        assert counts2["scripts_deactivated"] == 1
        hits = ei.find_similar(state_dir, "foo things", limit=5)
        assert not any(h["path"].endswith("foo.py") for h in hits)

        db_path = state_dir / "existence_index" / "index.sqlite"
        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT active FROM documents WHERE kind='script' AND path='scripts/foo.py'"
        ).fetchone()
        con.close()
        assert row == (0,)


# ─── acceptance pair (#750 core requirement) ───────────────────────────────


class TestAcceptancePair:
    def _seeded_repo(self, tmp_path) -> tuple[Path, Path]:
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")
        ei.reindex(state_dir, repo)
        return state_dir, repo

    def test_index_contains_track_memory(self, tmp_path):
        state_dir, _repo = self._seeded_repo(tmp_path)
        hits = ei.find_similar(state_dir, "track memory", limit=5)
        assert any(h["path"] == "scripts/track_memory.py" for h in hits)

    def test_monitor_memory_flags_track_memory_as_duplicate(self, tmp_path):
        state_dir, repo = self._seeded_repo(tmp_path)
        hits = ei.find_similar(
            state_dir,
            "Create a script to monitor RAM and memory usage",
            target_path="scripts/monitor_memory.py",
        )
        script_hits = [h for h in hits if h["kind"] == "script"]
        assert script_hits, "expected at least one script hit"
        assert any(h["path"] == "scripts/track_memory.py" and h["duplicate_suspect"] for h in script_hits)

        matched = ei.find_duplicate_script(
            state_dir, repo, "Create a script to monitor RAM and memory usage", "scripts/monitor_memory.py",
        )
        assert matched == "scripts/track_memory.py"

    def test_same_target_path_is_not_flagged(self, tmp_path):
        """A hit whose path IS the proposal's own target_path is the
        narrower _task_already_done_for_path's job (#736), not this index's —
        must not be double-flagged as a 'different existing duplicate'."""
        state_dir, repo = self._seeded_repo(tmp_path)
        matched = ei.find_duplicate_script(
            state_dir, repo, "track memory usage over time", "scripts/track_memory.py",
        )
        assert matched is None

    def test_unrelated_theme_does_not_flag(self, tmp_path):
        state_dir, repo = self._seeded_repo(tmp_path)
        hits = ei.find_similar(
            state_dir, "generate a markdown changelog", target_path="scripts/generate_changelog.py",
        )
        assert not any(h["kind"] == "script" and h["duplicate_suspect"] for h in hits)

        matched = ei.find_duplicate_script(
            state_dir, repo, "generate a markdown changelog", "scripts/generate_changelog.py",
        )
        assert matched is None


# ─── #757: intent derivation + tests-for-X carve-out ────────────────────────


class TestDeriveIntent:
    def test_tests_target_path_derives_test_for(self):
        intent = ei.derive_intent("Create test suite for approval truth normalization script",
                                  "tests/test_approval_truth.py")
        assert intent is not None
        assert intent[0] == "test-for"
        assert {"approval", "truth"} <= intent[1]

    def test_title_pattern_alone_derives_test_for(self):
        intent = ei.derive_intent("Create unit tests for backlog health script")
        assert intent == ("test-for", frozenset({"backlog", "health"}))

    def test_plain_script_target_derives_change(self):
        intent = ei.derive_intent("Create a workspace cache cleaner", "scripts/clean_workspace_cache.py")
        assert intent == ("change", "scripts/clean_workspace_cache.py")

    def test_no_target_no_pattern_derives_none(self):
        assert ei.derive_intent("Improve loop metrics reporting") is None

    def test_subject_stops_at_descriptive_tail(self):
        # Post-deploy live evidence (2026-07-14 21:06-21:16Z): the tail after
        # "script to verify ..." leaked into the subject, so unrelated test
        # suites shared >=2 words ("cycle", "verify") and the recent-failure
        # cascade survived the #757 fix.
        a = ei.derive_intent(
            "Create unit tests for cycle_logger script to verify cycle "
            "summary prepending and history formatting")
        b = ei.derive_intent(
            "Create unit tests for analyze_cycle_duration script to verify "
            "duration tracking and reporting")
        c = ei.derive_intent(
            "Create unit tests for analyze_pass_streak script to verify "
            "streak counting")
        assert a == ("test-for", frozenset({"cycle", "logger"}))
        assert ei.intents_match(a, b) is False
        assert ei.intents_match(b, c) is False
        # A reworded retry of the SAME subject still matches.
        assert ei.intents_match(
            a, ei.derive_intent("Create test suite for cycle_logger script")) is True

    def test_intents_match_same_subject_and_different_subject(self):
        a = ei.derive_intent("Create test suite for approval truth normalization script")
        b = ei.derive_intent("Create unit tests for approval truth script")
        c = ei.derive_intent("Create unit tests for backlog health script")
        assert ei.intents_match(a, b) is True
        assert ei.intents_match(a, c) is False
        assert ei.intents_match(a, None) is False


class TestTestsForXCarveOut:
    """#757 live evidence (2026-07-14 15:34Z): a tests/-target proposal whose
    title names the script under test must not be skipped as a duplicate of
    that script — but a REPEAT of the same test-for-subject proposal must be."""

    _TITLE = "Create test suite for approval truth normalization script"
    _TARGET = "tests/test_approval_truth.py"

    def _seeded_repo(self, tmp_path) -> tuple[Path, Path]:
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/approval_truth.py", "normalize approval truth records.")
        ei.reindex(state_dir, repo)
        return state_dir, repo

    def test_tests_target_proposal_not_flagged_against_named_script(self, tmp_path):
        state_dir, repo = self._seeded_repo(tmp_path)
        hits = ei.find_similar(state_dir, self._TITLE, target_path=self._TARGET)
        # The script IS found (guaranteed word overlap) but never suspect.
        assert any(h["path"] == "scripts/approval_truth.py" for h in hits)
        assert not any(h["duplicate_suspect"] for h in hits)
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) is None

    def test_second_identical_test_proposal_flagged_against_prior_attempt(self, tmp_path):
        state_dir, repo = self._seeded_repo(tmp_path)
        results_dir = state_dir / "subagents" / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "req-prior.json").write_text(
            json.dumps({"request_id": "req-prior", "backlog_title": self._TITLE}),
            encoding="utf-8",
        )
        ei.reindex(state_dir, repo)

        matched = ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET)
        assert matched == "req-prior"

    def test_test_proposal_flagged_against_existing_tests_file(self, tmp_path):
        state_dir, repo = self._seeded_repo(tmp_path)
        _write_script(repo, "tests/test_approval_truth.py", "tests for approval truth normalization.")
        ei.reindex(state_dir, repo)

        # Same subject, different filename — matched via the tests/ doc.
        matched = ei.find_duplicate_script(
            state_dir, repo, self._TITLE, "tests/test_approval_truth_normalization.py",
        )
        assert matched == "tests/test_approval_truth.py"

    def test_test_proposal_for_other_subject_not_flagged(self, tmp_path):
        state_dir, repo = self._seeded_repo(tmp_path)
        results_dir = state_dir / "subagents" / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "req-prior.json").write_text(
            json.dumps({"request_id": "req-prior", "backlog_title": self._TITLE}),
            encoding="utf-8",
        )
        ei.reindex(state_dir, repo)

        matched = ei.find_duplicate_script(
            state_dir, repo,
            "Create unit tests for backlog health script",
            "tests/test_backlog_health.py",
        )
        assert matched is None

    def test_ordinary_proposal_not_flagged_against_tests_file(self, tmp_path):
        """tests/ joined the corpus in #757 — an ordinary script proposal must
        not start matching test files (symmetric kind-aware rule)."""
        state_dir, repo = self._seeded_repo(tmp_path)
        _write_script(repo, "tests/test_workspace_cache.py", "tests workspace cache cleaning.")
        ei.reindex(state_dir, repo)

        matched = ei.find_duplicate_script(
            state_dir, repo,
            "Create a script to clean the workspace cache",
            "scripts/clean_workspace_cache.py",
        )
        assert matched is None

    def test_ordinary_true_positive_still_caught(self, tmp_path):
        """The carve-out must not weaken ordinary script dedup: a
        clean_workspace_cache proposal still matches an existing
        cleanup_caches.py (shared content words: workspace/cache...)."""
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/cleanup_caches.py", "clean workspace cache directories safely.")
        ei.reindex(state_dir, repo)

        matched = ei.find_duplicate_script(
            state_dir, repo,
            "Create a script to clean workspace cache directories",
            "scripts/clean_workspace_cache.py",
        )
        assert matched == "scripts/cleanup_caches.py"


# ─── ledger_title / hypothesis corpora ──────────────────────────────────────


class TestOtherCorpora:
    def test_ledger_title_indexed_from_results_dir(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        repo.mkdir()
        results_dir = state_dir / "subagents" / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "req-1.json").write_text(
            json.dumps({"request_id": "req-1", "backlog_title": "add a memory tracker script"}),
            encoding="utf-8",
        )

        counts = ei.reindex(state_dir, repo)
        assert counts["ledger_titles_indexed"] == 1

        hits = ei.find_similar(state_dir, "memory tracker", limit=5)
        assert any(h["kind"] == "ledger_title" and h["path"] == "req-1" for h in hits)

    def test_hypothesis_backlog_and_research_indexed(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        repo.mkdir()

        hyp_dir = state_dir / "hypotheses"
        hyp_dir.mkdir(parents=True)
        (hyp_dir / "backlog.json").write_text(
            json.dumps({"entries": [{"task_id": "t1", "task_title": "improve disk benchmarking"}]}),
            encoding="utf-8",
        )

        research_dir = state_dir / "research"
        research_dir.mkdir(parents=True)
        (research_dir / "hypotheses.json").write_text(
            json.dumps([{"cycle_id": "c1", "candidates": [{"title": "cpu governor watcher"}]}]),
            encoding="utf-8",
        )

        counts = ei.reindex(state_dir, repo)
        assert counts["hypotheses_indexed"] == 2

        hits_backlog = ei.find_similar(state_dir, "disk benchmarking", limit=5)
        assert any(h["kind"] == "hypothesis" for h in hits_backlog)

        hits_research = ei.find_similar(state_dir, "cpu governor watcher", limit=5)
        assert any(h["kind"] == "hypothesis" for h in hits_research)


# ─── kill switch / fail-open ─────────────────────────────────────────────────


class TestKillSwitchAndFailOpen:
    def test_kill_switch_disables_gate_helper(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")
        ei.reindex(state_dir, repo)

        monkeypatch.setenv(ei.ENABLED_ENV, "0")
        matched = ei.find_duplicate_script(
            state_dir, repo, "Create a script to monitor RAM and memory usage", "scripts/monitor_memory.py",
        )
        assert matched is None

    def test_enabled_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ei.ENABLED_ENV, raising=False)
        assert ei.existence_index_enabled() is True

    def test_garbage_env_value_still_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ei.ENABLED_ENV, "nonsense")
        assert ei.existence_index_enabled() is True

    def test_fail_open_on_missing_state_dir(self, tmp_path):
        missing_state = tmp_path / "does-not-exist" / "state"
        missing_repo = tmp_path / "does-not-exist" / "repo"
        # Must not raise, and must behave as "no match".
        counts = ei.reindex(missing_state, missing_repo)
        assert "error" not in counts
        hits = ei.find_similar(missing_state, "anything at all", limit=5)
        assert hits == [] or isinstance(hits, list)

    def test_find_duplicate_script_empty_title_returns_none(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        repo.mkdir()
        assert ei.find_duplicate_script(state_dir, repo, "", None) is None


# ─── bridge integration ─────────────────────────────────────────────────────


class TestBridgeExistenceIndexIntegration:
    def test_semantic_duplicate_skips_before_spawn_with_existence_index_marker(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        origin, work = _init_selfevo_repo(base)

        # Seed an existing script that is a semantic near-duplicate of the
        # incoming proposal (the #750 overnight evidence pattern).
        (work / "scripts").mkdir(exist_ok=True)
        (work / "scripts" / "track_memory.py").write_text(
            '"""track memory usage over time."""\n'
        )
        _run(work, "add", "scripts/track_memory.py")
        _run(work, "commit", "-m", "add track_memory.py")
        _run(work, "push", "origin", "HEAD:main")

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _ExplodingSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        _seed_bridge_request(
            state_dir,
            "req-existence",
            "cycle-existence",
            task_title="Create a script to monitor RAM and memory usage",
            task="Create a script to monitor RAM and memory usage.\n"
                 "Target path: scripts/monitor_memory.py\n",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        phases = [r["phase"] for r in rows]
        assert phases == ["started", "dedup", "outcome"]
        assert rows[1]["decision"] == "skipped_duplicate"
        assert rows[1]["matched_against"] == "existence-index:scripts/track_memory.py"
        assert rows[2]["outcome"] == "skipped-duplicate"

    def test_kill_switch_lets_semantic_duplicate_through_to_normal_path(self, tmp_path, monkeypatch):
        """With the gate disabled, the existence-index branch must not fire —
        proving it is a pure addition, not a replacement of the exact checks."""
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        origin, work = _init_selfevo_repo(base)

        (work / "scripts").mkdir(exist_ok=True)
        (work / "scripts" / "track_memory.py").write_text(
            '"""track memory usage over time."""\n'
        )
        _run(work, "add", "scripts/track_memory.py")
        _run(work, "commit", "-m", "add track_memory.py")
        _run(work, "push", "origin", "HEAD:main")

        monkeypatch.setenv(ei.ENABLED_ENV, "0")
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        _seed_bridge_request(
            state_dir,
            "req-existence-off",
            "cycle-existence-off",
            task_title="Create a script to monitor RAM and memory usage",
            task="Create a script to monitor RAM and memory usage.\n"
                 "Target path: scripts/monitor_memory.py\n",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        decisions = [r["decision"] for r in rows if r["phase"] == "dedup"]
        assert decisions == ["proceeded"]
