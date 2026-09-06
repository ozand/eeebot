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
        """#798 narrowed this acceptance pair: the different-artifact
        word-overlap flagging now applies only to proposals WITHOUT a
        concrete target path (a concrete-target proposal is about that one
        artifact — see the carve-out test below)."""
        state_dir, repo = self._seeded_repo(tmp_path)
        hits = ei.find_similar(
            state_dir,
            "Create a script to monitor RAM and memory usage",
        )
        script_hits = [h for h in hits if h["kind"] == "script"]
        assert script_hits, "expected at least one script hit"
        assert any(h["path"] == "scripts/track_memory.py" and h["duplicate_suspect"] for h in script_hits)

        matched = ei.find_duplicate_script(
            state_dir, repo, "Create a script to monitor RAM and memory usage",
        )
        assert matched == "scripts/track_memory.py"

    def test_concrete_target_proposal_not_flagged_against_different_script(self, tmp_path):
        """#798 origin defect (live 2026-07-18 16:20Z): 'Archive unused
        collect_telegram_live_proof script' targeting
        scripts/collect_telegram_live_proof.py was flagged against
        existence-index:scripts/validate_telegram_live_proof.py — a
        DIFFERENT, similarly-named sibling (>=2 shared words by
        construction). A proposal with a concrete non-test target must never
        be duplicate-suspect against a script on another path."""
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(
            repo, "scripts/collect_telegram_live_proof.py",
            "collect telegram live proof evidence.",
        )
        _write_script(
            repo, "scripts/validate_telegram_live_proof.py",
            "validate telegram live proof evidence.",
        )
        ei.reindex(state_dir, repo)

        title = "Archive unused collect_telegram_live_proof script"
        target = "scripts/collect_telegram_live_proof.py"
        hits = ei.find_similar(state_dir, title, target_path=target)
        # The sibling IS found (guaranteed word overlap) but never suspect.
        assert any(h["path"] == "scripts/validate_telegram_live_proof.py" for h in hits)
        assert not any(h["duplicate_suspect"] for h in hits)
        assert ei.find_duplicate_script(state_dir, repo, title, target) is None

    def test_same_target_path_is_not_flagged(self, tmp_path):
        """A hit whose path IS the proposal's own target_path is not a
        different existing duplicate: an existing target may be extended."""
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
        # #1215: the prior attempt must have INTEGRATED for its title to
        # count as existence evidence — a refused title is not evidence.
        (results_dir / "req-prior.json").write_text(
            json.dumps({
                "request_id": "req-prior", "backlog_title": self._TITLE,
                "rollback": {"integrated": True},
            }),
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
            json.dumps({
                "request_id": "req-prior", "backlog_title": self._TITLE,
                "rollback": {"integrated": True},
            }),
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
        clean-workspace-cache proposal (no concrete target path — #798
        narrowed different-path flagging to exactly this no-target case)
        still matches an existing cleanup_caches.py (shared content words:
        workspace/cache...)."""
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/cleanup_caches.py", "clean workspace cache directories safely.")
        ei.reindex(state_dir, repo)

        matched = ei.find_duplicate_script(
            state_dir, repo,
            "Create a script to clean workspace cache directories",
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
            json.dumps({
                "request_id": "req-1", "backlog_title": "add a memory tracker script",
                "rollback": {"integrated": True},
            }),
            encoding="utf-8",
        )

        counts = ei.reindex(state_dir, repo)
        assert counts["ledger_titles_indexed"] == 1

        hits = ei.find_similar(state_dir, "memory tracker", limit=5)
        assert any(h["kind"] == "ledger_title" and h["path"] == "req-1" for h in hits)

    def test_hypothesis_files_are_not_indexed(self, tmp_path):
        """#1219: a hypothesis is a statement of something not yet done — its
        title is never evidence that an artifact exists. Neither the live
        ``hypotheses/backlog.json`` nor the writer-less
        ``research/hypotheses.json`` produces documents any more."""
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

        assert "hypotheses_indexed" not in counts
        assert counts["hypotheses_deactivated"] == 0
        assert ei.find_similar(state_dir, "disk benchmarking", limit=5) == []
        assert ei.find_similar(state_dir, "cpu governor watcher", limit=5) == []
        con = sqlite3.connect(str(ei._index_path(state_dir)))
        try:
            assert con.execute("SELECT count(*) FROM documents WHERE kind = 'hypothesis'").fetchone()[0] == 0
        finally:
            con.close()


# ─── #1219: retirement is the index's contract, not each builder's ──────────


def _active_by_kind(state_dir: Path) -> dict[str, int]:
    con = sqlite3.connect(str(ei._index_path(state_dir)))
    try:
        rows = con.execute(
            "SELECT kind, count(*) FROM documents WHERE active = 1 GROUP BY kind",
        ).fetchall()
        fts = con.execute("SELECT count(*) FROM docs_fts").fetchone()[0]
    finally:
        con.close()
    out = {kind: n for kind, n in rows}
    out["_fts_rows"] = fts
    return out


class TestRetirementContract:
    def _poisoned_index(self, tmp_path: Path) -> tuple[Path, Path]:
        """An index in the live host's shape: hypothesis documents minted by
        the pre-#1219 builder from research snapshots that no file holds any
        more, plus backlog hypotheses, plus a real script."""
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")
        con = ei._open_db(state_dir)
        try:
            for i in range(6):
                ei._upsert_document(con, "hypothesis", f"hyp-research-cycle-{i:012d}-0", f"research candidate number {i} about memory")
            ei._upsert_document(con, "hypothesis", "hyp-backlog-t1", "improve disk benchmarking for memory")
            con.commit()
        finally:
            con.close()
        return state_dir, repo

    def test_first_reindex_retires_every_hypothesis_document(self, tmp_path):
        state_dir, repo = self._poisoned_index(tmp_path)
        before = _active_by_kind(state_dir)
        assert before["hypothesis"] == 7 and before["_fts_rows"] == 7

        counts = ei.reindex(state_dir, repo)

        after = _active_by_kind(state_dir)
        assert counts["hypotheses_deactivated"] == 7
        assert "hypothesis" not in after
        assert after["script"] == 1 and after["_fts_rows"] == 1
        # Rows stay (history), inactive.
        con = sqlite3.connect(str(ei._index_path(state_dir)))
        try:
            assert con.execute("SELECT count(*) FROM documents WHERE kind = 'hypothesis' AND active = 0").fetchone()[0] == 7
        finally:
            con.close()

    def test_second_reindex_is_a_no_op(self, tmp_path):
        state_dir, repo = self._poisoned_index(tmp_path)
        ei.reindex(state_dir, repo)
        counts = ei.reindex(state_dir, repo)
        assert counts["hypotheses_deactivated"] == 0
        assert counts["scripts_deactivated"] == 0 and counts["ledger_titles_deactivated"] == 0

    def test_retired_documents_no_longer_take_gate_slots(self, tmp_path):
        """The measured harm: hypothesis docs held 36% of the gate's top-5
        slots on 400 live proposals and pushed a real script duplicate out of
        the top-5 ten times. With the corpus retired the script is found."""
        state_dir, repo = self._poisoned_index(tmp_path)
        # Pre-retirement the query's top-5 is all hypothesis text (7 docs,
        # limit 5, every one mentions "memory").
        hits = ei.find_similar(state_dir, "monitor RAM and memory usage", limit=5)
        assert hits and all(h["kind"] == "hypothesis" for h in hits)
        assert ei.find_duplicate_script(state_dir, repo, "monitor RAM and memory usage") == "scripts/track_memory.py"
        hits = ei.find_similar(state_dir, "monitor RAM and memory usage", limit=5)
        assert [h["kind"] for h in hits] == ["script"]

    def test_a_builder_that_raises_skips_retirement_for_its_kind(self, tmp_path, monkeypatch):
        """Unknown evidence is not empty evidence: a transient read error in one
        builder must not retire that corpus. The skip is reported, and the
        other kinds still retire normally."""
        state_dir, repo = self._poisoned_index(tmp_path)
        ei.reindex(state_dir, repo)  # scripts indexed, hypotheses retired
        assert _active_by_kind(state_dir) == {"script": 1, "_fts_rows": 1}

        def _boom(con, selfevo_repo):
            raise OSError("scripts/ unreadable")

        monkeypatch.setattr(ei, "_reindex_scripts", _boom)
        counts = ei.reindex(state_dir, repo)

        assert counts["retirement_skipped"] == ["script"]
        assert counts["scripts_deactivated"] == 0
        assert _active_by_kind(state_dir) == {"script": 1, "_fts_rows": 1}, "the live script corpus survived the failed pass"

    def test_every_kind_is_retired_by_the_same_rule(self, tmp_path):
        """A deleted script, a refused attempt and a hypothesis all leave the
        active set the same way — through :data:`_CORPORA`, not a rule inside
        their builder."""
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        gone = _write_script(repo, "scripts/gone_soon.py", "a script about to be deleted.")
        results = state_dir / "subagents" / "results"
        results.mkdir(parents=True)
        (results / "result-req-refused.json").write_text(json.dumps({
            "request_id": "req-refused", "backlog_title": "Create unit tests for gone soon script",
            "rollback": {"integrated": False},
        }), encoding="utf-8")
        con = ei._open_db(state_dir)
        try:
            ei._upsert_document(con, "ledger_title", "req-refused", "Create unit tests for gone soon script")
            ei._upsert_document(con, "hypothesis", "hyp-research-x-0", "some hypothesis about gone soon")
            con.commit()
        finally:
            con.close()
        first = ei.reindex(state_dir, repo)
        # The refused attempt is not evidence (#1218) and the hypothesis has no
        # builder (#1219): both retire on the first pass; the script stays.
        assert (first["scripts_deactivated"], first["ledger_titles_deactivated"], first["hypotheses_deactivated"]) == (0, 1, 1)
        assert _active_by_kind(state_dir) == {"script": 1, "_fts_rows": 1}
        gone.unlink()

        second = ei.reindex(state_dir, repo)

        assert (second["scripts_deactivated"], second["ledger_titles_deactivated"], second["hypotheses_deactivated"]) == (1, 0, 0)
        assert _active_by_kind(state_dir) == {"_fts_rows": 0}
        assert [kind for kind, _ in ei._CORPORA] == ["script", "ledger_title", "hypothesis"]


# ─── #840: related_scripts (relevance ranking for the proposer inventory) ──


class TestRelatedScripts:
    def test_returns_relevant_script_matching_query(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")
        _write_script(repo, "scripts/deploy_release.py", "deploys the latest release.")

        hits = ei.related_scripts(state_dir, repo, "track memory usage")

        assert "scripts/track_memory.py" in hits

    def test_excludes_test_files_and_dedups(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")
        _write_script(repo, "tests/test_track_memory.py", "tests for track memory usage.")

        hits = ei.related_scripts(state_dir, repo, "track memory usage")

        assert hits.count("scripts/track_memory.py") == 1
        assert not any(h.startswith("tests/") for h in hits)
        assert not any(Path(h).name.startswith("test_") for h in hits)

    def test_disabled_returns_empty(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")

        monkeypatch.setenv(ei.ENABLED_ENV, "0")
        hits = ei.related_scripts(state_dir, repo, "track memory usage")

        assert hits == []

    def test_fail_open_on_missing_repo(self, tmp_path):
        missing_state = tmp_path / "does-not-exist" / "state"
        missing_repo = tmp_path / "does-not-exist" / "repo"

        hits = ei.related_scripts(missing_state, missing_repo, "anything at all")

        assert hits == []

    def test_empty_query_returns_empty(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")

        assert ei.related_scripts(state_dir, repo, "") == []


# ─── kill switch / fail-open ─────────────────────────────────────────────────


class TestKillSwitchAndFailOpen:
    def test_kill_switch_disables_gate_helper(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")
        ei.reindex(state_dir, repo)

        monkeypatch.setenv(ei.ENABLED_ENV, "0")
        # No target_path — with the switch on this would match (see the
        # acceptance pair); #798 makes a concrete-target variant pass
        # trivially, so the no-target shape keeps this test meaningful.
        matched = ei.find_duplicate_script(
            state_dir, repo, "Create a script to monitor RAM and memory usage",
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


# ─── #1215: a refused proposal's title is not evidence the thing exists ─────


def _write_result(directory: Path, request_id: str, **fields) -> Path:
    """Write a bridge-shaped result artifact (see
    ``bridge._write_bridge_completed_result``) under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "subagent-result-v1", "request_id": request_id}
    payload.update(fields)
    path = directory / f"result-{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestRefusedTitleIsNotExistence:
    """#1215 live evidence: ``tests/test_verify_and_proof.py`` was never
    created and never touched by any commit on origin/main, yet the proposal
    to create it was refused 4 times since 2026-08-16 as a duplicate — of the
    ``ledger_title`` document minted from its own earlier refusal. A
    ``ledger_title`` document asserts that an *attempt happened*, not that an
    *artifact exists*; only an attempt that integrated (or whose target now
    exists) may suppress a repeat."""

    _TITLE = "Create unit tests for verify_and_proof script"
    _TARGET = "tests/test_verify_and_proof.py"
    _REFUSED = {
        "integrated": False,
        "reason": "existence_index_duplicate",
        "main_sha_before": "abc", "main_sha_after": "abc",
    }

    def _repo(self, tmp_path) -> tuple[Path, Path]:
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        # The subject script exists (the tests would be FOR it); the test
        # file itself — the proposal's target — is absent.
        _write_script(repo, "scripts/verify_and_proof.py", "verify cycle claims and record proof.")
        return state_dir, repo

    def _refused_attempt(self, state_dir: Path, request_id: str = "req-refused", **extra) -> Path:
        return _write_result(
            state_dir / "subagents" / "results", request_id,
            backlog_title=self._TITLE, target_path=self._TARGET,
            result_status="blocked", status="blocked",
            cycle_id=f"cycle-{request_id}",
            rollback=dict(self._REFUSED), **extra,
        )

    def _active(self, state_dir: Path, request_id: str) -> int | None:
        con = sqlite3.connect(str(ei._index_path(state_dir)))
        try:
            row = con.execute(
                "SELECT active FROM documents WHERE kind = 'ledger_title' AND path = ?",
                (request_id,),
            ).fetchone()
        finally:
            con.close()
        return None if row is None else row[0]

    # ── the failing fixture named in the issue ──────────────────────────────

    def test_refused_attempt_title_does_not_suppress_repeat(self, tmp_path):
        state_dir, repo = self._repo(tmp_path)
        self._refused_attempt(state_dir)
        assert not (repo / self._TARGET).exists()

        ei.reindex(state_dir, repo)
        matched = ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET)

        assert matched is None, (
            f"refused attempt {matched!r} was treated as proof the target exists"
        )
        # The refused title is not in the active corpus at all.
        assert self._active(state_dir, "req-refused") in (None, 0)

    def test_refused_attempt_is_not_indexed_and_is_counted(self, tmp_path):
        state_dir, repo = self._repo(tmp_path)
        self._refused_attempt(state_dir)

        counts = ei.reindex(state_dir, repo)

        assert counts["ledger_titles_indexed"] == 0
        assert counts["ledger_titles_not_integrated"] == 1
        hits = ei.find_similar(state_dir, self._TITLE, target_path=self._TARGET)
        assert not any(h["kind"] == "ledger_title" for h in hits)

    # ── the rule is FOR this case: an integrated attempt still suppresses ──

    def test_integrated_attempt_still_suppresses_repeat(self, tmp_path):
        state_dir, repo = self._repo(tmp_path)
        _write_result(
            state_dir / "subagents" / "results", "req-shipped",
            backlog_title=self._TITLE, target_path=self._TARGET,
            result_status="completed", status="completed", commits_pushed=1,
            files_changed=[self._TARGET], cycle_id="cycle-shipped",
            rollback={"integrated": True, "reason": None},
        )

        ei.reindex(state_dir, repo)
        matched = ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET)

        assert matched == "req-shipped"
        assert self._active(state_dir, "req-shipped") == 1

    def test_refused_attempt_whose_target_now_exists_still_suppresses(self, tmp_path):
        """Rule 1b: the target arrived (e.g. as a side file of another cycle)
        — the artifact exists, so the title IS evidence even though that
        particular attempt was refused."""
        state_dir, repo = self._repo(tmp_path)
        self._refused_attempt(state_dir)
        _write_script(repo, self._TARGET, "tests for verify and proof.")

        ei.reindex(state_dir, repo)
        matched = ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET)

        # The same-path tests/ script hit is deliberately never suspect, so
        # the suppression here comes from the ledger_title document — kept
        # because the target exists.
        assert matched == "req-refused"

    # ── ledger fallback for results without a rollback record ──────────────

    def test_result_without_rollback_uses_ledger_outcome_success(self, tmp_path):
        from nanobot.runtime import cycle_ledger

        state_dir, repo = self._repo(tmp_path)
        _write_result(
            state_dir / "subagents" / "results", "req-legacy",
            backlog_title=self._TITLE, target_path=self._TARGET,
            cycle_id="cycle-legacy",
        )
        cycle_ledger.record_cycle_outcome(
            state_dir, "cycle-legacy", "success", None, [self._TARGET], "cycle/legacy",
        )

        ei.reindex(state_dir, repo)
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) == "req-legacy"

    def test_result_without_rollback_and_non_success_ledger_row_is_not_evidence(self, tmp_path):
        from nanobot.runtime import cycle_ledger

        state_dir, repo = self._repo(tmp_path)
        _write_result(
            state_dir / "subagents" / "results", "req-legacy",
            backlog_title=self._TITLE, target_path=self._TARGET,
            cycle_id="cycle-legacy",
        )
        cycle_ledger.record_cycle_outcome(
            state_dir, "cycle-legacy", "skipped-duplicate", "existence_index_duplicate", [], None,
        )

        ei.reindex(state_dir, repo)
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) is None

    def test_ledger_success_in_rotated_archive_is_read(self, tmp_path):
        """The ledger rotates daily into ``cycles-YYYY-MM-DD.jsonl.gz``; an
        integrated attempt from before today must still count (#1178/#1207:
        rotation narrows every reader that only opens the active file)."""
        import gzip

        state_dir, repo = self._repo(tmp_path)
        _write_result(
            state_dir / "subagents" / "results", "req-old",
            backlog_title=self._TITLE, target_path=self._TARGET,
            cycle_id="cycle-old",
        )
        ledger_dir = state_dir / "ledger"
        ledger_dir.mkdir(parents=True)
        row = {"phase": "outcome", "cycle_id": "cycle-old", "outcome": "success"}
        with gzip.open(ledger_dir / "cycles-2026-08-20.jsonl.gz", "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

        ei.reindex(state_dir, repo)
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) == "req-old"

    # ── retirement: the host's already-poisoned index heals on first reindex

    def test_preexisting_refused_title_document_is_retired(self, tmp_path):
        """The live index already holds ``ledger_title`` documents minted from
        refused attempts under the old rule. The first reindex after deploy
        must deactivate them — the result JSON says the attempt never
        integrated."""
        state_dir, repo = self._repo(tmp_path)
        self._refused_attempt(state_dir)
        con = ei._open_db(state_dir)
        try:
            ei._upsert_document(con, "ledger_title", "req-refused", self._TITLE)
            con.commit()
        finally:
            con.close()
        assert self._active(state_dir, "req-refused") == 1

        counts = ei.reindex(state_dir, repo)

        assert counts["ledger_titles_deactivated"] == 1
        assert self._active(state_dir, "req-refused") == 0
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) is None

    def test_title_document_with_no_surviving_result_is_retired(self, tmp_path):
        """Results migrate to ``subagents/archive/`` and the archive is
        bounded — a document whose attempt can no longer be re-verified as
        integrated is not evidence of existence either. (An integrated
        artifact stays covered by the ``script`` corpus: the file exists.)"""
        state_dir, repo = self._repo(tmp_path)
        con = ei._open_db(state_dir)
        try:
            ei._upsert_document(con, "ledger_title", "req-vanished", self._TITLE)
            con.commit()
        finally:
            con.close()

        counts = ei.reindex(state_dir, repo)

        assert counts["ledger_titles_deactivated"] == 1
        assert self._active(state_dir, "req-vanished") == 0
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) is None

    def test_retired_document_is_reactivated_if_the_attempt_later_integrates(self, tmp_path):
        state_dir, repo = self._repo(tmp_path)
        path = self._refused_attempt(state_dir)
        ei.reindex(state_dir, repo)
        assert self._active(state_dir, "req-refused") in (None, 0)

        data = json.loads(path.read_text(encoding="utf-8"))
        data["rollback"] = {"integrated": True, "reason": None}
        path.write_text(json.dumps(data), encoding="utf-8")

        ei.reindex(state_dir, repo)
        assert self._active(state_dir, "req-refused") == 1
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) == "req-refused"

    # ── archive: results migrate out of results/ within the hour (#1176) ───

    def test_integrated_result_in_archive_still_suppresses(self, tmp_path):
        state_dir, repo = self._repo(tmp_path)
        _write_result(
            state_dir / "subagents" / "archive", "req-archived",
            backlog_title=self._TITLE, target_path=self._TARGET,
            rollback={"integrated": True},
        )
        # A request artifact in the same flat archive dir is not a result.
        (state_dir / "subagents" / "archive" / "request-req-archived.json").write_text(
            json.dumps({"request_id": "req-archived", "task_title": self._TITLE}),
            encoding="utf-8",
        )

        counts = ei.reindex(state_dir, repo)

        assert counts["ledger_titles_indexed"] == 1
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) == "req-archived"

    def test_same_request_in_results_and_archive_is_classified_once(self, tmp_path):
        state_dir, repo = self._repo(tmp_path)
        self._refused_attempt(state_dir)
        _write_result(
            state_dir / "subagents" / "archive", "req-refused",
            backlog_title=self._TITLE, target_path=self._TARGET,
            rollback=dict(self._REFUSED),
        )

        counts = ei.reindex(state_dir, repo)

        assert counts["ledger_titles_not_integrated"] == 1
        assert ei.find_duplicate_script(state_dir, repo, self._TITLE, self._TARGET) is None

    # ── the other direction: the script-match half is untouched ────────────

    def test_existing_script_still_refused_with_refused_title_present(self, tmp_path):
        """Non-goal guard: a proposal duplicating an EXISTING test file is
        still refused via the ``script`` corpus, whether or not a refused
        ``ledger_title`` for the same subject sits alongside."""
        state_dir, repo = self._repo(tmp_path)
        # A refused attempt for an UNRELATED subject sits in the corpus too.
        self._refused_attempt(state_dir)
        _write_script(repo, "tests/test_lessons_integrity.py", "tests for lessons integrity checks.")
        ei.reindex(state_dir, repo)

        title = "Create test suite for lessons integrity script"
        target = "tests/test_lessons_integrity_suite.py"
        hits = ei.find_similar(state_dir, title, target_path=target)
        assert any(
            h["kind"] == "script" and h["path"] == "tests/test_lessons_integrity.py"
            and h["duplicate_suspect"] for h in hits
        )
        assert ei.find_duplicate_script(state_dir, repo, title, target) == "tests/test_lessons_integrity.py"

    def test_existing_ordinary_script_still_refused(self, tmp_path):
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        _write_script(repo, "scripts/track_memory.py", "track memory usage over time.")
        self._refused_attempt(state_dir)
        ei.reindex(state_dir, repo)

        matched = ei.find_duplicate_script(
            state_dir, repo, "Create a script to monitor RAM and memory usage",
        )
        assert matched == "scripts/track_memory.py"


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

        # No "Target path:" marker — #798 narrowed different-path flagging
        # to proposals without a concrete target (a concrete-target request
        # would legitimately proceed to spawn instead).
        _seed_bridge_request(
            state_dir,
            "req-existence",
            "cycle-existence",
            task_title="Create a script to monitor RAM and memory usage",
            task="Create a script to monitor RAM and memory usage.\n",
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

        # No "Target path:" marker, mirroring the enabled-path test above —
        # this shape WOULD be skipped by the index when enabled (#798).
        _seed_bridge_request(
            state_dir,
            "req-existence-off",
            "cycle-existence-off",
            task_title="Create a script to monitor RAM and memory usage",
            task="Create a script to monitor RAM and memory usage.\n",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        decisions = [r["decision"] for r in rows if r["phase"] == "dedup"]
        assert decisions == ["proceeded"]
