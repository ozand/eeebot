"""Tests for issue #1118: tri-state cycle verdict (accept/reject/inconclusive).

Covers ``nanobot.runtime.bridge._derive_cycle_verdict`` (pure, deterministic
mapping table) and ``_executor_reported_skipped`` (best-effort telemetry
read) as unit tests, plus one light bridge-integration test proving a real
green cycle's terminal ledger row carries ``verdict: accept`` and an
already-done dedup skip carries ``verdict: reject`` end to end.

``outcome`` itself is never touched by any of this — see
tests/test_cycle_ledger.py for the byte-identical-shape guarantee on an
omitted verdict.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)

# ─── unit: _derive_cycle_verdict — the full mapping matrix ─────────────────


class TestDeriveCycleVerdict:
    def test_success_is_accept(self):
        assert bridge._derive_cycle_verdict("success", None) == ("accept", None)

    def test_promotion_candidate_is_accept(self):
        assert bridge._derive_cycle_verdict("promotion_candidate", None) == ("accept", None)

    @pytest.mark.parametrize(
        "reason",
        ["already_done_tag", "already_done", "recent_duplicate_failure", "existence_index_duplicate"],
    )
    def test_skipped_duplicate_reasons_are_reject(self, reason):
        assert bridge._derive_cycle_verdict("skipped-duplicate", reason) == ("reject", reason)

    def test_executor_reported_skipped_reason_is_reject(self):
        """The issue's headline case: a zero-commit cycle whose executor
        honestly reported outcome: skipped in its structured final answer
        (i.e. verified already-done) must land on 'reject', not
        'inconclusive' — a HEALTHY negative result, distinct from an
        operational failure."""
        assert bridge._derive_cycle_verdict("partial", "executor_reported_skipped") == (
            "reject", "executor_reported_skipped",
        )
        assert bridge._derive_cycle_verdict("failed", "executor_reported_skipped") == (
            "reject", "executor_reported_skipped",
        )

    @pytest.mark.parametrize(
        "reason",
        [
            "gate_failed",
            "mutation_surface_violation",
            "blocked_file_present",
            "out_of_band_main_detected",
            "switch_base_gate_error",
            "switch_base_gate_blocked",
            "head_on_main_precondition_failed",
            "no_commit",
            "internal_error",
        ],
    )
    def test_infra_and_gate_reasons_are_inconclusive(self, reason):
        """Acceptance criterion: gate/infra failures (spawn timeout, checkout
        failure, smoke-infra error) must record 'inconclusive', never
        'reject' — they are not a verified negative result."""
        verdict, verdict_reason = bridge._derive_cycle_verdict("failed", reason)
        assert verdict == "inconclusive"
        assert verdict_reason == reason

    def test_bare_partial_with_no_reason_is_inconclusive(self):
        """'explored but failed to implement' (no dedup match, no executor
        self-report, no rollback reason) is genuinely ambiguous — must be
        'inconclusive', never a false 'accept'/'reject'."""
        assert bridge._derive_cycle_verdict("partial", None) == ("inconclusive", None)

    def test_bare_failed_with_no_reason_is_inconclusive(self):
        assert bridge._derive_cycle_verdict("failed", None) == ("inconclusive", None)

    def test_unrecognized_outcome_is_inconclusive_fail_closed(self):
        assert bridge._derive_cycle_verdict("not-a-real-outcome", None) == ("inconclusive", None)

    def test_unrecognized_reason_with_known_outcome_falls_through_to_outcome_mapping(self):
        """An outcome the table maps directly (success/promotion_candidate)
        is not derailed by a stray/unknown reason string."""
        assert bridge._derive_cycle_verdict("success", "some_future_reason_code") == ("accept", None)

    def test_empty_string_reason_behaves_like_none(self):
        assert bridge._derive_cycle_verdict("partial", "") == ("inconclusive", None)


# ─── unit: _executor_reported_skipped — telemetry read, fail-open ─────────


class TestExecutorReportedSkipped:
    def test_none_task_id_returns_false(self, tmp_path):
        assert bridge._executor_reported_skipped(tmp_path, None) is False

    def test_missing_telemetry_file_returns_false(self, tmp_path):
        assert bridge._executor_reported_skipped(tmp_path, "nonexistent-task-id") is False

    def test_true_when_final_answer_declares_skipped(self, tmp_path):
        subagents_dir = tmp_path / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "abc123.json").write_text(
            json.dumps({
                "result": json.dumps({
                    "action_taken": "verified the feature already exists",
                    "files_changed": [],
                    "outcome": "skipped",
                    "concrete_next_action": "n/a",
                    "findings": ["already implemented in mod.py"],
                }),
            }),
            encoding="utf-8",
        )
        assert bridge._executor_reported_skipped(tmp_path, "abc123") is True

    def test_false_when_final_answer_declares_completed(self, tmp_path):
        subagents_dir = tmp_path / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "abc123.json").write_text(
            json.dumps({"result": json.dumps({"outcome": "completed"})}),
            encoding="utf-8",
        )
        assert bridge._executor_reported_skipped(tmp_path, "abc123") is False

    def test_false_when_result_is_not_json(self, tmp_path):
        subagents_dir = tmp_path / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "abc123.json").write_text(
            json.dumps({"result": "I looked around and didn't do anything structured."}),
            encoding="utf-8",
        )
        assert bridge._executor_reported_skipped(tmp_path, "abc123") is False

    def test_falls_back_to_summary_when_result_absent(self, tmp_path):
        subagents_dir = tmp_path / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "abc123.json").write_text(
            json.dumps({"summary": json.dumps({"outcome": "skipped"})}),
            encoding="utf-8",
        )
        assert bridge._executor_reported_skipped(tmp_path, "abc123") is True

    def test_malformed_telemetry_file_fails_open(self, tmp_path):
        subagents_dir = tmp_path / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "abc123.json").write_text("not even json", encoding="utf-8")
        assert bridge._executor_reported_skipped(tmp_path, "abc123") is False


# ─── light bridge integration: verdict lands on the real terminal row ─────


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


class TestBridgeIntegrationVerdict:
    def test_full_green_cycle_records_verdict_accept(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()
        _init_selfevo_repo(base)

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())

        _seed_bridge_request(state_dir, "req-green-verdict", "cycle-green-verdict")

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert outcome_rows[-1]["outcome"] == "success"  # untouched, byte-identical
        assert outcome_rows[-1]["verdict"] == "accept"
        assert "verdict_reason" not in outcome_rows[-1]

    def test_already_done_skip_records_verdict_reject(self, tmp_path, monkeypatch):
        base = tmp_path
        state_dir = base / "state"
        state_dir.mkdir()

        monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
        monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
        monkeypatch.setattr(bridge, "_task_already_done", lambda *_a, **_k: True)

        _seed_bridge_request(
            state_dir, "req-dup-verdict", "cycle-dup-verdict",
            task_title="implement thing xyz already done",
        )

        result = asyncio.run(bridge._main_impl())
        assert result == 0

        rows = _read_ledger(state_dir)
        outcome_rows = [r for r in rows if r["phase"] == "outcome"]
        assert outcome_rows[-1]["outcome"] == "skipped-duplicate"  # untouched, byte-identical
        assert outcome_rows[-1]["verdict"] == "reject"
        assert outcome_rows[-1]["verdict_reason"] == "already_done"
