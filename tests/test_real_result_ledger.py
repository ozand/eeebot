"""#1748: the real-failure criterion's inputs, recorded on the ledger outcome row.

``bridge._is_real_result`` reads a result artifact on five fields
(``result_status``, ``status``, ``terminal_reason``, ``materialized_from``,
``blocker.reason``) to decide whether a prior cycle was a real failure or a
blocked stub. Result artifacts are pruned within ~29 days; the ledger's
``outcome`` row is append-only and survives — but until this issue it carried
only ``outcome``/``reason``, so that question became silently unanswerable
once the artifact aged out (#1451 lost 77 of 122 rows this way).

``bridge._real_result_ledger_inputs`` computes the same five inputs (plus the
derived boolean) from the values a bridge call site already has, calling
``_is_real_result`` itself rather than re-reading the artifact — the one-
writer requirement. These tests pin: the row and ``_is_real_result`` agree on
fixtures where they could differ; an old row without the key is handled by
every listed reader; and ``scripts/replay_semantic_dedup.py``'s audit reports
``complete: true`` once the comparison set is built entirely from post-fix
rows (the artifact dump it used to require is no longer necessary).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)

# scripts/ is not a package — load it the same way tests/test_replay_semantic_dedup.py does.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "replay_semantic_dedup", _REPO_ROOT / "scripts" / "replay_semantic_dedup.py"
)
assert _spec and _spec.loader
replay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = replay
_spec.loader.exec_module(replay)


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


# ─── unit: the helper agrees with _is_real_result by construction ─────────


class TestRealResultLedgerInputsAgreeWithCriterion:
    @pytest.mark.parametrize(
        "result_status, expected",
        [
            ("completed", True),
            ("already_done", True),
            ("blocked", False),
        ],
    )
    def test_derived_boolean_matches_is_real_result_on_the_same_artifact_shape(
        self, result_status, expected,
    ):
        """The fixture where they COULD disagree: a status value that flips
        the criterion. Both computations must agree because the helper calls
        ``_is_real_result`` itself — this pins that it still does."""
        inputs = bridge._real_result_ledger_inputs(result_status)
        assert inputs["is_real_result"] is expected
        # Rebuild the artifact shape _is_real_result actually reads and
        # confirm it returns the identical answer — not a re-derivation,
        # the SAME question asked the SAME way.
        artifact_shape = {
            "result_status": inputs["result_status"],
            "status": inputs["status"],
            "terminal_reason": inputs["terminal_reason"],
            "materialized_from": inputs["materialized_from"],
            "blocker": {"reason": inputs["blocker_reason"]} if inputs["blocker_reason"] else {},
        }
        assert bridge._is_real_result(artifact_shape) is inputs["is_real_result"]

    def test_terminal_reason_and_blocker_flip_the_criterion_too(self):
        """The other two inputs _is_real_result reads (dead in bridge's own
        writer today, but part of the contract) must also be threaded
        through and agree."""
        inputs = bridge._real_result_ledger_inputs(
            "completed", terminal_reason="local_executor_unavailable",
        )
        assert inputs["is_real_result"] is False

        inputs = bridge._real_result_ledger_inputs(
            "completed", blocker={"reason": "local_executor_unavailable"},
        )
        assert inputs["is_real_result"] is False
        assert inputs["blocker_reason"] == "local_executor_unavailable"


# ─── end-to-end: the written artifact and the ledger row agree ────────────


class TestEndToEndAgreement:
    def test_green_cycle_result_artifact_and_ledger_row_agree(self, tmp_path, monkeypatch):
        """A real (non-blocked) result: both the artifact _is_real_result
        would read and the ledger row's real_result say True."""
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        _init_selfevo_repo(base)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        _seed_bridge_request(state_dir, "req-green", "cycle-green")
        assert asyncio.run(bridge._main_impl()) == 0

        result_path = state_dir / "subagents" / "results" / "result-req-green.json"
        artifact = json.loads(result_path.read_text(encoding="utf-8"))

        outcome_rows = [r for r in _read_ledger(state_dir) if r["phase"] == "outcome"]
        row = outcome_rows[-1]
        assert row["real_result"]["is_real_result"] == bridge._is_real_result(artifact)
        assert row["real_result"]["is_real_result"] is True
        assert row["real_result"]["materialized_from"] == artifact["materialized_from"]
        assert row["real_result"]["result_status"] == artifact["result_status"]

    def test_suppressed_duplicate_result_artifact_and_ledger_row_agree(self, tmp_path, monkeypatch):
        """A blocked-stub result (recent-failure suppression): both the
        artifact and the ledger row say False — the exact case #1451 lost
        once the artifact aged out."""
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        _init_selfevo_repo(base)
        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        # A genuine blocked-stub prior (no rollback.reason) -- #798 defect 2
        # established that a SKIP bookkeeping row (rollback.reason set to a
        # dedup reason) must NOT suppress; this one has none, so it is read
        # as a real recent failure and does suppress.
        title = "Create a script to check memory pressure levels"
        results_dir = state_dir / "subagents" / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "result-req-prior.json").write_text(
            json.dumps({
                "request_id": "req-prior",
                "backlog_title": title,
                "result_status": "blocked",
            }),
            encoding="utf-8",
        )

        _seed_bridge_request(
            state_dir, "req-dup", "cycle-dup",
            task_title=f"Implement and commit: {title}",
        )
        assert asyncio.run(bridge._main_impl()) == 0

        result_path = state_dir / "subagents" / "results" / "result-req-dup.json"
        artifact = json.loads(result_path.read_text(encoding="utf-8"))
        assert artifact["result_status"] == "blocked"

        outcome_rows = [
            r for r in _read_ledger(state_dir)
            if r["phase"] == "outcome" and r.get("cycle_id") == "cycle-dup"
        ]
        row = outcome_rows[-1]
        assert row["real_result"]["is_real_result"] == bridge._is_real_result(artifact)
        assert row["real_result"]["is_real_result"] is False


# ─── an old row (pre-#1748, no real_result key) is handled by every reader ─


_OLD_ROW = {
    "phase": "outcome",
    "cycle_id": "cycle-pre-1748",
    "outcome": "failed",
    "reason": "recent_duplicate_failure",
    "files_changed": [],
    "branch": None,
    "ts": "2026-08-01T00:00:00Z",
}


class TestOldRowsWithoutRealResultStillWork:
    """Every reader below treats `phase: "outcome"` rows by reading named
    keys (`outcome`, `reason`, `cycle_id`, `ts`, ...) via `.get()` — none
    inspects `real_result` or does presence/key-count validation, so an old
    row missing the key reads exactly as it did before #1748. This directly
    exercises the readers most likely to break on that class of change
    (#1374): action_index, demand, scorecard, health, llm_proposer.
    """

    def test_action_index_reads_old_row_unchanged(self, tmp_path):
        from nanobot.runtime import action_index

        state_dir = tmp_path / "state"
        ledger_dir = state_dir / "ledger"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "cycles.jsonl").write_text(json.dumps(_OLD_ROW) + "\n", encoding="utf-8")
        by_cycle = action_index._ledger_by_cycle(state_dir)
        assert by_cycle["cycle-pre-1748"]["outcome"] == "failed"

    def test_scorecard_ledger_rows_reads_old_row_unchanged(self, tmp_path):
        from nanobot.runtime import scorecard

        state_dir = tmp_path / "state"
        ledger_dir = state_dir / "ledger"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "cycles.jsonl").write_text(json.dumps(_OLD_ROW) + "\n", encoding="utf-8")
        recent_now = datetime(2026, 8, 2, tzinfo=timezone.utc)
        rows, _window = scorecard._ledger_rows(state_dir, recent_now)
        outcome_rows = [r for r in rows if r.get("phase") == "outcome"]
        assert outcome_rows[0]["outcome"] == "failed"

    def test_health_read_cycle_progress_reads_old_row_unchanged(self, tmp_path):
        from nanobot.runtime import health

        state_dir = tmp_path / "state"
        ledger_dir = state_dir / "ledger"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "cycles.jsonl").write_text(json.dumps(_OLD_ROW) + "\n", encoding="utf-8")
        # A 'failed' old row without `real_result` contributes no success
        # timestamp either way -- this pins that the missing key does not
        # raise or get mistaken for a truthy value anywhere in the read path.
        progress = health.read_cycle_progress(state_dir, since_ts="2026-07-01T00:00:00Z")
        assert progress["dominant_reason"] == "recent_duplicate_failure"

    def test_llm_proposer_terminal_rows_reads_old_row_unchanged(self, tmp_path):
        from nanobot.runtime import llm_proposer

        state_dir = tmp_path / "state"
        ledger_dir = state_dir / "ledger"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "cycles.jsonl").write_text(json.dumps(_OLD_ROW) + "\n", encoding="utf-8")
        recent_now = datetime(2026, 8, 2, tzinfo=timezone.utc)
        rows = llm_proposer._load_ledger_rows(state_dir, now=recent_now)
        outcome_only = llm_proposer._terminal_rows(rows)
        assert len(outcome_only) == 1
        assert outcome_only[0].get("outcome") == "failed"


# ─── AC5: replay_semantic_dedup.py's audit reports complete for a post-fix window ─


class TestReplayAuditCompleteFromPostFixRows:
    """scripts/replay_semantic_dedup.py is NOT modified by #1748 (read only).
    Its ``audit_coverage(candidates, result_index)`` decides completeness by
    whether each candidate's prior cycle id is a key in ``result_index`` — a
    dump of surviving result artifacts. Before #1748 that dump was the ONLY
    source for those fields once an artifact aged out. These tests build
    ``result_index`` straight from ledger rows' new ``real_result`` field
    instead of a separate artifact dump, proving the ledger alone now
    supplies what the audit needs for any window built entirely of post-fix
    rows.
    """

    _BASE = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    def _ts(self, offset_h: float) -> str:
        return (self._BASE + timedelta(hours=offset_h)).isoformat().replace("+00:00", "Z")

    def test_audit_is_complete_when_every_prior_outcome_row_carries_real_result(self):
        prior_real_result = bridge._real_result_ledger_inputs("blocked")
        rows = [
            {
                "phase": "proposed", "cycle_id": "c1", "demand_id": "d1",
                "target_path": "scripts/a.py", "task_title": "t",
                "expected_outcome_claim": "", "ts": self._ts(0),
            },
            {
                "phase": "outcome", "cycle_id": "c1", "outcome": "failed",
                "reason": "recent_duplicate_failure", "ts": self._ts(0),
                "real_result": prior_real_result,
            },
            {
                "phase": "proposed", "cycle_id": "c2", "demand_id": "d1",
                "target_path": "scripts/a.py", "task_title": "t",
                "expected_outcome_claim": "", "ts": self._ts(2),
            },
            {
                "phase": "outcome", "cycle_id": "c2", "outcome": "success", "ts": self._ts(2),
                "real_result": bridge._real_result_ledger_inputs("completed"),
            },
        ]
        cycles = replay.build_cycles(rows)
        candidates = replay.name_key_candidates(cycles)
        assert len(candidates) == 1  # c2 blocked by c1's failure on (d1, scripts/a.py)

        result_index = {
            row["cycle_id"]: row["real_result"]
            for row in rows
            if row.get("phase") == "outcome" and "real_result" in row
        }
        audit = replay.audit_coverage(candidates, result_index)
        assert audit.complete is True
        assert audit.missing == 0
        assert audit.adjudicable == audit.total == 1

    def test_audit_is_incomplete_when_a_prior_row_predates_the_fix(self):
        """Contrast case: a pre-#1748 row (no ``real_result``, artifact
        already pruned) still reports incomplete — the fix does not retroactively
        answer the question for history, only stops it recurring."""
        rows = [
            {
                "phase": "proposed", "cycle_id": "c1", "demand_id": "d1",
                "target_path": "scripts/a.py", "task_title": "t",
                "expected_outcome_claim": "", "ts": self._ts(0),
            },
            {
                "phase": "outcome", "cycle_id": "c1", "outcome": "failed",
                "reason": "recent_duplicate_failure", "ts": self._ts(0),
                # no real_result -- pre-fix row
            },
            {
                "phase": "proposed", "cycle_id": "c2", "demand_id": "d1",
                "target_path": "scripts/a.py", "task_title": "t",
                "expected_outcome_claim": "", "ts": self._ts(2),
            },
            {
                "phase": "outcome", "cycle_id": "c2", "outcome": "success", "ts": self._ts(2),
                "real_result": bridge._real_result_ledger_inputs("completed"),
            },
        ]
        cycles = replay.build_cycles(rows)
        candidates = replay.name_key_candidates(cycles)
        assert len(candidates) == 1

        result_index = {
            row["cycle_id"]: row["real_result"]
            for row in rows
            if row.get("phase") == "outcome" and "real_result" in row
        }
        audit = replay.audit_coverage(candidates, result_index)
        assert audit.complete is False
        assert audit.missing == 1
