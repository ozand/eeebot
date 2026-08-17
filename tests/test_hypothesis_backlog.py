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


# ─── #878: verdict computed the same pass a hypothesis is answered ─────────


def _write_microbench(state_dir: Path, cycle_id: str, *, improvement_pct: float) -> None:
    d = state_dir / "heldout"
    d.mkdir(parents=True, exist_ok=True)
    (d / "microbench.json").write_text(
        json.dumps({
            "schema_version": "heldout-microbench-v1",
            "entries": {
                cycle_id: {
                    "module": "nanobot/runtime/existence_index.py",
                    "metric": "wall_ms_best_of_5",
                    "baseline_ms": 100.0,
                    "candidate_ms": 100.0 * (1 - improvement_pct / 100.0),
                    "improvement_pct": improvement_pct,
                    "direction": "lower",
                    "schema": "heldout-microbench-entry-v1",
                }
            },
        }),
        encoding="utf-8",
    )


class TestVerdictOnAnswer:
    def test_answered_hypothesis_gets_supported_verdict_from_microbench(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        _write_microbench(state_dir, "c1", improvement_pct=10.0)
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        cycle_ledger.append_event(
            state_dir, {"phase": "outcome", "cycle_id": "c1", "outcome": "success"}
        )

        hypothesis_backlog.reconcile(state_dir)

        entry = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert entry["status"] == "answered"
        assert entry["verdict"] == "supported"
        assert entry["verdict_evidence"]["source"] == "microbench"
        assert entry["verdict_at"]
        assert entry["title"] == "Fix widget"

    def test_answered_hypothesis_gets_inconclusive_verdict_with_no_signal(self, tmp_path):
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

        entry = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert entry["verdict"] == "inconclusive"

    def test_verdict_ledger_event_appended(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        _write_microbench(state_dir, "c1", improvement_pct=1.0)
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        cycle_ledger.append_event(
            state_dir, {"phase": "outcome", "cycle_id": "c1", "outcome": "success"}
        )

        hypothesis_backlog.reconcile(state_dir)

        path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        verdict_rows = [r for r in rows if r.get("phase") == "hypothesis" and r.get("reason") == "verdict"]
        assert len(verdict_rows) == 1
        assert verdict_rows[0]["verdict"] == "refuted"
        assert verdict_rows[0]["hypothesis_ref"] == "hypothesis-h1"
        assert verdict_rows[0]["cycle_id"] == "c1"

    def test_verdict_computed_only_once(self, tmp_path):
        """A second reconcile pass over an already-answered entry must not
        recompute/re-append the verdict (idempotent, same as the existing
        answered-status guard)."""
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        _write_microbench(state_dir, "c1", improvement_pct=10.0)
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        cycle_ledger.append_event(
            state_dir, {"phase": "outcome", "cycle_id": "c1", "outcome": "success"}
        )
        hypothesis_backlog.reconcile(state_dir)
        hypothesis_backlog.reconcile(state_dir)

        path = state_dir / "ledger" / "cycles.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        verdict_rows = [r for r in rows if r.get("phase") == "hypothesis" and r.get("reason") == "verdict"]
        assert len(verdict_rows) == 1


class TestSupportedHypotheses:
    def test_supported_hypotheses_returns_newest_first(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered", "verdict": "supported", "verdict_at": "2026-08-01T00:00:00Z",
                "verdict_evidence": {"source": "microbench"}, "title": "Older win",
            },
            "hypothesis-h2": {
                "status": "answered", "verdict": "supported", "verdict_at": "2026-08-05T00:00:00Z",
                "verdict_evidence": {"source": "confirmed_usage"}, "title": "Newer win",
            },
            "hypothesis-h3": {
                "status": "answered", "verdict": "refuted", "verdict_at": "2026-08-06T00:00:00Z",
                "title": "Not this one",
            },
        })
        result = hypothesis_backlog.supported_hypotheses(state_dir)
        assert [r["title"] for r in result] == ["Newer win", "Older win"]

    def test_supported_hypotheses_capped_to_n(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        entries = {
            f"hypothesis-h{i}": {
                "status": "answered", "verdict": "supported",
                "verdict_at": f"2026-08-0{i}T00:00:00Z", "title": f"Win {i}",
            }
            for i in range(1, 6)
        }
        _write_lifecycle(state_dir, entries)
        result = hypothesis_backlog.supported_hypotheses(state_dir, n=3)
        assert len(result) == 3

    def test_no_lifecycle_file_returns_empty(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert hypothesis_backlog.supported_hypotheses(state_dir) == []


class TestLifecycleCounts:
    def test_counts_by_status_and_verdict(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {"status": "active"},
            "hypothesis-h2": {"status": "answered", "verdict": "supported"},
            "hypothesis-h3": {"status": "answered", "verdict": "refuted"},
        })
        assert hypothesis_backlog.lifecycle_counts(state_dir) == {
            "active": 1, "answered": 2, "supported": 1, "refuted": 1, "inconclusive": 0,
        }

    def test_no_lifecycle_is_all_zero(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert hypothesis_backlog.lifecycle_counts(state_dir) == {
            "active": 0, "answered": 0, "supported": 0, "refuted": 0, "inconclusive": 0,
        }


class TestHasInFlightExperiment:
    def test_false_with_no_candidates(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert hypothesis_backlog.has_in_flight_experiment(state_dir) is False

    def test_true_when_proposed_without_outcome(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        assert hypothesis_backlog.has_in_flight_experiment(state_dir) is True

    def test_false_once_outcome_recorded(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        cycle_ledger.append_event(
            state_dir, {"phase": "outcome", "cycle_id": "c1", "outcome": "failed"}
        )
        assert hypothesis_backlog.has_in_flight_experiment(state_dir) is False

    def test_false_when_only_answered_hypothesis_has_open_cycle(self, tmp_path):
        """An already-answered hypothesis's stale in-flight-looking row must
        not count — only ACTIVE candidates are considered."""
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        _write_lifecycle(state_dir, {"hypothesis-h1": {"status": "answered"}})
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c2", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        assert hypothesis_backlog.has_in_flight_experiment(state_dir) is False
