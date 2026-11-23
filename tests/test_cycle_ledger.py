"""Tests for cycle-ledger evidence and bounded bridge lifecycle integration.

ADR-039: cycle-start rows persist a supplied exact retry key while preserving
legacy row shape when callers omit it.
Covers typed append/read helpers, bridge gate/outcome records, phase receipts,
owner-scoped provider intent dispatch and restart hold behavior.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import bridge, cycle_ledger, intent_journal, state, state_access
from nanobot.runtime.service_paths import is_delivered


def _read_ledger(state_dir: Path) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestRuleCDelivery:
    def test_git_integration_and_delivery_are_separate(self):
        service = ["diary/2026-09-21.md", "memory/MEMORY.md"]
        mixed = [*service, "scripts/feature.py"]
        assert is_delivered(True, service) is False
        assert is_delivered(True, mixed) is True
        assert is_delivered(False, mixed) is False

    def test_service_only_ledger_contract_is_partial_inconclusive_and_not_real(self, tmp_path):
        inputs = bridge._real_result_ledger_inputs({"result_status": "blocked", "status": "blocked"})
        cycle_ledger.record_cycle_outcome(
            tmp_path, "cycle-service", "partial", "service_only", [
                "diary/2026-09-21.md", "memory/MEMORY.md",
            ], "selfevo/cycle-service", verdict="inconclusive",
            verdict_reason="service_only", real_result=inputs, delivered=False,
        )
        row = _read_ledger(tmp_path)[0]
        assert (row["outcome"], row["verdict"], row["reason"], row["verdict_reason"]) == (
            "partial", "inconclusive", "service_only", "service_only",
        )
        assert row["real_result"]["is_real_result"] is False


# ─── append_event / round-trip ────────────────────────────────────────────────


class TestAppendEvent:
    def test_append_and_read_round_trip(self, tmp_path):
        cycle_ledger.append_event(tmp_path, {"phase": "started", "cycle_id": "c1"})
        cycle_ledger.append_event(tmp_path, {"phase": "outcome", "cycle_id": "c1", "outcome": "success"})

        rows = _read_ledger(tmp_path)
        assert len(rows) == 2
        assert rows[0]["phase"] == "started"
        assert rows[0]["cycle_id"] == "c1"
        assert "ts" in rows[0]
        assert rows[1]["outcome"] == "success"

    def test_creates_parent_dir(self, tmp_path):
        state_dir = tmp_path / "does" / "not" / "exist" / "yet"
        cycle_ledger.append_event(state_dir, {"phase": "started"})
        assert (state_dir / "ledger" / "cycles.jsonl").exists()

    def test_fail_open_on_unwritable_dir(self, tmp_path, monkeypatch):
        """An append that can't write (e.g. mkdir raising) must never propagate."""

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "mkdir", _boom)
        # Must not raise.
        cycle_ledger.append_event(tmp_path, {"phase": "started"})

    def test_fail_open_on_unserializable_event(self, tmp_path):
        """A non-JSON-serializable value falls back to str() via default=str,
        but if something still blows up, append_event swallows it."""
        cycle_ledger.append_event(tmp_path, {"phase": "started", "weird": object()})
        rows = _read_ledger(tmp_path)
        assert len(rows) == 1
        assert "weird" in rows[0]


# ─── typed helpers ─────────────────────────────────────────────────────────────


class TestTypedHelpers:
    def test_record_cycle_started_writes_phase_started(self, tmp_path):
        cycle_ledger.record_cycle_started(tmp_path, "cycle-1", "req-1", None)
        rows = _read_ledger(tmp_path)
        assert rows[0] == {
            "phase": "started",
            "cycle_id": "cycle-1",
            "request_id": "req-1",
            "branch": None,
            "ts": rows[0]["ts"],
        }

    def test_record_cycle_started_persists_retry_key_when_supplied(self, tmp_path):
        cycle_ledger.record_cycle_started(
            tmp_path, "cycle-1", "req-1", None, retry_key="task-secret",
        )
        row = _read_ledger(tmp_path)[0]
        assert row["phase"] == "started"
        assert row["retry_key"] == "task-secret"

    def test_record_dedup_decision_valid_values(self, tmp_path):
        for decision in cycle_ledger.VALID_DEDUP_DECISIONS:
            cycle_ledger.record_dedup_decision(tmp_path, "c1", decision, "match-x")
        rows = _read_ledger(tmp_path)
        assert {r["decision"] for r in rows} == set(cycle_ledger.VALID_DEDUP_DECISIONS)

    def test_record_dedup_decision_unknown_coerced_to_proceeded(self, tmp_path):
        cycle_ledger.record_dedup_decision(tmp_path, "c1", "bogus-decision", None)
        rows = _read_ledger(tmp_path)
        assert rows[0]["decision"] == "proceeded"

    def test_record_gate_decision_allow_and_block(self, tmp_path):
        cycle_ledger.record_gate_decision(tmp_path, "c1", True, "smoke_passed", [])
        cycle_ledger.record_gate_decision(tmp_path, "c1", False, "mutation_surface_violation", ["nanobot/x.py"])
        rows = _read_ledger(tmp_path)
        assert rows[0]["allowed"] is True
        assert rows[0]["violations"] == []
        assert rows[1]["allowed"] is False
        assert rows[1]["violations"] == ["nanobot/x.py"]

    def test_skill_hygiene_rule_name_survives_in_gate_journal(self, tmp_path):
        violation = (
            "skill duplicate batch: exact description shared by changed skills "
            "'first-skill' and 'second-skill'"
        )
        cycle_ledger.record_gate_decision(
            tmp_path, "cycle-skill", False, "mutation_surface_violation", [violation]
        )
        row = _read_ledger(tmp_path)[0]
        assert row["phase"] == "gate"
        assert row["violations"] == [violation]
        assert row["violations"][0].startswith("skill duplicate batch:")

    def test_record_cycle_outcome_lesson_candidate_is_additive(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1",
            lesson_candidate={"condition_met": True, "queued": False, "refusal_reason": "condition_equals_detail"},
        )
        row = _read_ledger(tmp_path)[0]
        assert row["lesson_candidate"] == {
            "condition_met": True,
            "queued": False,
            "refusal_reason": "condition_equals_detail",
        }

    def test_record_cycle_outcome_accepts_change_shape(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1",
            change_shape="feature",
        )
        assert _read_ledger(tmp_path)[0]["change_shape"] == "feature"

    def test_record_cycle_outcome_rejects_unknown_change_shape(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1",
            change_shape="garbage",
        )
        assert "change_shape" not in _read_ledger(tmp_path)[0]

    def test_record_cycle_outcome_valid_enum(self, tmp_path):
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")
        rows = _read_ledger(tmp_path)
        assert rows[0]["outcome"] == "success"
        assert rows[0]["files_changed"] == ["a.py"]
        assert rows[0]["branch"] == "selfevo/cycle-1"
        assert rows[0]["change_tier"] == "code-bearing"

    def test_record_cycle_outcome_doc_only_change_tier(self, tmp_path):
        cycle_ledger.record_cycle_outcome(tmp_path, "c2", "success", None, ["docs/guide.md", "memory/facts/fact.json"], "selfevo/cycle-2")
        rows = _read_ledger(tmp_path)
        assert rows[0]["change_tier"] == "doc-only"

    def test_record_cycle_outcome_invalid_enum_coerced_to_failed(self, tmp_path):
        """#720 test-plan contract: an invalid outcome value is coerced to
        'failed' rather than raising or writing free text."""
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "not-a-real-outcome", "weird", [], None)
        rows = _read_ledger(tmp_path)
        assert rows[0]["outcome"] == "failed"

    def test_record_cycle_outcome_push_pending_is_valid_not_coerced(self, tmp_path):
        """#1709: a gate-passed cycle whose push exhausted its transient
        retries is 'push_pending', not 'failed' — it must survive the
        VALID_OUTCOMES coercion, carrying the branch and reason."""
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "push_pending", "push_pending", [], "selfevo/cycle-1",
        )
        rows = _read_ledger(tmp_path)
        assert rows[0]["outcome"] == "push_pending"
        assert rows[0]["reason"] == "push_pending"
        assert rows[0]["branch"] == "selfevo/cycle-1"

    def test_record_cycle_outcome_push_pending_preserves_changed_paths(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c-service", "push_pending", "push_pending",
            ["diary/2026-09-25.md", "memory/MEMORY.md"], "selfevo/cycle-service",
            main_sha_before="abc123",
        )
        row = _read_ledger(tmp_path)[0]
        assert row["files_changed"] == ["diary/2026-09-25.md", "memory/MEMORY.md"]

    def test_record_cycle_outcome_push_pending_carries_main_sha_before(self, tmp_path):
        """#1709 increment 2: bridge._finish_pending_pushes reads this back
        to tell whether origin/main moved since the original attempt."""
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "push_pending", "push_pending", [], "selfevo/cycle-1",
            main_sha_before="abc123",
        )
        rows = _read_ledger(tmp_path)
        assert rows[0]["main_sha_before"] == "abc123"

    def test_record_cycle_outcome_without_main_sha_before_omits_the_key(self, tmp_path):
        """Additive field: every pre-#1709 call site (and any outcome other
        than push_pending) keeps a byte-identical row with no such key."""
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, [], "selfevo/cycle-1")
        rows = _read_ledger(tmp_path)
        assert "main_sha_before" not in rows[0]

    @pytest.mark.parametrize("outcome", ["pushed_late", "superseded", "abandoned"])
    def test_record_cycle_outcome_late_push_resolutions_are_valid_not_coerced(self, tmp_path, outcome):
        """#1709 increment 2: the three resolutions of a push_pending cycle
        must survive VALID_OUTCOMES coercion the same as push_pending itself."""
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", outcome, None, [], "selfevo/cycle-1")
        rows = _read_ledger(tmp_path)
        assert rows[0]["outcome"] == outcome

    # ─── #1748: real_result carries bridge._is_real_result's own inputs ───

    def test_record_cycle_outcome_without_real_result_omits_the_key(self, tmp_path):
        """Every pre-#1748 call site keeps working byte-identically: omitting
        ``real_result`` means the row carries no ``real_result`` key at all."""
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")
        rows = _read_ledger(tmp_path)
        assert "real_result" not in rows[0]

    # ADR-037 (#2076): only the existing handled-marker enum may be persisted.
    @pytest.mark.parametrize("disposition", sorted(cycle_ledger.VALID_HANDLED_DISPOSITIONS))
    def test_record_cycle_outcome_preserves_handled_disposition(self, tmp_path, disposition):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "cycle-adr037", "failed", "executor_llm_error", [], None,
            handled_disposition=disposition,
        )
        row = _read_ledger(tmp_path)[0]
        assert row["cycle_id"] == "cycle-adr037"
        assert row["handled_disposition"] == disposition

    def test_record_cycle_outcome_omits_missing_or_unknown_handled_disposition(self, tmp_path):
        """ADR-037: legacy callers and unknown values add neither key nor payload."""
        cycle_ledger.record_cycle_outcome(tmp_path, "legacy", "success", None, [], None)
        cycle_ledger.record_cycle_outcome(
            tmp_path, "unknown", "failed", None, [], None,
            handled_disposition="raw-secret-sentinel",
        )
        rows = _read_ledger(tmp_path)
        assert "handled_disposition" not in rows[0]
        assert "handled_disposition" not in rows[1]
        assert "raw-secret-sentinel" not in (tmp_path / "ledger" / "cycles.jsonl").read_text()

    def test_handled_disposition_is_not_projected_to_live_recent_outcomes(self, tmp_path):
        """ADR-037: internal disposition remains absent from operator projection."""
        cycle_ledger.record_cycle_outcome(
            tmp_path, "private-cycle", "failed", None, [], None,
            handled_disposition="retired_state_lost",
        )
        projected = state._live_recent_outcomes(tmp_path)
        assert len(projected) == 1
        assert projected[0]["cycle_id"] == "private-cycle"
        assert "handled_disposition" not in projected[0]

    def test_record_cycle_outcome_delivered_is_additive(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "partial", "service_only", ["diary/a.md"], "branch",
            delivered=False,
        )
        row = _read_ledger(tmp_path)[0]
        assert row["delivered"] is False
        assert row["outcome"] == "partial"

    def test_record_cycle_outcome_real_result_carries_inputs_plus_boolean(self, tmp_path):
        """The boolean is written ALONGSIDE the inputs, never in place of
        them — a derived flag cannot be re-derived under a changed criterion."""
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "failed", "recent_duplicate_failure", [], None,
            real_result={
                "result_status": "blocked",
                "status": "blocked",
                "terminal_reason": None,
                "materialized_from": "bridge_llm_execution",
                "blocker_reason": None,
                "is_real_result": False,
            },
        )
        row = _read_ledger(tmp_path)[0]["real_result"]
        assert row == {
            "result_status": "blocked",
            "status": "blocked",
            "terminal_reason": None,
            "materialized_from": "bridge_llm_execution",
            "blocker_reason": None,
            "is_real_result": False,
        }

    # ─── #1118: verdict is a NEW, purely additive, keyword-only field ─────

    def test_record_cycle_outcome_lesson_candidate_without_refusal_omits_reason(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "success", None, [], None,
            lesson_candidate={"condition_met": False, "queued": False},
        )
        candidate = _read_ledger(tmp_path)[0]["lesson_candidate"]
        assert candidate == {"condition_met": False, "queued": False}

    def test_record_cycle_outcome_without_verdict_omits_the_key_entirely(self, tmp_path):
        """Every pre-#1118 positional call site keeps working byte-identically:
        omitting ``verdict`` means the row carries no ``verdict``/
        ``verdict_reason`` key at all — not even ``None`` — so the shape is
        indistinguishable from before #1118."""
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")
        rows = _read_ledger(tmp_path)
        assert "verdict" not in rows[0]
        assert "verdict_reason" not in rows[0]

    def test_record_cycle_outcome_with_valid_verdict_and_reason(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "skipped-duplicate", "already_done", [], None,
            verdict="reject", verdict_reason="already_done",
        )
        rows = _read_ledger(tmp_path)
        assert rows[0]["outcome"] == "skipped-duplicate"  # untouched, byte-identical
        assert rows[0]["verdict"] == "reject"
        assert rows[0]["verdict_reason"] == "already_done"

    def test_record_cycle_outcome_verdict_without_reason_omits_reason_key(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "success", None, ["a.py"], None, verdict="accept",
        )
        rows = _read_ledger(tmp_path)
        assert rows[0]["verdict"] == "accept"
        assert "verdict_reason" not in rows[0]

    def test_record_cycle_outcome_invalid_verdict_is_dropped_not_coerced(self, tmp_path):
        """Unlike ``outcome`` (coerced to 'failed'), an unrecognized ``verdict``
        is simply omitted — an absent verdict is itself a valid, honest state."""
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "success", None, [], None, verdict="maybe",
        )
        rows = _read_ledger(tmp_path)
        assert "verdict" not in rows[0]

    def test_record_cycle_outcome_exact_dict_shape_still_matches_pre_1118(self, tmp_path):
        """Sibling of the exact-equality check on record_cycle_started's
        output (see TestAppendEvent above): confirms the OMITTED-verdict row
        shape is unchanged, exhaustively, not just on the keys this test file
        happens to assert elsewhere."""
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")
        rows = _read_ledger(tmp_path)
        row = dict(rows[0])
        row.pop("ts", None)
        assert row == {
            "phase": "outcome",
            "cycle_id": "c1",
            "outcome": "success",
            "reason": None,
            "files_changed": ["a.py"],
            "branch": "selfevo/cycle-1",
            "change_tier": "code-bearing",
        }

    def test_one_terminal_row_per_cycle(self, tmp_path):
        """Exercising the full helper set for one cycle produces exactly one
        'outcome'-phase row (the others are 'started'/'dedup'/'gate')."""
        cycle_ledger.record_cycle_started(tmp_path, "c1", "req-1", None)
        cycle_ledger.record_dedup_decision(tmp_path, "c1", "proceeded", None)
        cycle_ledger.record_gate_decision(tmp_path, "c1", True, "smoke_passed", [])
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")

        rows = _read_ledger(tmp_path)
        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert len(outcome_rows) == 1
        assert [r["phase"] for r in rows] == ["started", "dedup", "gate", "outcome"]

    def test_record_cycle_outcome_records_box_iteration_metrics(self, tmp_path):
        """#1850: record actual iteration consumption, limit active in cycle,
        fraction, and forecast placeholder."""
        # Case 1: cycle completed early within a limit of 80
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1",
            iterations_used=12,
            iterations_limit=80,
            iterations_predicted=15,
        )
        # Case 2: cycle hit the cap under a different limit (e.g. 40)
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c2", "failed", "max_iterations_reached", [], "selfevo/cycle-2",
            iterations_used=40,
            iterations_limit=40,
            iterations_predicted=None,
        )
        # Case 3: legacy / omitted values do not corrupt pre-1850 shape
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c3", "success", None, [], "selfevo/cycle-3",
        )

        rows = _read_ledger(tmp_path)
        r1 = next(r for r in rows if r["cycle_id"] == "c1")
        assert r1["iterations_used"] == 12
        assert r1["iterations_limit"] == 80
        assert r1["iteration_fraction"] == 0.15
        assert r1["iterations_predicted"] == 15

        r2 = next(r for r in rows if r["cycle_id"] == "c2")
        assert r2["iterations_used"] == 40
        assert r2["iterations_limit"] == 40
        assert r2["iteration_fraction"] == 1.0
        assert "iterations_predicted" not in r2

        r3 = next(r for r in rows if r["cycle_id"] == "c3")
        assert "iterations_used" not in r3
        assert "iterations_limit" not in r3
        assert "iteration_fraction" not in r3
        assert "iterations_predicted" not in r3


def test_run_window_is_state_access_owned_and_reports_retained_rows(tmp_path):
    from nanobot import crash_record

    state = tmp_path / "state"
    crash_record._start_run_marker(state)
    crash_record.record_exit(state, outcome="success", exit_status=0)
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    window = state_access.run_window(state, since_ts=since)
    assert window.status == "complete"
    assert len(window.rows) == 1
    assert window.rows[0]["phase"] == "run_end"


def test_run_window_beyond_retention_is_explicit(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(days=100)
    state = tmp_path / "state"
    run_dir = state / "bridge"
    run_dir.mkdir(parents=True)
    archive = run_dir / "runs-2020-01-01.jsonl.gz"
    import gzip
    with gzip.open(archive, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"phase": "run_end", "duration_s": 10}) + "\\n")
    window = state_access.run_window(state, since_ts=old.isoformat().replace("+00:00", "Z"))
    assert window.status == "unavailable"
    assert window.rows == ()
    assert "beyond_retention" in window.notes
    assert "no_retained_rows" in window.notes


def test_run_window_pruned_store_is_unavailable_not_empty(tmp_path):
    state = tmp_path / "state"
    (state / "bridge").mkdir(parents=True)
    window = state_access.run_window(
        state, since_ts=(datetime.now(timezone.utc) - timedelta(days=100)).isoformat().replace("+00:00", "Z"),
    )
    assert window.status == "unavailable"
    assert "beyond_retention" in window.notes


# ─── planning session (#1852) ──────────────────────────────────────────────────


class TestRecordPlanningSession:
    def test_records_success_with_both_iteration_counts(self, tmp_path):
        cycle_ledger.record_planning_session(
            tmp_path, "c1", "integrated", iterations_used=7, iterations_planned=25,
        )
        rows = _read_ledger(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["phase"] == "planning_session"
        assert row["outcome"] == "integrated"
        assert row["iterations_used"] == 7
        assert row["iterations_planned"] == 25

    def test_records_parse_format_metadata(self, tmp_path):
        cycle_ledger.record_planning_session(
            tmp_path, "c1", "integrated", iterations_used=3, iterations_planned=None,
            parse_mode="fenced", format_violation="prose_prefix",
        )
        row = _read_ledger(tmp_path)[0]
        assert row["parse_mode"] == "fenced"
        assert row["format_violation"] == "prose_prefix"
        assert row["iterations_planned"] is None

    def test_missing_forecast_stays_none_not_zero(self, tmp_path):
        """#1850-class distinction: a session that never produced a forecast
        must not be recorded as having forecast zero."""
        cycle_ledger.record_planning_session(
            tmp_path, "c1", "spawn_failed", iterations_used=None, iterations_planned=None,
            reason="subagent spawn error",
        )
        row = _read_ledger(tmp_path)[0]
        assert row["iterations_planned"] is None
        assert row["iterations_used"] is None
        assert row["reason"] == "subagent spawn error"

    def test_no_plan_is_a_distinct_outcome(self, tmp_path):
        cycle_ledger.record_planning_session(
            tmp_path, 'c1', 'no_plan', iterations_used=20,
            iterations_planned=None, reason='iteration_budget_no_final',
        )
        row = _read_ledger(tmp_path)[0]
        assert row['outcome'] == 'no_plan'
        assert row['iterations_used'] == 20
        assert row['reason'] == 'iteration_budget_no_final'

    def test_unrecognized_outcome_coerces_to_malformed(self, tmp_path):
        cycle_ledger.record_planning_session(
            tmp_path, "c1", "made_up_outcome", iterations_used=1, iterations_planned=None,
        )
        row = _read_ledger(tmp_path)[0]
        assert row["outcome"] == "malformed"


# ─── rotation ──────────────────────────────────────────────────────────────────


class TestRotation:
    def test_rotates_stale_active_file_to_gz(self, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        active = ledger_dir / "cycles.jsonl"
        active.write_text('{"phase": "started"}\n', encoding="utf-8")

        import os

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
        os.utime(active, (yesterday, yesterday))

        # New append triggers rotation before writing the new line.
        cycle_ledger.append_event(tmp_path, {"phase": "started", "cycle_id": "today"})

        gz_files = list(ledger_dir.glob("cycles-*.jsonl.gz"))
        assert len(gz_files) == 1
        with gzip.open(gz_files[0], "rt", encoding="utf-8") as fh:
            archived = fh.read()
        assert '"phase": "started"' in archived

        # Active file now contains only today's fresh row.
        rows = _read_ledger(tmp_path)
        assert len(rows) == 1
        assert rows[0]["cycle_id"] == "today"

    def test_prunes_archives_past_retention(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "5")
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()

        old_day = (datetime.now(timezone.utc).date() - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_day = (datetime.now(timezone.utc).date() - timedelta(days=1)).strftime("%Y-%m-%d")
        old_gz = ledger_dir / f"cycles-{old_day}.jsonl.gz"
        recent_gz = ledger_dir / f"cycles-{recent_day}.jsonl.gz"
        with gzip.open(old_gz, "wt", encoding="utf-8") as fh:
            fh.write('{"phase": "started"}\n')
        with gzip.open(recent_gz, "wt", encoding="utf-8") as fh:
            fh.write('{"phase": "started"}\n')

        cycle_ledger.append_event(tmp_path, {"phase": "started", "cycle_id": "new"})

        assert not old_gz.exists()
        assert recent_gz.exists()

    def test_retention_env_default_and_override(self, monkeypatch):
        monkeypatch.delenv("CYCLE_LEDGER_RETENTION_DAYS", raising=False)
        assert cycle_ledger._retention_days() == 90
        monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "10")
        assert cycle_ledger._retention_days() == 10
        monkeypatch.setenv("CYCLE_LEDGER_RETENTION_DAYS", "not-a-number")
        assert cycle_ledger._retention_days() == 90


# ─── light bridge integration ─────────────────────────────────────────────────


def _git(repo: Path) -> list[str]:
    return ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(_git(repo) + list(args), capture_output=True, text=True)


def _init_selfevo_repo(base: Path) -> tuple[Path, Path]:
    """Build a bare 'origin' + a working clone at base/'eeebot-self-evolving',
    matching bridge.py's ``STATE_DIR.parent / 'eeebot-self-evolving'`` layout.
    """
    origin = base / "origin.git"
    work = base / "eeebot-self-evolving"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _run(work, "config", "user.email", "bridge@test.local")
    _run(work, "config", "user.name", "bridge-test")
    _run(work, "checkout", "-B", "main")
    (work / "tests").mkdir()
    (work / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    # Pytest otherwise discovers ancestor config (e.g. a workstation-wide
    # pyproject.toml with coverage addopts) when this synthetic clone lives
    # under tmp_path. Keep the bridge's real smoke subprocess rooted in this
    # disposable repo without inheriting unrelated host/test-runner settings.
    (work / "pytest.ini").write_text(
        "[pytest]\naddopts = -p no:cacheprovider\n", encoding="utf-8",
    )
    (work / "mod.py").write_text("def ok():\n    return True\n")
    _run(work, "add", ".")
    _run(work, "commit", "-m", "init")
    _run(work, "push", "origin", "HEAD:main")
    return origin, work


def _seed_bridge_request(state_dir: Path, request_id: str, cycle_id: str, **extra) -> None:
    # #1222: the bridge resolves the active goal from goals/goal_text.json
    # (operator canon); the coordinator's outbox/registry are not read.
    (state_dir / "goals").mkdir(parents=True, exist_ok=True)
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": "goal-1", "text": "test goal"}),
        encoding="utf-8",
    )
    (state_dir / "subagents" / "requests").mkdir(parents=True, exist_ok=True)
    req = {"request_id": request_id, "cycle_id": cycle_id, **extra}
    (state_dir / "subagents" / "requests" / f"{request_id}.json").write_text(
        json.dumps(req), encoding="utf-8",
    )


class _FakeSkillHygieneSubagentManager:
    """Commit the proven same-batch duplicate-description incident shape."""

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace
        self._state_dir = _kwargs.get("skill_fitness_state_dir")
        self._running_tasks: dict = {}

    async def spawn(self, **_kwargs):
        description = "The same structurally valid description for both changed skills"
        for name in ("first-skill", "second-skill"):
            path = self.workspace / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "---\n"
                f"name: {name}\n"
                f"description: {description}\n"
                "---\n\n# Skill\n## Usage\nPerform the bounded workflow.\n",
                encoding="utf-8",
            )
        _run(self.workspace, "add", "skills")
        _run(self.workspace, "commit", "-m", "feat: add duplicate skill batch")
        # Round 2, item 1: completion is positive-only -- production always
        # writes 'ok' terminal telemetry for a finished executor.
        if self._state_dir is not None:
            task_id = f"skill-hygiene-{id(self)}"
            (Path(self._state_dir) / "subagents").mkdir(parents=True, exist_ok=True)
            (Path(self._state_dir) / "subagents" / f"{task_id}.json").write_text(
                json.dumps({"status": "ok", "summary": "done", "result": "done"}), encoding="utf-8",
            )

            async def _noop():
                return None

            self._running_tasks[task_id] = asyncio.create_task(_noop())
        return "fake skill subagent spawned"


class _FakeSubagentManager:
    """Stand-in for nanobot.agent.subagent.SubagentManager: simulates a
    subagent that commits one real change directly to the (already
    cycle-branched) workspace repo, without spawning a real LLM turn.

    #1852: the bridge now also spawns a SECOND, EARLIER SubagentManager for
    the planning session, via the same (monkeypatched) class -- telling the
    two apart by ``telemetry_component`` and making the planner's spawn a
    true no-op (no file, no commit) is what keeps every existing "exactly
    one spawn"/"instances[0] is the executor" test in this suite correct
    without each of them needing to know the planner exists at all.
    """

    def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
        self.workspace = workspace
        self._telemetry_component = telemetry_component
        self._state_dir = _kwargs.get("skill_fitness_state_dir")
        self._running_tasks: dict = {}

    async def spawn(self, **_kwargs):
        if self._telemetry_component == "planner":
            return "fake planner spawned (noop)"
        (self.workspace / "scripts").mkdir(exist_ok=True)
        (self.workspace / "scripts" / "feature.py").write_text("def feature():\n    return 42\n")
        _run(self.workspace, "add", "scripts/feature.py")
        _run(self.workspace, "commit", "-m", "feat: add feature")
        # Round 2 external re-check, item 1 (architect resolution
        # 2026-09-26): completion is positive-only -- a real subagent
        # always writes terminal telemetry with status: "ok" on success
        # (nanobot/agent/subagent.py's `_write_subagent_telemetry`).
        # Without a durable state_dir (a caller that never passed
        # `skill_fitness_state_dir` through to the real SubagentManager
        # constructor) there is nowhere to write it -- same as any other
        # caller-shape gap, left as a no-op rather than guessed at.
        if self._state_dir is not None:
            task_id = f"fake-executor-{id(self)}"
            (Path(self._state_dir) / "subagents").mkdir(parents=True, exist_ok=True)
            (Path(self._state_dir) / "subagents" / f"{task_id}.json").write_text(
                json.dumps({"status": "ok", "summary": "done", "result": "done"}), encoding="utf-8",
            )

            async def _noop():
                return None

            self._running_tasks[task_id] = asyncio.create_task(_noop())
        return "fake subagent spawned"


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch, tmp_path):
    """Mirrors tests/test_bridge_cycle_branch.py: point the bounded gate's
    core-smoke set at the one test file these fixtures create.
    """
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    # ADR-034 rule 3: should_propose/build_context/bridge.py's executor
    # gate all now hard-require a real release charter to proceed.
    _adr034_release_root = tmp_path / "_adr034_release_root"
    _adr034_release_root.mkdir(exist_ok=True)
    (_adr034_release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", _adr034_release_root)


def _stub_planning_session(monkeypatch, plan_text: str = "a novel bounded increment") -> None:
    """ADR-035 rule 1 (#1942): req/task now come from the planning session's
    own plan, never from a rotation-picked queue file -- stand in a plan
    that survives the pre-spawn dedup gates untouched (no prior result to
    collide with) so these integration tests still exercise the executor
    spawn/gate/outcome machinery, not planner decision-making."""
    async def _fake_planning_session(**_kwargs):
        return {
            'ran': True, 'iterations_used': 1, 'iterations_planned': 1,
            'tampered_files': [], 'plan': {'plan': plan_text, 'candidate_id': None},
        }

    monkeypatch.setattr(bridge, "_run_planning_session", _fake_planning_session)


def _executor_provider_restart_worker(state_dir: Path, stage: str) -> None:
    """Exercise one lost executor send, then verify a fresh process holds it."""
    import pytest

    from nanobot.agent.subagent import SubagentManager as RealManager
    from nanobot.config.schema import Config
    from nanobot.providers.litellm_provider import LiteLLMProvider

    patch = pytest.MonkeyPatch()
    TestBridgeIntegrationLedgerRows._enable_intent_journal_for_windows(patch)
    TestBridgeIntegrationLedgerRows._enable_test_lock(patch)
    base = state_dir.parent
    patch.setattr(bridge, "STATE_DIR", state_dir)
    patch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    patch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    bridge._CORE_SMOKE_TESTS = ("tests/test_smoke.py",)
    release_root = base / "release"
    release_root.mkdir(exist_ok=True)
    (release_root / "goals.md").write_text("test charter", encoding="utf-8")
    patch.setattr(bridge, "RELEASE_ROOT", release_root)
    config = Config()
    config.agents.defaults.model = "gpt-4o-mini"
    patch.setattr(bridge, "load_config", lambda *_args: config)
    patch.setattr(bridge, "_make_provider", lambda _config: LiteLLMProvider(
        api_key="fake", api_base="https://mock.invalid/v1", default_model="gpt-4o-mini",
    ))

    if stage == "dispatch":
        _init_selfevo_repo(base)
        patch.setattr(bridge, "SubagentManager", RealManager)
        _stub_planning_session(patch)
        _seed_bridge_request(state_dir, "req-process-loss", "cycle-process-loss")
        owners = []
        original_begin = bridge._begin_bridge_cycle_parent

        def capture_owner(state_root):
            owner = original_begin(state_root)
            owners.append(owner)
            return owner

        patch.setattr(bridge, "_begin_bridge_cycle_parent", capture_owner)
        import litellm
        import openai
        from openai import _base_client
        sdk_httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx
        sends = state_dir / "mock-transport-sends.txt"

        def transport(request):
            assert len(owners) == 1
            rows = intent_journal.read_owner_records(state_dir, owners[0])
            assert rows[-1]["event"] == "intent" and rows[-1]["target"] == "provider.chat"
            parent = json.loads((state_dir / "intent-journal" / f"{owners[0]}.parent.json").read_text())
            assert parent["active_operation_id"] == rows[-1]["operation_id"]
            with sends.open("a", encoding="utf-8") as stream:
                stream.write("send\n")
            raise sdk_httpx.ConnectError("simulated lost response", request=request)

        default_client = openai.DefaultAsyncHttpxClient

        class MockClient(default_client):
            def __init__(self, **kwargs):
                kwargs.pop("transport", None)
                super().__init__(transport=sdk_httpx.MockTransport(transport), **kwargs)

        patch.setattr(openai, "DefaultAsyncHttpxClient", MockClient)
        patch.setattr(litellm, "num_retries", None)
        patch.setattr(litellm, "route_all_chat_openai_to_responses", False)
        assert asyncio.run(bridge.main()) == 0
        assert len(owners) == 1
        assert [row["event"] for row in intent_journal.read_owner_records(
            state_dir, owners[0],
        ) if row.get("target") == "provider.chat"] == ["intent"]
        (state_dir / "executor-owner-id.txt").write_text(owners[0], encoding="utf-8")
        return

    if stage == "restart":
        entry = state_dir / "restart-entry.txt"
        _original_begin = bridge._begin_bridge_cycle_parent

        def capture_restart_owner(root):
            entry.write_text("entered", encoding="utf-8")
            return _original_begin(root)

        patch.setattr(bridge, "_begin_bridge_cycle_parent", capture_restart_owner)
        patch.setattr(bridge.llm_proposer, "maybe_propose", lambda *_a, **_k: (
            state_dir / "restart-proposer.txt"
        ).write_text("called", encoding="utf-8"))
        async def unexpected_planner(**_kwargs):
            (state_dir / "restart-planner.txt").write_text("called", encoding="utf-8")
            raise AssertionError("held owner must block planner dispatch")
        patch.setattr(bridge, "_run_planning_session", unexpected_planner)
        patch.setattr(bridge, "_make_provider", lambda _config: (
            (state_dir / "restart-provider.txt").write_text("called", encoding="utf-8"), object(),
        )[1])
        assert asyncio.run(bridge.main()) == 0
        return
    raise AssertionError(f"unknown executor restart worker stage: {stage}")


def _checkpoint_restart_worker(state_dir: Path, stage: str) -> None:
    """Run real bridge integration, then recover its durable receipt in a fresh process."""
    import os

    import pytest

    from nanobot.runtime import phase_checkpoint

    base = state_dir.parent
    repo = base / "eeebot-self-evolving"
    patch = pytest.MonkeyPatch()
    TestBridgeIntegrationLedgerRows._enable_intent_journal_for_windows(patch)
    TestBridgeIntegrationLedgerRows._enable_test_lock(patch)
    patch.setattr(bridge, "STATE_DIR", state_dir)
    patch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    patch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    patch.setattr(bridge, "_make_provider", lambda _config: object())
    integrations = state_dir / "integration-calls.txt"
    pushes = state_dir / "integration-pushes.txt"

    def _count_actual_push(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", [])
        if isinstance(command, (list, tuple)) and "push" in command and any(
            str(part).startswith("--force-with-lease=main:") for part in command
        ):
            with pushes.open("a", encoding="utf-8") as handle:
                handle.write("pushed\n")
        return original_run(*args, **kwargs)

    original_run = subprocess.run
    patch.setattr(subprocess, "run", _count_actual_push)

    if stage == "crash":
        _init_selfevo_repo(base)
        patch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        bridge._CORE_SMOKE_TESTS = ("tests/test_smoke.py",)
        release_root = base / "release"
        release_root.mkdir()
        (release_root / "goals.md").write_text("test charter", encoding="utf-8")
        patch.setattr(bridge, "RELEASE_ROOT", release_root)
        _stub_planning_session(patch)
        _seed_bridge_request(state_dir, "req-restart", "cycle-restart")
        integrate = bridge._integrate_cycle_to_main

        def _count_initial_integration(*args, **kwargs):
            result = integrate(*args, **kwargs)
            with integrations.open("a", encoding="utf-8") as handle:
                handle.write("integrated\n")
            return result

        patch.setattr(bridge, "_integrate_cycle_to_main", _count_initial_integration)
        complete = bridge._complete_bridge_checkpoint_phase

        def _exit_after_integration(*args, **kwargs):
            result = complete(*args, **kwargs)
            if kwargs["phase"] == "integration":
                os._exit(93)
            return result

        patch.setattr(bridge, "_complete_bridge_checkpoint_phase", _exit_after_integration)
        asyncio.run(bridge.main())
        raise AssertionError("expected process exit after durable integration receipt")

    patch.undo()
    if stage != "restart":
        raise AssertionError(f"unknown worker stage: {stage}")
    patch = pytest.MonkeyPatch()
    TestBridgeIntegrationLedgerRows._enable_intent_journal_for_windows(patch)
    TestBridgeIntegrationLedgerRows._enable_test_lock(patch)
    patch.setattr(bridge, "STATE_DIR", state_dir)
    patch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    patch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    patch.setattr(bridge, "_make_provider", lambda _config: object())
    patch.setattr(bridge.llm_proposer, "maybe_propose", lambda *_a, **_k: None)
    release_root = base / "release"
    patch.setattr(bridge, "RELEASE_ROOT", release_root)
    raw = json.loads((state_dir / "subagent_bridge" / "phase_checkpoint.json").read_text())
    owner_id = raw["owner_id"]
    integrate = bridge._integrate_cycle_to_main

    def _count_integration(*args, **kwargs):
        result = integrate(*args, **kwargs)
        with integrations.open("a", encoding="utf-8") as handle:
            handle.write("integrated\n")
        return result

    patch.setattr(bridge, "_integrate_cycle_to_main", _count_integration)
    planner_calls = []

    async def _no_plan(**_kwargs):
        planner_calls.append("called")
        return {"ran": True, "iterations_used": 1, "iterations_planned": None,
                "tampered_files": [], "plan": None, "terminal_outcome": "no_plan"}

    patch.setattr(bridge, "_run_planning_session", _no_plan)
    assert asyncio.run(bridge.main()) == 0
    assert integrations.read_text(encoding="utf-8").splitlines() == ["integrated"]
    assert pushes.read_text(encoding="utf-8").splitlines() == ["pushed"]
    assert planner_calls == ["called"]
    checkpoint = phase_checkpoint.load_checkpoint(state_dir, owner_id=owner_id)
    assert checkpoint is not None and checkpoint.phase == "integration" and checkpoint.status == "complete"
    main_sha = _run(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    assert checkpoint.memos["integration_main_sha"] == main_sha
    assert phase_checkpoint.verify_completed_phase(
        state_dir, owner_id=owner_id, cycle_id=checkpoint.cycle_id, repo_root=repo,
    ) == checkpoint
    assert bridge._prepare_repository_for_cycle(repo, state_dir)["restored"] is True
    assert integrations.read_text(encoding="utf-8").splitlines() == ["integrated"]
    assert pushes.read_text(encoding="utf-8").splitlines() == ["pushed"]
    snapshot = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
    assert snapshot is not None and snapshot.route_status == "closed" and snapshot.owner_id != owner_id
    assert snapshot.no_execution_receipt is not None and snapshot.no_execution_receipt[1] == "no_plan"
    phases = [row["phase"] for row in intent_journal.read_owner_records(state_dir, owner_id)
              if row["event"] == "phase_consumed"]
    assert phases == ["post_execution", "smoke_gate", "integration"]


class TestBridgeIntegrationLedgerRows:
    def test_selected_integration_receipt_recovery_across_process_restart(self, tmp_path):
        import sys

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_bridge_request(state_dir, "req-restart", "cycle-restart")
        script = (
            "import runpy, sys; from pathlib import Path; "
            "runpy.run_path('tests/test_cycle_ledger.py')['_checkpoint_restart_worker']("
            "Path(sys.argv[1]), sys.argv[2])"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", script, str(state_dir), "crash"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        assert crashed.returncode == 93, crashed.stderr
        checkpoint = json.loads(
            (state_dir / "subagent_bridge" / "phase_checkpoint.json").read_text(encoding="utf-8"),
        )
        assert checkpoint["phase"] == "integration" and checkpoint["status"] == "complete"
        assert checkpoint["memos"]["smoke_gate_receipt"] == {
            "passed": True, "tested_head": checkpoint["head_sha"],
        }
        assert checkpoint["memos"]["integration_main_sha"]
        restarted = subprocess.run(
            [sys.executable, "-c", script, str(state_dir), "restart"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        assert restarted.returncode == 0, restarted.stderr
        assert "no plan produced this cycle" in restarted.stdout
        assert "integrate:" not in restarted.stdout

    @staticmethod
    def _run_under_test_lock(monkeypatch):
        TestBridgeIntegrationLedgerRows._enable_test_lock(monkeypatch)
        return asyncio.run(bridge.main())

    @staticmethod
    def _enable_test_lock(monkeypatch):
        class _LockHandle:
            def close(self):
                pass

        monkeypatch.setattr(bridge, "_acquire_bridge_lock", lambda _state_dir: _LockHandle())

    @staticmethod
    def _enable_intent_journal_for_windows(monkeypatch):
        """Supply only process-local lock/fsync primitives for logic tests on Windows."""
        import os
        import threading
        from contextlib import contextmanager

        if os.name != "nt":
            return
        locks = {}
        guard = threading.Lock()

        @contextmanager
        def _lock(path):
            key = str(path.resolve())
            path.touch(exist_ok=True)
            with guard:
                lock = locks.setdefault(key, threading.Lock())
            with lock:
                yield

        monkeypatch.setattr(intent_journal, "_owner_lock", _lock)
        monkeypatch.setattr(intent_journal, "_fsync_directory", lambda _path: None)

    def test_skill_hygiene_violation_reaches_production_gate_journal(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        self._enable_intent_journal_for_windows(monkeypatch)
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSkillHygieneSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        monkeypatch.setenv("SUBAGENT_BRIDGE_MAX_REVISIONS", "0")
        _stub_planning_session(monkeypatch)

        _seed_bridge_request(state_dir, "req-skill", "cycle-skill")
        assert asyncio.run(bridge._main_impl()) == 0

        gate_rows = [row for row in _read_ledger(state_dir) if row["phase"] == "gate"]
        assert gate_rows[-1]["allowed"] is False
        assert gate_rows[-1]["reason"] == "mutation_surface_violation"
        assert any(
            violation.startswith("skill duplicate batch:")
            for violation in gate_rows[-1]["violations"]
        )

    def test_started_bridge_owner_holds_before_proposer_and_keeps_same_owner(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._enable_intent_journal_for_windows(monkeypatch)
        _init_selfevo_repo(tmp_path)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "target_workspace")
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        proposer_calls = []
        planning_calls = []
        executor_spawns = []
        monkeypatch.setattr(
            bridge.llm_proposer, "maybe_propose",
            lambda *_a, **_k: proposer_calls.append("called"),
        )
        monkeypatch.setattr(bridge, "SubagentManager", lambda **_kwargs: executor_spawns.append("spawn"))
        _seed_bridge_request(state_dir, "req-started-owner", "cycle-started-owner")
        self._enable_test_lock(monkeypatch)

        from nanobot.runtime import phase_checkpoint
        from tests.test_cycle_phase_machine import _cycle_repo
        repo, base, head = _cycle_repo(tmp_path)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        owner = bridge._begin_bridge_cycle_parent(state_dir)
        bridge._begin_bridge_checkpoint_phase(
            state_dir, owner, cycle_id="cycle-started-owner", branch="selfevo/cycle-cycle-opaque-001",
            base_sha=base, head_sha=head, phase="post_execution", repo_root=repo,
        )
        # The checkpoint is running, the journal lifecycle is started-empty,
        # and the reader must hold before proposer/planner/executor dispatch.
        owner_rows_before = intent_journal.read_owner_records(state_dir, owner)
        route_before = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
        assert route_before is not None and route_before.owner_id == owner and route_before.parent_started

        async def unexpected_planner(**kwargs):
            planning_calls.append(kwargs.get("cycle_id"))
            raise AssertionError("started owner must hold before planner dispatch")

        monkeypatch.setattr(bridge, "_run_planning_session", unexpected_planner)
        assert self._run_under_test_lock(monkeypatch) == 0
        route_after = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
        assert route_after is not None and route_after.owner_id == owner and route_after.parent_started
        assert intent_journal.provision_route_parent(state_dir, "bridge.cycle") == owner
        assert intent_journal.read_owner_records(state_dir, owner) == owner_rows_before
        assert proposer_calls == [] and planning_calls == [] and executor_spawns == []
        checkpoint = phase_checkpoint.load_checkpoint(state_dir, owner_id=owner)
        assert checkpoint is not None and checkpoint.status == "running"

    def test_persisted_rest_precheck_blocks_until_deadline_across_restart(self, tmp_path, monkeypatch):
        import sys
        from datetime import datetime, timedelta, timezone

        from nanobot.runtime import planner_rest

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._enable_intent_journal_for_windows(monkeypatch)
        _init_selfevo_repo(tmp_path)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "target_workspace")
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        self._enable_test_lock(monkeypatch)
        clock = {"now": datetime(2026, 11, 19, tzinfo=timezone.utc)}
        monkeypatch.setattr(planner_rest, "datetime", type("Clock", (), {
            "now": staticmethod(lambda _tz=None: clock["now"]),
            "fromisoformat": datetime.fromisoformat,
        }))
        calls = {"proposer": 0, "planner": 0, "executor": 0}
        monkeypatch.setattr(bridge.llm_proposer, "maybe_propose", lambda *_a, **_k: calls.__setitem__("proposer", calls["proposer"] + 1))
        monkeypatch.setattr(bridge, "SubagentManager", lambda **_kwargs: calls.__setitem__("executor", calls["executor"] + 1))

        def _no_plan_session(**_kwargs):
            async def _result():
                calls["planner"] += 1
                return {
                    "ran": True, "iterations_used": 1, "iterations_planned": None,
                    "tampered_files": [], "plan": None, "terminal_outcome": "no_plan",
                }
            return _result()

        monkeypatch.setattr(bridge, "_run_planning_session", lambda **kwargs: _no_plan_session(**kwargs))
        _seed_bridge_request(state_dir, "req-rest-deadline", "cycle-rest-deadline")
        # Persist the existing rest decision before bridge entry, exactly as a
        # prior normal planner cycle does.
        wake_file = tmp_path / "wake-input.txt"
        wake_file.write_text("unchanged", encoding="utf-8")
        deadline = (clock["now"] + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        writer = (
            "import sys; from nanobot.runtime import planner_rest; "
            "planner_rest.record_rest(sys.argv[1], 'cycle-prior-rest', "
            "planner_rest.WakeCondition(kind='file', ref=sys.argv[3]), "
            "sys.argv[4], sys.argv[2])"
        )
        subprocess.run(
            [sys.executable, "-c", writer, str(state_dir), str(state_dir.parent / "eeebot-self-evolving"), str(wake_file), deadline],
            cwd=Path(__file__).resolve().parents[1], check=True,
        )

        def _fresh_process_precheck(now):
            script = (
                "import json, sys; from datetime import datetime, timezone; "
                "from nanobot.runtime import planner_rest; "
                "now = datetime.fromisoformat(sys.argv[3]); "
                "planner_rest.datetime = type('Clock', (), {"
                "'now': staticmethod(lambda _tz=None: now), "
                "'fromisoformat': datetime.fromisoformat}); "
                "print(json.dumps(planner_rest.precheck(sys.argv[1], sys.argv[2])))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(state_dir), str(state_dir.parent / "eeebot-self-evolving"), now.isoformat()],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True,
            )
            return tuple(json.loads(result.stdout.strip()))

        # Existing bridge entry honors persisted state before proposer/planner.
        asyncio.run(bridge.main())
        assert calls == {"proposer": 0, "planner": 0, "executor": 0}
        held_owner = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
        assert held_owner is not None and held_owner.route_status == "closed"
        assert held_owner.no_execution_receipt is not None
        assert held_owner.no_execution_receipt[1] == "rest_unchanged"

        # A fresh interpreter sees the persisted state and remains held before
        # deadline; the bridge does not dispatch proposer, planner, or executor.
        clock["now"] += timedelta(minutes=4)
        assert _fresh_process_precheck(clock["now"]) == (False, "unchanged")
        asyncio.run(bridge.main())
        assert calls == {"proposer": 0, "planner": 0, "executor": 0}
        held_again = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
        assert held_again is not None and held_again.route_status == "closed"

        # At the persisted deadline the same existing precheck permits normal
        # cycle flow through proposer/planner; no timer or new scheduler is introduced.
        clock["now"] += timedelta(minutes=1)
        assert _fresh_process_precheck(clock["now"]) == (True, "deadline_passed")
        monkeypatch.setattr(bridge, "_prepare_repository_for_cycle", lambda *_args: {"restored": True})
        asyncio.run(bridge.main())
        assert calls == {"proposer": 1, "planner": 1, "executor": 0}
        after_deadline = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
        assert after_deadline is not None and after_deadline.route_status == "closed"
        assert after_deadline.no_execution_receipt is not None
        assert after_deadline.no_execution_receipt[1] == "no_plan"

    def test_explicit_no_plan_receipt_closes_without_executor_or_phase(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        self._enable_intent_journal_for_windows(monkeypatch)
        _init_selfevo_repo(base)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        monkeypatch.setattr(bridge.llm_proposer, "maybe_propose", lambda *_a, **_k: None)
        self._enable_test_lock(monkeypatch)
        _seed_bridge_request(state_dir, "req-no-plan", "cycle-no-plan")
        owners = []
        original_begin = bridge._begin_bridge_cycle_parent

        def _capture_owner(state_root):
            owner_id = original_begin(state_root)
            owners.append(owner_id)
            return owner_id

        planning_calls = []

        async def _no_plan(**kwargs):
            planning_calls.append(kwargs.get("cycle_id"))
            return {
                "ran": True, "iterations_used": 1, "iterations_planned": None,
                "tampered_files": [], "plan": None, "terminal_outcome": "no_plan",
            }

        monkeypatch.setattr(bridge, "_begin_bridge_cycle_parent", _capture_owner)
        monkeypatch.setattr(bridge, "_run_planning_session", _no_plan)
        assert self._run_under_test_lock(monkeypatch) == 0
        assert len(owners) == 1
        rows = intent_journal.read_owner_records(state_dir, owners[0])
        assert [row["event"] for row in rows] == ["no_execution_consumed"]
        assert rows[0]["cycle_id"].startswith("cycle-")
        assert rows[0]["outcome"] == "no_plan"
        # The no-plan receipt is terminal and cannot be amended as execution.
        with pytest.raises(intent_journal.JournalUnavailableError):
            intent_journal.consume_bridge_cycle_phase(
                state_dir, owners[0], cycle_id=rows[0]["cycle_id"],
                phase="post_execution", checkpoint_head_sha="a" * 40,
                checkpoint_status="complete",
            )

        assert self._run_under_test_lock(monkeypatch) == 0
        assert len(planning_calls) == 2 and planning_calls[1] != planning_calls[0]
        assert len(owners) == 2 and owners[1] != owners[0]
        next_rows = intent_journal.read_owner_records(state_dir, owners[1])
        assert len(next_rows) == 1 and next_rows[0]["event"] == "no_execution_consumed"

    def test_planner_exception_is_not_no_execution_and_restart_holds_before_planner(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        self._enable_intent_journal_for_windows(monkeypatch)
        _init_selfevo_repo(tmp_path)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "target_workspace")
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        monkeypatch.setattr(bridge.llm_proposer, "maybe_propose", lambda *_a, **_k: None)
        _seed_bridge_request(state_dir, "req-planner-error", "cycle-planner-error")
        owners = []
        planning_calls = []
        original_begin = bridge._begin_bridge_cycle_parent

        def _capture_owner(state_root):
            owner_id = original_begin(state_root)
            owners.append(owner_id)
            return owner_id

        async def _planner_error(**kwargs):
            planning_calls.append(kwargs.get("cycle_id"))
            raise RuntimeError("planner failed before terminal result")

        monkeypatch.setattr(bridge, "_begin_bridge_cycle_parent", _capture_owner)
        monkeypatch.setattr(bridge, "_run_planning_session", _planner_error)
        assert self._run_under_test_lock(monkeypatch) == 0
        assert len(owners) == 1
        assert intent_journal.read_owner_records(state_dir, owners[0]) == []

        async def _planner_unexpected(**kwargs):
            planning_calls.append(kwargs.get("cycle_id"))
            raise AssertionError("started-empty owner must hold before planner")

        monkeypatch.setattr(bridge, "_run_planning_session", _planner_unexpected)
        assert self._run_under_test_lock(monkeypatch) == 0
        assert len(planning_calls) == 1
        assert owners == [owners[0]]
        assert intent_journal.read_owner_records(state_dir, owners[0]) == []

    def test_full_green_cycle_writes_started_gate_success(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        self._enable_intent_journal_for_windows(monkeypatch)
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        # No real LLM call happens (spawn() is faked above) — avoid the
        # "no API key configured" hard-exit in _make_provider by stubbing it.
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        _stub_planning_session(monkeypatch)

        _seed_bridge_request(state_dir, "req-green", "cycle-green")
        bridge_owners = []
        original_begin = bridge._begin_bridge_cycle_parent

        def _capture_owner(state_root):
            owner_id = original_begin(state_root)
            bridge_owners.append(owner_id)
            return owner_id

        monkeypatch.setattr(bridge, "_begin_bridge_cycle_parent", _capture_owner)

        result = self._run_under_test_lock(monkeypatch)
        assert result == 0

        rows = _read_ledger(state_dir)
        phases = [r["phase"] for r in rows]
        assert phases[0] == "started"
        assert "dedup" in phases
        assert "gate" in phases
        assert phases[-2] == "outcome"
        assert phases[-1] == "cycle_cost"

        gate_rows = [r for r in rows if r["phase"] == "gate"]
        assert gate_rows[-1]["allowed"] is True

        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert outcome_rows[-1]["outcome"] == "success"
        assert outcome_rows[-1]["change_tier"] == "code-bearing"
        assert "scripts/feature.py" in outcome_rows[-1]["files_changed"]

        phase_rows = intent_journal.read_owner_records(state_dir, bridge_owners[0])
        assert [row["phase"] for row in phase_rows if row["event"] == "phase_consumed"] == [
            "post_execution", "smoke_gate", "integration",
        ]
        from nanobot.runtime import phase_checkpoint

        checkpoint = phase_checkpoint.load_checkpoint(
            state_dir, owner_id=bridge_owners[0],
        )
        assert checkpoint is not None and checkpoint.phase == "integration"
        assert checkpoint.status == "complete"
        assert len(bridge_owners) == 1

    @pytest.mark.parametrize("transport_outcome", ["success", "lost"])
    def test_executor_provider_attempt_uses_bridge_cycle_owner(
        self, tmp_path, monkeypatch, transport_outcome,
    ):
        from nanobot.agent.subagent import SubagentManager as RealManager
        from nanobot.config.schema import Config
        from nanobot.providers.litellm_provider import LiteLLMProvider

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        if transport_outcome == "lost":
            self._enable_intent_journal_for_windows(monkeypatch)
            script = (
                "import runpy, sys; from pathlib import Path; "
                "runpy.run_path('tests/test_cycle_ledger.py')['_executor_provider_restart_worker']("
                "Path(sys.argv[1]), sys.argv[2])"
            )
            dispatch = subprocess.run(
                [sys.executable, "-c", script, str(state_dir), "dispatch"],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            )
            assert dispatch.returncode == 0, dispatch.stdout + dispatch.stderr
            dispatch_output = dispatch.stdout + dispatch.stderr
            assert "previous HTTP attempt has unknown settlement" in dispatch_output
            owner = (state_dir / "executor-owner-id.txt").read_text(encoding="utf-8")
            journal_path = state_dir / "intent-journal" / f"{owner}.jsonl"
            parent_path = state_dir / "intent-journal" / f"{owner}.parent.json"
            dispatched_rows = intent_journal.read_owner_records(state_dir, owner)
            provider_rows = [row for row in dispatched_rows if row.get("target") == "provider.chat"]
            assert len(provider_rows) == 1
            assert provider_rows[0]["event"] == "intent" and provider_rows[0]["component"] == "subagent"
            dispatched_parent = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
            assert dispatched_parent is not None and dispatched_parent.owner_id == owner
            assert dispatched_parent.parent_started and dispatched_parent.has_unresolved_attempts
            assert dispatched_parent.active_operation_id == provider_rows[0]["operation_id"]
            journal_before_restart, parent_before_restart = journal_path.read_bytes(), parent_path.read_bytes()
            restart = subprocess.run(
                [sys.executable, "-c", script, str(state_dir), "restart"],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            )
            assert restart.returncode == 0, restart.stdout + restart.stderr
            assert "bridge: cycle owner held (JournalUnavailableError); aborting before proposer" in restart.stdout
            assert (state_dir / "restart-entry.txt").read_text(encoding="utf-8") == "entered"
            assert not (state_dir / "restart-proposer.txt").exists()
            assert not (state_dir / "restart-planner.txt").exists()
            assert not (state_dir / "restart-provider.txt").exists()
            assert (state_dir / "mock-transport-sends.txt").read_text(encoding="utf-8").splitlines() == ["send"]
            assert journal_path.read_bytes() == journal_before_restart
            assert parent_path.read_bytes() == parent_before_restart
            assert intent_journal.read_route_parent_state(state_dir, "bridge.cycle").owner_id == owner
            held = [row for row in _read_ledger(state_dir) if row.get("phase") == "bridge_cycle_intent_held"]
            assert held and held[-1]["reason"] == "JournalUnavailableError"
            return

        self._enable_intent_journal_for_windows(monkeypatch)
        self._enable_test_lock(monkeypatch)
        _init_selfevo_repo(tmp_path)
        config = Config()
        config.agents.defaults.model = "gpt-4o-mini"
        monkeypatch.setattr(bridge, "load_config", lambda *_args: config)
        monkeypatch.setattr(
            bridge, "_make_provider",
            lambda _config: LiteLLMProvider(
                api_key="fake", api_base="https://mock.invalid/v1", default_model="gpt-4o-mini",
            ),
        )
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "target_workspace")
        _stub_planning_session(monkeypatch)
        monkeypatch.setattr(bridge, "SubagentManager", RealManager)
        _seed_bridge_request(state_dir, "req-intent", "cycle-intent")
        owners = []
        original_begin = bridge._begin_bridge_cycle_parent

        def capture_owner(state_root):
            owner = original_begin(state_root)
            owners.append(owner)
            return owner

        monkeypatch.setattr(bridge, "_begin_bridge_cycle_parent", capture_owner)
        import litellm
        import openai
        from openai import _base_client
        sdk_httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx
        sent = []

        def transport(request):
            assert len(owners) == 1
            records = intent_journal.read_owner_records(state_dir, owners[0])
            pending = [row for row in records if row["event"] == "intent"]
            assert pending and pending[-1]["target"] == "provider.chat"
            assert records[-1]["event"] == "intent"
            assert pending[-1]["component"] == "subagent"
            parent_path = state_dir / "intent-journal" / f"{owners[0]}.parent.json"
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            assert parent["active_operation_id"] == pending[-1]["operation_id"]
            sent.append(request.content)
            if transport_outcome == "lost":
                raise sdk_httpx.ConnectError("simulated lost response", request=request)
            body = {
                "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            return sdk_httpx.Response(200, json=body, request=request)

        default_client = openai.DefaultAsyncHttpxClient

        class MockClient(default_client):
            def __init__(self, **kwargs):
                kwargs.pop("transport", None)
                super().__init__(transport=sdk_httpx.MockTransport(transport), **kwargs)

        monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", MockClient)
        monkeypatch.setattr(litellm, "num_retries", None)
        monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", False)
        assert self._run_under_test_lock(monkeypatch) == 0
        rows = intent_journal.read_owner_records(state_dir, owners[0])
        if transport_outcome == "success":
            assert [row["event"] for row in rows if row.get("target") == "provider.chat"] == [
                "intent", "terminal", "consumed",
            ]
            assert len(sent) == 1
            return

        assert [row["event"] for row in rows if row.get("target") == "provider.chat"] == ["intent"]
        assert len(sent) == 1
        parent_path = state_dir / "intent-journal" / f"{owners[0]}.parent.json"
        parent_before_restart = parent_path.read_bytes()
        journal_path = state_dir / "intent-journal" / f"{owners[0]}.jsonl"
        journal_before_restart = journal_path.read_bytes()
        assert self._run_under_test_lock(monkeypatch) == 0
        assert len(sent) == 1
        assert parent_path.read_bytes() == parent_before_restart
        assert journal_path.read_bytes() == journal_before_restart
        assert intent_journal.read_route_parent_state(state_dir, "bridge.cycle").owner_id == owners[0]

    def test_exact_tag_skip_writes_started_then_skipped(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "target_workspace")
        monkeypatch.setattr(bridge, "_cycle_tag_exists", lambda *_a, **_k: True)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        _stub_planning_session(monkeypatch)

        _seed_bridge_request(state_dir, "req-tagged", "cycle-tagged")
        self._enable_intent_journal_for_windows(monkeypatch)
        assert self._run_under_test_lock(monkeypatch) == 0

        rows = _read_ledger(state_dir)
        # ADR-035 rest amendment (#1964): a dedup match now also feeds
        # planner_dedup_evidence.record_rejected_duplicate, so the next
        # planning session sees why -- an extra trailing ledger row.
        assert [row["phase"] for row in rows] == [
            "started", "dedup", "outcome", "rejected_duplicate", "cycle_cost",
        ]
        assert rows[1]["decision"] == "skipped_duplicate"
        assert rows[2]["outcome"] == "skipped-duplicate"
        assert rows[2]["reason"] == "already_done_tag"

    # @lat: [[architecture#Intent-before-effect evidence]]
    def test_real_duplicate_closes_empty_owner_before_next_legitimate_invocation(
        self, tmp_path, monkeypatch,
    ):
        """A real success tag suppresses spawn, but must not strand the next cycle.

        The separate lost-provider-attempt case above asserts an unknown effect
        remains held with its journal bytes unchanged; this case exercises the
        no-effect duplicate lifecycle across two complete bridge invocations.
        """
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        self._enable_intent_journal_for_windows(monkeypatch)
        _origin, work = _init_selfevo_repo(base)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
        _seed_bridge_request(state_dir, "req-duplicate-owner", "cycle-duplicate-owner")

        planning_cycles = []
        async def planning_session(**kwargs):
            cycle_id = kwargs["cycle_id"]
            planning_cycles.append(cycle_id)
            if len(planning_cycles) == 1:
                # The first valid planner result is a genuine exact-tag duplicate.
                _run(work, "tag", f"cycle-{cycle_id}-success", "HEAD")
            return {
                "ran": True, "iterations_used": 1, "iterations_planned": 1,
                "tampered_files": [],
                "plan": {"plan": "implement a novel bounded increment", "candidate_id": None},
            }

        monkeypatch.setattr(bridge, "_run_planning_session", planning_session)
        executor_spawns = []

        class _ObservedExecutor(_FakeSubagentManager):
            async def spawn(self, **kwargs):
                if self._telemetry_component != "planner":
                    executor_spawns.append(kwargs)
                return await super().spawn(**kwargs)

        monkeypatch.setattr(bridge, "SubagentManager", _ObservedExecutor)
        self._enable_test_lock(monkeypatch)

        assert asyncio.run(bridge.main()) == 0
        assert executor_spawns == []
        first_owner = intent_journal.read_route_parent_state(state_dir, "bridge.cycle")
        assert first_owner is not None and first_owner.parent_started
        assert first_owner.route_status == "closed"
        assert first_owner.no_execution_receipt == (planning_cycles[0], "duplicate_suppressed")
        assert [row["event"] for row in intent_journal.read_owner_records(
            state_dir, first_owner.owner_id,
        )] == ["no_execution_consumed"]
        duplicate_outcomes = [
            row for row in _read_ledger(state_dir)
            if row.get("phase") == "outcome" and row.get("reason") == "already_done_tag"
        ]
        assert len(duplicate_outcomes) == 1
        assert duplicate_outcomes[0]["outcome"] == "skipped-duplicate"

        # The following planner result is novel (its cycle has no success tag).
        # It must reach the executor rather than being blocked by the empty
        # started owner left by the duplicate path.
        assert asyncio.run(bridge.main()) == 0
        assert len(planning_cycles) == 2
        assert len(executor_spawns) == 1
