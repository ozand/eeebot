"""Tests for #751: the hypotheses -> priorities reader.

Covers reading candidates from the primary source
(``hypotheses/backlog.json``, ``cycle_persist._build_hypothesis_backlog_snapshot``'s
shape) and the strategist's ``durable.json``, that the writer-less
``research/hypotheses.json`` is no longer a source (#1219), the bounded
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


def _write_durable(state_dir: Path, entries: list[dict]) -> None:
    hypotheses_dir = state_dir / "hypotheses"
    hypotheses_dir.mkdir(parents=True, exist_ok=True)
    (hypotheses_dir / "durable.json").write_text(
        json.dumps({"schema": "hypothesis-durable-v1", "entries": entries}),
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


class TestResearchFeedIsNotASource:
    """#1219: ``research/hypotheses.json`` lost its writer when the planner
    module was deleted (#924) and froze on 2026-08-22. #751's intent — the
    hypothesis -> priority chain — is served from ``backlog.json`` (as #751
    itself specified) and the strategist's ``durable.json``; the frozen file
    is ignored even when present, and candidates never come from it."""

    def test_research_file_alone_yields_no_candidates(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_research(
            state_dir,
            [{"date": "2026-07-01", "cycle_id": "cycle-a", "candidates": [{"title": "Research candidate one"}]}],
        )
        assert hypothesis_backlog.top_candidates(state_dir) == []

    def test_backlog_and_durable_serve_the_chain_without_research(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Primary title"}])
        (state_dir / "hypotheses" / "durable.json").write_text(
            json.dumps({"entries": [{"hypothesis_id": "hypothesis-d1", "task_title": "Durable title"}]}),
            encoding="utf-8",
        )
        _write_research(
            state_dir,
            [{"date": "2026-07-01", "cycle_id": "cycle-a", "candidates": [{"title": "Frozen research title"}]}],
        )

        candidates = hypothesis_backlog.top_candidates(state_dir)

        assert [(c["title"], c["source"]) for c in candidates] == [
            ("Durable title", "durable"), ("Primary title", "backlog"),
        ]
        assert not any(c["source"] == "research" for c in candidates)

    def test_corrupt_research_file_is_irrelevant(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        research_dir = state_dir / "research"
        research_dir.mkdir(parents=True)
        (research_dir / "hypotheses.json").write_text("not json", encoding="utf-8")
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Primary title"}])
        assert [c["title"] for c in hypothesis_backlog.top_candidates(state_dir)] == ["Primary title"]


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

    def test_duplicate_fifo_ids_are_reminted_before_append(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_durable(state_dir, [
            {"hypothesis_id": "hyp-0022", "title": "First", "hypothesis": "claim one"},
            {"hypothesis_id": "hyp-0022", "title": "Second", "hypothesis": "claim two"},
        ])

        hypothesis_backlog.append_hypotheses(state_dir, [])

        entries = json.loads(
            (state_dir / "hypotheses" / "durable.json").read_text(encoding="utf-8")
        )["entries"]
        assert len({entry["hypothesis_id"] for entry in entries}) == 2
        assert all(entry["hypothesis_id"].startswith("hyp-") for entry in entries)
        assert all(entry["hypothesis_id"] != "hyp-0022" for entry in entries)

    def test_generated_hypothesis_ids_remain_unique_after_fifo_eviction(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_durable(state_dir, [])
        for i in range(22):
            hypothesis_backlog.append_hypotheses(
                state_dir,
                [{"title": f"Hypothesis {i}", "hypothesis": f"claim {i}"}],
            )
        entries = json.loads(
            (state_dir / "hypotheses" / "durable.json").read_text(encoding="utf-8")
        )["entries"]
        ids = [entry["hypothesis_id"] for entry in entries]
        assert len(ids) == len(set(ids)) == hypothesis_backlog.DURABLE_MAX_ENTRIES

    def test_strategist_hypothesis_ref_answers_via_demand_id(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hyp-0022", "task_title": "Fix widget"}])
        cycle_ledger.append_event(
            state_dir,
            {
                "phase": "proposed",
                "cycle_id": "c1",
                "task_title": "Fix widget",
                "serves": "demand hypothesis-cab86c3e9ed8",
                "hypothesis_ref": "hyp-0022",
            },
        )
        cycle_ledger.append_event(
            state_dir, {"phase": "outcome", "cycle_id": "c1", "outcome": "success"}
        )

        hypothesis_backlog.reconcile(state_dir)

        entry = _read_lifecycle(state_dir)["entries"]["hyp-0022"]
        assert entry["status"] == "answered"
        assert entry["answered_evidence"] == "c1"

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


class TestInconclusiveVerdictUpgrade:
    """#878 opus-review N1 fix: an answered+inconclusive hypothesis is
    re-checked on every LATER reconcile pass (the confirmed-usage source
    needs time after completion to observe usage, so day-0 is structurally
    always inconclusive for it)."""

    def test_inconclusive_upgrades_to_supported_once_confirmed(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "hypothesis h1"},
        )
        cycle_ledger.append_event(
            state_dir, {"phase": "outcome", "cycle_id": "c1", "outcome": "success"}
        )
        # First pass: no measured signal at all yet -> inconclusive.
        hypothesis_backlog.reconcile(state_dir)
        entry = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert entry["verdict"] == "inconclusive"

        # Usage evidence arrives later: demand/completed.json now shows the
        # cycle's scripts/ artifact confirmed used.
        completed_dir = state_dir / "demand"
        completed_dir.mkdir(parents=True, exist_ok=True)
        (completed_dir / "completed.json").write_text(
            json.dumps({
                "schema_version": "demand-completed-v1",
                "entries": {
                    "entry-1": {
                        "cycle_id": "c1",
                        "files_changed": ["scripts/widget.py"],
                        "confirmed": True,
                        "signal": "pycache",
                        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                },
            }),
            encoding="utf-8",
        )

        # Second (later) pass: must re-check and upgrade.
        hypothesis_backlog.reconcile(state_dir)
        entry = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert entry["verdict"] == "supported"
        assert entry["verdict_evidence"]["source"] == "confirmed_usage"

        rows = [
            json.loads(line)
            for line in (state_dir / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        verdict_rows = [r for r in rows if r.get("phase") == "hypothesis" and r.get("reason") == "verdict"]
        assert [r["verdict"] for r in verdict_rows] == ["inconclusive", "supported"]

    def test_still_inconclusive_produces_no_extra_writes(self, tmp_path):
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
        hypothesis_backlog.reconcile(state_dir)
        hypothesis_backlog.reconcile(state_dir)

        rows = [
            json.loads(line)
            for line in (state_dir / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        verdict_rows = [r for r in rows if r.get("phase") == "hypothesis" and r.get("reason") == "verdict"]
        # One event from the initial answered-transition only — repeated
        # still-inconclusive passes append nothing further.
        assert len(verdict_rows) == 1

    def test_legacy_answered_entry_with_no_verdict_field_gets_evaluated(self, tmp_path):
        """#894: an "answered" lifecycle entry that predates #878 entirely
        has NO "verdict" key at all (not the string "inconclusive" — simply
        absent). The reconcile re-eval branch used to check
        ``verdict == "inconclusive"`` and so never touched this legacy
        shape; it must now also catch ``verdict is None``."""
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        _write_microbench(state_dir, "c1", improvement_pct=10.0)
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered",
                "title": "Fix widget",
                "first_seen": "2026-01-01T00:00:00Z",
                "cycles_untouched": 0,
                "answered_evidence": "c1",
                "answered_at": "2026-01-01T00:00:00Z",
                # no "verdict" / "verdict_evidence" / "verdict_at" at all.
            },
        })

        entry_before = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert "verdict" not in entry_before

        hypothesis_backlog.reconcile(state_dir)

        entry = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert entry["verdict"] == "supported"
        assert entry["verdict_evidence"]["source"] == "microbench"
        assert entry["verdict_at"]

        rows = [
            json.loads(line)
            for line in (state_dir / "ledger" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        verdict_rows = [r for r in rows if r.get("phase") == "hypothesis" and r.get("reason") == "verdict"]
        assert len(verdict_rows) == 1
        assert verdict_rows[0]["verdict"] == "supported"

    def test_legacy_answered_entry_without_answered_evidence_skipped_quietly(self, tmp_path):
        """Guard: the re-eval path requires ``answered_evidence`` (the
        serving cycle_id) — a legacy entry missing that too must be left
        alone rather than raising or fabricating a cycle_id."""
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered",
                "title": "Fix widget",
                "first_seen": "2026-01-01T00:00:00Z",
                "cycles_untouched": 0,
                # no answered_evidence, no verdict at all.
            },
        })

        hypothesis_backlog.reconcile(state_dir)

        entry = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert "verdict" not in entry
        assert entry["status"] == "answered"

    def test_microbench_refuted_is_not_reopened_by_the_upgrade_path(self, tmp_path):
        """A verdict that is already supported/refuted (not inconclusive)
        must never be re-checked by the upgrade path — only inconclusive
        entries are eligible."""
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
        entry = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert entry["verdict"] == "refuted"

        # Even if usage evidence now claims confirmed use, the stored
        # verdict must stay "refuted" -- microbench already resolved it and
        # the entry is no longer "inconclusive".
        completed_dir = state_dir / "demand"
        completed_dir.mkdir(parents=True, exist_ok=True)
        (completed_dir / "completed.json").write_text(
            json.dumps({
                "schema_version": "demand-completed-v1",
                "entries": {
                    "entry-1": {
                        "cycle_id": "c1",
                        "files_changed": ["scripts/widget.py"],
                        "confirmed": True,
                        "signal": "pycache",
                        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                },
            }),
            encoding="utf-8",
        )
        hypothesis_backlog.reconcile(state_dir)
        entry = _read_lifecycle(state_dir)["entries"]["hypothesis-h1"]
        assert entry["verdict"] == "refuted"


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

    def test_demand_serves_format_counts_as_in_flight(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        cycle_ledger.append_event(
            state_dir,
            {"phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget", "serves": "demand hypothesis-h1"},
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

    def test_timeout_releases_a_crashed_experiment(self, tmp_path):
        """#878 opus-review Y1 fix: a proposed cycle that never got an
        outcome (crash/kill) must NOT stay in-flight forever — once its
        'proposed' row is older than IN_FLIGHT_TIMEOUT_DAYS, the lane
        releases and a new hypothesis experiment may be minted."""
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        old_ts = (
            datetime.now(timezone.utc)
            - timedelta(days=hypothesis_backlog.IN_FLIGHT_TIMEOUT_DAYS + 1)
        ).isoformat().replace("+00:00", "Z")
        cycle_ledger.append_event(
            state_dir,
            {
                "phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget",
                "serves": "hypothesis h1", "ts": old_ts,
            },
        )
        # No outcome row at all -- simulates a crashed/killed cycle.
        assert hypothesis_backlog.has_in_flight_experiment(state_dir) is False

    def test_within_timeout_still_counts_as_in_flight(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        recent_ts = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z")
        cycle_ledger.append_event(
            state_dir,
            {
                "phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget",
                "serves": "hypothesis h1", "ts": recent_ts,
            },
        )
        assert hypothesis_backlog.has_in_flight_experiment(state_dir) is True

    def test_missing_ts_conservatively_still_counts_as_in_flight(self, tmp_path):
        """A malformed row without a parseable ts cannot be confirmed as
        timed out, so it stays conservatively in-flight (fail-open toward
        the existing behavior, not toward releasing the lane)."""
        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget"}])
        path = state_dir / "ledger" / "cycles.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Hand-written row with no ts at all (append_event always sets one;
        # this simulates external/corrupt state).
        path.write_text(
            json.dumps({
                "phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget",
                "serves": "hypothesis h1",
            }) + "\n",
            encoding="utf-8",
        )
        assert hypothesis_backlog.has_in_flight_experiment(state_dir) is True

    def test_demand_allows_new_hypothesis_after_in_flight_timeout(self, tmp_path):
        """End-to-end: demand.collect_demand mints a hypothesis item again
        once the previously in-flight experiment has timed out."""
        from nanobot.runtime import demand

        state_dir = _state_dir(tmp_path)
        _write_backlog(state_dir, [{"hypothesis_id": "hypothesis-h1", "task_title": "Fix widget", "metric": "m1"}])
        old_ts = (
            datetime.now(timezone.utc)
            - timedelta(days=hypothesis_backlog.IN_FLIGHT_TIMEOUT_DAYS + 1)
        ).isoformat().replace("+00:00", "Z")
        cycle_ledger.append_event(
            state_dir,
            {
                "phase": "proposed", "cycle_id": "c1", "task_title": "Fix widget",
                "serves": "hypothesis h1", "ts": old_ts,
            },
        )
        hyps = [i for i in demand.collect_demand(state_dir, None) if i["kind"] == "hypothesis"]
        assert len(hyps) == 1
