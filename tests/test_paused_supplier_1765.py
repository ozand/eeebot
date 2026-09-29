"""#1765: a gateway/supplier outage records `outcome: "paused-supplier"`,
distinct from `outcome: "failed"` — the classifier's own defect stays
`failed` and keeps failing loudly.

Covers: the classifier itself (positive-match allowlist, conservative
default); the ledger's new outcome value and audit field
(`llm_error_classification`); `_decide_handled_marker`'s unconditional
bypass for a supplier pause (no marker, no retry counter, ever); the
end-to-end proof that the SAME queued request survives multiple outage
cycles unchanged (the AC's own required test); `_recent_failure_match`'s
exclusion (demand cooling never sees this reason); goal-gap futility's
exclusion; and the scorecard's own counters.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import bridge, cycle_ledger, goal_gap_futility as futility, scorecard
from tests.test_bridge_executor_llm_error import (
    TRANSPORT_ERROR,
    _LLMDeadSubagentManager,
    _stub_planning_session,
)
from tests.test_cycle_ledger import _init_selfevo_repo, _seed_bridge_request


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    # ADR-034 rule 3: should_propose/build_context/bridge.py's executor
    # gate all now hard-require a real release charter to proceed.
    _adr034_release_root = tmp_path / "_adr034_release_root"
    _adr034_release_root.mkdir(exist_ok=True)
    (_adr034_release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", _adr034_release_root)


def _wire(tmp_path, monkeypatch, manager_cls):
    base = tmp_path
    state_dir = base / "state"
    state_dir.mkdir(exist_ok=True)
    _init_selfevo_repo(base)
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", base / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", manager_cls)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    return state_dir


# ─── the classifier: allowlist, not a catch-all ────────────────────────────


class TestClassifyLlmError:
    @pytest.mark.parametrize("text", [
        "Error code: 429 - rate limited",
        "HTTP 503 Service Unavailable",
        "received status code 500 from upstream",
    ])
    def test_http_contextual_supplier_status_remains_paused(self, text):
        assert bridge._classify_llm_error(text) == "paused-supplier"

    @pytest.mark.parametrize("text", [
        "Error: LLM execution failed: Error calling LLM: litellm.InternalServerError: "
        "OpenAIException - Connection error.",
        "litellm.APIConnectionError: connection refused",
        "connection reset by peer",
        "Error code: 429 - {'error': 'rate limited'}",
        "Error code: 503 - Service Unavailable",
        "HTTP 502 Bad Gateway",
        "litellm.RateLimitError: Error code: 429",
        "litellm.ServiceUnavailableError: Error code: 502",
        "No deployments available for selected model",
        "No healthy deployment available",
        "litellm.NotFoundError: Error code: 404 - model route not found",
        # #1765 review: real 7-day host corpus, verbatim (7 distinct strings,
        # 61 occurrences, all supplier-side). The RateLimitError text names
        # the local gateway's own retry-after, not an HTTP code alone.
        "litellm.RateLimitError: RateLimitError: OpenAIException - No deployments "
        "available for selected model, Try again in 30 seconds",
        # THE hard case: a bare "Error code: 400" would file as OURS under a
        # status-code-keyed classifier -- this one is a supplier fault (the
        # local gateway's model daemon has nothing loaded), caught on the
        # message text, never on the 400.
        "litellm.BadRequestError: OpenAIException - Error code: 400 - "
        "{'detail': 'No model loaded. Call POST /inference/load first)'}",
    ])
    def test_supplier_side_signals_classify_as_paused_supplier(self, text):
        assert bridge._classify_llm_error(text) == "paused-supplier"

    @pytest.mark.parametrize("text", [
        "maximum output is 500 tokens",
        "requested 500 tokens but the model allows 400",
        "limit is 429 tokens",
    ])
    def test_bare_numeric_data_does_not_claim_supplier_outage(self, text):
        assert bridge._classify_llm_error(text) == "failed"

    def test_bare_400_status_code_alone_is_not_a_supplier_signal(self):
        """The classifier keys on the MESSAGE, not the HTTP status -- a 400
        with no supplier-shaped phrasing must still default to 'failed'."""
        assert bridge._classify_llm_error(
            "litellm.BadRequestError: Error code: 400 - invalid request: "
            "unsupported parameter 'foo'"
        ) == "failed"

    @pytest.mark.parametrize("text", [
        "",
        "litellm.BadRequestError: This model's maximum context length is 8192 tokens.",
        "litellm.BadRequestError: Invalid tool call arguments: expected object, got string",
        "litellm.BadRequestError: Invalid value for temperature: must be between 0 and 2",
        "ValueError: malformed JSON in tool arguments",
        "our own schema validation failed: missing required field 'name'",
    ])
    def test_our_own_defect_signals_stay_failed(self, text):
        assert bridge._classify_llm_error(text) == "failed"

    @pytest.mark.parametrize("text, expected", [
        ("HTTP 502 Bad Gateway", "paused-supplier"),
        ("HTTP 503 Service Unavailable", "paused-supplier"),
        ("http 429", "paused-supplier"),
        ("HTTP 404 Not Found", "failed"),       # a client status stays ours
        ("maximum output is 500 tokens", "failed"),  # a bare number is not a status line
    ])
    def test_rx_http_status_line_alternative(self, text, expected):
        """#1919: pins the one alternative added to main's list -- "http",
        whitespace, then 429/500/502/503/504. Main's verbatim regex does not
        match an HTTP status line."""
        assert bridge._classify_llm_error(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("received status code 500 from upstream", "paused-supplier"),
        ("gateway: status code 503 returned by upstream", "paused-supplier"),
        ("status code 500", "failed"),              # no upstream relay named
        ("status code 404 from upstream", "failed"),  # a client status stays ours
    ])
    def test_rx_upstream_status_code_alternative(self, text, expected):
        """#1919: pins the second alternative added to main's list -- "status
        code" + 429/500/502/503/504 followed within 30 chars by "upstream"."""
        assert bridge._classify_llm_error(text) == expected

    @pytest.mark.parametrize("text", [
        # the local gateway's own text during the 2026-09-29 incident, verbatim
        "No deployments available for selected model, Try again in 8355 seconds",
        "Error code: 503 - Service Unavailable",
        "Connection error.",
    ])
    def test_supplier_outage_text_wins_over_generic_call_site_evidence(self, text):
        """#1919 review P1: the executor records error_type="provider_error"
        whenever a response carries none (subagent.py). An affirmative
        outage match must still classify as paused-supplier, never fall
        through to the structured branch's model_call_incomplete fallback --
        that would spend the incomplete-retry budget on the supplier's
        uptime, which #1765 exists to prevent."""
        failure = {"stage": "model_call", "call_stage": "model_call", "error_type": "provider_error", "message": text}
        assert bridge._classify_llm_error(text, model_call_failure=failure) == "paused-supplier"

    @pytest.mark.parametrize("text", ["socket timeout", "Request timed out after 60s", "litellm.Timeout: Read timeout"])
    def test_model_call_timeout_needs_call_site_evidence(self, text):
        assert bridge._classify_llm_error(text) == "failed"
        assert bridge._classify_llm_error(text, model_call_failure={"stage": "model_call"}) == "model_call_incomplete"
        assert bridge._classify_llm_error(text, model_call_failure={"stage": "tool_execution"}) == "failed"
        assert bridge._classify_llm_error(text, model_call_failure={"stage": "response_handling"}) == "failed"
        assert bridge._classify_llm_error(text, model_call_failure={"stage": "response_handling", "call_stage": "model_call"}) == "model_call_incomplete"

    @pytest.mark.parametrize("error_type, message", [
        ("BadRequestError", "Azure OpenAI API Error 400: invalid temperature"),
        ("BadRequestError", "Azure OpenAI API Error 404: deployment not found"),
        ("BadRequestError", "invalid temperature"),
        ("AuthenticationError", "Incorrect API key provided"),
        ("PermissionDeniedError", "model access denied"),
        ("provider_error", "Azure OpenAI API Error 401: Incorrect API key"),
        ("provider_error", "Azure OpenAI API Error 404: Deployment not found"),
        ("provider_error", "Error calling Codex: Invalid API key"),
    ])
    def test_definitive_client_rejection_with_model_call_telemetry_stays_failed(self, error_type, message):
        failure = {
            "stage": "response_handling",
            "call_stage": "model_call",
            "error_type": error_type,
            "message": message,
        }
        assert bridge._classify_llm_error(
            f"litellm.{error_type}: {message}",
            model_call_failure=failure,
        ) == "failed"

    @pytest.mark.parametrize("error_type", ["RateLimitError", "InternalServerError", "ServiceUnavailableError"])
    def test_affirmative_provider_error_types_are_supplier_pauses(self, error_type):
        failure = {"stage": "response_handling", "call_stage": "model_call", "error_type": error_type, "message": "gateway response"}
        assert bridge._classify_llm_error("gateway response", model_call_failure=failure) == "paused-supplier"

    def test_default_is_conservative_not_a_catch_all(self):
        """An error text mentioning neither class's vocabulary at all stays
        'failed' -- the default direction the issue specifies, and the proof
        this is an allowlist rather than "anything unfamiliar is an outage"."""
        assert bridge._classify_llm_error("something entirely unrecognized happened") == "failed"


# ─── the ledger: new outcome value, audit field ────────────────────────────


class TestLedgerOutcomeAndAuditField:
    def test_paused_supplier_is_a_valid_outcome_not_coerced(self, tmp_path):
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "paused-supplier", "llm_supplier_paused", [], None)
        rows = _read_ledger(tmp_path)
        assert rows[0]["outcome"] == "paused-supplier"

    def test_llm_error_classification_field_carries_class_and_raw_text(self, tmp_path):
        cycle_ledger.record_cycle_outcome(
            tmp_path, "c1", "paused-supplier", "llm_supplier_paused", [], None,
            llm_error_classification={"class": "paused-supplier", "raw_error": "Connection error"},
        )
        row = _read_ledger(tmp_path)[0]
        assert row["llm_error_classification"] == {"class": "paused-supplier", "raw_error": "Connection error"}

    def test_llm_error_classification_omitted_when_not_given(self, tmp_path):
        cycle_ledger.record_cycle_outcome(tmp_path, "c1", "success", None, ["a.py"], "selfevo/cycle-1")
        assert "llm_error_classification" not in _read_ledger(tmp_path)[0]


def _read_ledger(state_dir: Path) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ─── _decide_handled_marker: unconditional bypass ──────────────────────────


class TestDecideHandledMarkerSupplierPaused:
    def test_model_call_incomplete_has_separate_bounded_retry_stop(self, tmp_path):
        marker = tmp_path / "handled_req.txt"
        retry = tmp_path / "retry_incomplete_req.json"
        for attempt in range(1, bridge.MODEL_CALL_INCOMPLETE_MAX_RETRIES + 1):
            result = bridge._decide_handled_marker(
                marker, "req.json", llm_error=True, model_call_incomplete=True,
            )
            if attempt < bridge.MODEL_CALL_INCOMPLETE_MAX_RETRIES:
                assert result == "incomplete_retry"
                assert not marker.exists()
            else:
                assert result == "incomplete_retries_paused"
                assert marker.exists()
        state = json.loads(retry.read_text(encoding="utf-8"))
        assert state["count"] == bridge.MODEL_CALL_INCOMPLETE_MAX_RETRIES
        assert state["operator_check"] == "check request size / budget"
        assert not (tmp_path / "retry_req.json").exists()

    def test_error_card_retry_attempt_reads_incomplete_counter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", tmp_path)
        (tmp_path / "retry_incomplete_req.json").write_text(
            json.dumps({"count": 3, "max": 3}), encoding="utf-8",
        )
        request_id = "req"
        retry_path = bridge._model_call_incomplete_retry_path(request_id)
        attempt = None
        if retry_path is not None and retry_path.exists():
            retry_data = json.loads(retry_path.read_text(encoding="utf-8"))
            retry_count = int(retry_data.get("count") or 0)
            if retry_count:
                attempt = f"{retry_count}/{retry_data.get('max') or bridge.LLM_ERROR_MAX_RETRIES}"
        assert attempt == "3/3"

    def test_supplier_paused_writes_no_marker_and_no_retry_counter(self, tmp_path):
        marker = tmp_path / "handled_req.txt"
        result = bridge._decide_handled_marker(
            marker, "req.json", llm_error=True, supplier_paused=True,
        )
        assert result == "supplier_paused"
        assert not marker.exists()
        assert not (tmp_path / "retry_req.json").exists()

    def test_repeated_supplier_paused_calls_never_accumulate_state(self, tmp_path):
        """Unlike llm_error's bounded retry, calling this 10 times in a row
        leaves the filesystem exactly as untouched as calling it once."""
        marker = tmp_path / "handled_req.txt"
        for _ in range(10):
            assert bridge._decide_handled_marker(
                marker, "req.json", llm_error=True, supplier_paused=True,
            ) == "supplier_paused"
        assert not marker.exists()
        assert not (tmp_path / "retry_req.json").exists()


# ─── end-to-end: the same request survives an outage, unchanged ───────────


class TestOutageCycleOffersTheSameItemAgainUnchanged:
    def test_paused_supplier_cycle_records_outcome_and_never_retires(self, tmp_path, monkeypatch):
        """Architect resolution 2026-09-25 (ADR-035 rule 3 / ADR-031 rule 2):
        an interrupted-by-supply plan is not retried inline and not
        requeued -- it becomes the pending open increment, and further
        ticks are HELD (no session, no repo prep, no provider) until the
        backoff deadline, never re-offered every tick the way the old
        rotation-queue behavior would have."""
        from nanobot.runtime import open_increment

        state_dir = _wire(tmp_path, monkeypatch, _LLMDeadSubagentManager)
        title = "Add markdown catalog link path resolver to workspace_validation_helpers.py"
        _seed_bridge_request(state_dir, "req-outage", "cycle-outage", task_title=title)
        _stub_planning_session(monkeypatch, title)
        bridge_state = state_dir / "subagent_bridge"

        rc = asyncio.run(bridge._main_impl())
        assert rc == bridge.EXIT_SUPPLIER_PAUSED

        # (1) the ledger row: paused-supplier, not failed, with both the
        # classifier's decision AND the raw error text on the row.
        outcome = [r for r in _read_ledger(state_dir) if r["phase"] == "outcome"][-1]
        assert outcome["outcome"] == "paused-supplier"
        assert outcome["reason"] == "llm_supplier_paused"
        assert outcome["llm_error_classification"]["class"] == "paused-supplier"
        assert "Connection error" in outcome["llm_error_classification"]["raw_error"]
        assert bridge._classify_llm_error(TRANSPORT_ERROR) == "paused-supplier"

        # (2) no error card: no error_card_recording ledger row at all.
        assert not any(r["phase"] == "error_card_recording" for r in _read_ledger(state_dir))

        # (3) no marker, no retry counter anywhere -- the bounded llm_error
        # retry mechanism this would otherwise share is never touched.
        assert list(bridge_state.glob("handled_*.txt")) == []
        assert list(bridge_state.glob("retry_*.json")) == []

        # (4) demand cooling never sees this as a recent failure.
        assert bridge._recent_failure_match(title, state_dir) is None

        # (5) the interrupted plan is now the pending open increment.
        pending = open_increment.pending_open_increment(state_dir)
        assert pending is not None
        assert pending["reason"] == "interrupted_supply"

        # (6) a tick before the backoff deadline: held, no session at all --
        # nothing new in the ledger, nothing in bridge_state either.
        rows_before = _read_ledger(state_dir)
        rc = asyncio.run(bridge._main_impl())
        assert rc == 0
        rows_after = _read_ledger(state_dir)
        # Exactly one new row: the held-tick's own bookkeeping (mirrors
        # planner_rest's `planner_rest_held` event) -- no cycle phases
        # (started/dedup/gate/outcome) at all, since no session ran.
        new_rows = rows_after[len(rows_before):]
        assert [r["phase"] for r in new_rows] == ["open_increment_held"]
        assert list(bridge_state.glob("handled_*.txt")) == []
        assert list(bridge_state.glob("retry_*.json")) == []

        # (7) a tick after the deadline: the session runs again (simulate
        # cooldown elapsed -- real wait is minutes), open_increment still
        # first (this stub never emits a resolution decision).
        oi_state = open_increment.load_state(state_dir)
        oi_state.hold["deadline"] = "2000-01-01T00:00:00Z"
        open_increment._save_state(state_dir, oi_state)
        rc = asyncio.run(bridge._main_impl())
        assert rc == bridge.EXIT_SUPPLIER_PAUSED
        assert open_increment.pending_open_increment(state_dir) is not None
        # Consecutive interruption of the SAME retry_key -- backoff doubled.
        assert open_increment.load_state(state_dir).consecutive_supply_interrupts == 2

    def test_zero_commit_interruption_persist_failure_aborts_the_tick(self, tmp_path, monkeypatch):
        """Codex re-check on `bf8f0537` (P1): the zero-commit branch of
        this same interruption path (cycle_commit_count == 0, no D1
        barrier to gate it) used to discard record_supply_interruption's
        `.persisted` signal entirely, letting the tick reach the normal
        finishing path even when the write was never verified -- the
        interrupted plan then ends up in neither `pending` nor `running`.
        """
        from nanobot.runtime import open_increment

        state_dir = _wire(tmp_path, monkeypatch, _LLMDeadSubagentManager)
        title = "Add markdown catalog link path resolver to workspace_validation_helpers.py"
        _seed_bridge_request(state_dir, "req-outage-persistfail", "cycle-outage-persistfail", task_title=title)
        _stub_planning_session(monkeypatch, title)

        # Only the interruption write itself fails -- a blanket
        # _save_state stub would also break the registration write that
        # already landed before this call (record_attempt_started only
        # ever touches `running`, never `pending`).
        _real_save_state = open_increment._save_state

        def _fail_on_pending_write(sd, st):
            if st.pending is not None:
                return False
            return _real_save_state(sd, st)

        monkeypatch.setattr(open_increment, "_save_state", _fail_on_pending_write)

        rc = asyncio.run(bridge._main_impl())
        assert rc == 0

        assert open_increment.pending_open_increment(state_dir) is None, (
            "an unverified interruption write must not be trusted as a real pending increment"
        )
        state = open_increment.load_state(state_dir)
        assert state.running is not None, "the registration must survive for the next tick's kill-check to retry"

    def test_once_the_supplier_recovers_the_same_request_completes_normally(self, tmp_path, monkeypatch):
        """The candidate that survived the outage above is not stuck -- once
        the supplier is healthy again (a normal manager) and the backoff
        deadline has passed, the very same candidate completes like it
        never happened. (Resolving the pending open increment itself is the
        real planning session's job via `open_increment_decision`, not
        exercised by the stub here.)"""
        from nanobot.runtime import open_increment
        from tests.test_cycle_ledger import _FakeSubagentManager

        state_dir = _wire(tmp_path, monkeypatch, _LLMDeadSubagentManager)
        title = "Add markdown catalog link path resolver to workspace_validation_helpers.py"
        _seed_bridge_request(state_dir, "req-recovers", "cycle-recovers", task_title=title)
        _stub_planning_session(monkeypatch, title)
        key = bridge._retry_key_for(None, title)

        rc = asyncio.run(bridge._main_impl())
        assert rc == bridge.EXIT_SUPPLIER_PAUSED
        assert open_increment.pending_open_increment(state_dir) is not None

        # Force the backoff deadline into the past (simulates cooldown
        # elapsed) before the supplier recovers.
        oi_state = open_increment.load_state(state_dir)
        oi_state.hold["deadline"] = "2000-01-01T00:00:00Z"
        open_increment._save_state(state_dir, oi_state)

        monkeypatch.setattr(bridge, "SubagentManager", _FakeSubagentManager)
        rc = asyncio.run(bridge._main_impl())
        assert rc == 0
        outcome = [r for r in _read_ledger(state_dir) if r["phase"] == "outcome"][-1]
        assert outcome["outcome"] == "success"
        assert (state_dir / "subagent_bridge" / f"handled_{key}.txt").exists()


class _IncompleteCallManager(_LLMDeadSubagentManager):
    """The executor's model call ended without a usable response and without
    any supplier-outage wording: call-site evidence says model_call, the
    error type is not a supplier one -> model_call_incomplete."""

    error_text = "Error: LLM execution failed: ValueError: provider response ended before the final chunk"

    async def spawn(self, **kwargs):
        result = await super().spawn(**kwargs)
        path = bridge.STATE_DIR / "subagents" / f"{self.task_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["model_call_failure"] = {
            "stage": "model_call", "call_stage": "model_call",
            "error_type": "ValueError", "message": self.error_text,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return result


class TestIncompleteRetryCounterIsReadUnderTheWritersKey:
    def test_both_readers_find_the_counter_the_real_writer_wrote(self, tmp_path, monkeypatch):
        """#1919 review P1: after #1942 every cycle mints a fresh request_id
        and the handled/retry bookkeeping keys on the stable retry_key. The
        counter is written by the real _decide_handled_marker inside a real
        cycle, and must be found by the attempt label (error_card_recording)
        and by the recent-failure suppression -- neither may look it up
        under the per-cycle request_id."""
        state_dir = _wire(tmp_path, monkeypatch, _IncompleteCallManager)
        title = "Add retry-key regression coverage for incomplete model calls"
        _seed_bridge_request(state_dir, "req-incomplete", "cycle-incomplete", task_title=title)
        _stub_planning_session(monkeypatch, title)
        key = bridge._retry_key_for(None, title)

        asyncio.run(bridge._main_impl())

        outcome = [r for r in _read_ledger(state_dir) if r["phase"] == "outcome"][-1]
        assert outcome["reason"] == "model_call_incomplete", outcome
        bridge_state = state_dir / "subagent_bridge"
        assert (bridge_state / f"retry_incomplete_{key}.json").exists(), sorted(p.name for p in bridge_state.iterdir())
        results = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (state_dir / "subagents" / "results").rglob("*.json")
        ]
        assert results and all(r.get("retry_key") == key and r.get("request_id") != key for r in results), results

        # reader 1: the attempt label on the error-card row
        card_rows = [r for r in _read_ledger(state_dir) if r["phase"] == "error_card_recording"]
        assert card_rows and card_rows[-1].get("attempt") == f"1/{bridge.MODEL_CALL_INCOMPLETE_MAX_RETRIES}", card_rows

        # reader 2: recent-failure suppression, before and after exhaustion
        assert bridge._recent_failure_match(title, state_dir) is None
        marker = bridge_state / f"handled_{key}.txt"
        for _ in range(bridge.MODEL_CALL_INCOMPLETE_MAX_RETRIES - 1):
            bridge._decide_handled_marker(marker, "req.json", llm_error=True, model_call_incomplete=True)
        assert bridge._recent_failure_match(title, state_dir) is not None


# ─── the exit-code consumers: crash_record's exit_streak, health.py ───────


class TestExitStreakAndHealthDoNotCountAnOutage:
    """#1765 review: only changing the ledger row is not enough -- the
    process exits non-zero, and TWO independent writers key on that exit
    code, not the ledger: the bridge's own __main__ guard
    (source="process") and crash_record's systemd ExecStopPost= CLI
    (source="systemd"). Both must skip EXIT_SUPPLIER_PAUSED, or
    consecutive_failures still climbs on a supplier outage no matter what
    the ledger says.
    """

    def test_supplier_paused_exit_code_is_distinct_from_executor_llm_error(self):
        assert bridge.EXIT_SUPPLIER_PAUSED not in (0, bridge.EXIT_EXECUTOR_LLM_ERROR, bridge.EXIT_SYSTEM_PROMPT_OVERFLOW)

    def test_incomplete_model_call_has_distinct_nonzero_exit_code(self):
        assert bridge.EXIT_MODEL_CALL_INCOMPLETE not in (
            0, bridge.EXIT_EXECUTOR_LLM_ERROR, bridge.EXIT_SYSTEM_PROMPT_OVERFLOW,
            bridge.EXIT_SUPPLIER_PAUSED,
        )

    def test_crash_record_mirrors_model_call_incomplete_exit_code(self):
        from nanobot import crash_record
        assert crash_record.MODEL_CALL_INCOMPLETE_EXIT_CODE == bridge.EXIT_MODEL_CALL_INCOMPLETE

    def test_crash_record_mirrors_the_same_exit_code_value(self):
        """crash_record.py must not import bridge.py (module docstring) --
        the value is mirrored as a literal there; this pins the two from
        drifting apart silently."""
        from nanobot import crash_record
        assert crash_record.SUPPLIER_PAUSED_EXIT_CODE == bridge.EXIT_SUPPLIER_PAUSED

    def test_main_guard_source_skips_record_exit_for_supplier_paused(self):
        """Static-inspection test, same technique as
        test_bridge_exit_record.py::test_bridge_guard_records_the_exit_code_and_stays_last
        -- the `if __name__ == "__main__":` guard is the last module
        statement and cannot be exercised as an importable function, so its
        SHAPE is asserted from the source instead."""
        import ast
        from pathlib import Path as _Path

        repo_root = _Path(bridge.__file__).resolve().parents[2]
        src = (repo_root / "nanobot" / "runtime" / "bridge.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        guard = tree.body[-1]
        assert isinstance(guard, ast.If) and "__main__" in ast.dump(guard.test)
        body_src = ast.get_source_segment(src, guard)
        assert "_exit_code == EXIT_MODEL_CALL_INCOMPLETE" in body_src
        assert "outcome=\"model_call_incomplete\"" in body_src
        assert "outcome_classification=\"model_call_incomplete\"" in body_src
        assert "update_streak=False" in body_src
        assert "if not _skip_exit_record:" in body_src
        assert "_crash_record.record_exit(" in body_src
        assert "if BRIDGE_ENABLED:" in body_src

    def test_systemd_cli_records_model_call_incomplete_without_streak_mutation(self, tmp_path):
        from nanobot import crash_record

        state = tmp_path / "state"
        crash_record._start_run_marker(state)
        crash_record.record_exit(state, outcome="failure", exit_status=1, error="NameError: x")
        crash_record._start_run_marker(state)
        before = crash_record._load_streak(state / "bridge" / "exit_streak.json")
        assert before["consecutive_failures"] == 1

        rc = crash_record.main([
            "--source", "systemd",
            "--exit-status", str(bridge.EXIT_MODEL_CALL_INCOMPLETE),
            "--exit-code", "exited",
            "--service-result", "exit-code",
            "--state-dir", str(state),
        ])
        assert rc == 0
        after = crash_record._load_streak(state / "bridge" / "exit_streak.json")
        assert after["consecutive_failures"] == before["consecutive_failures"]
        assert after["last_failure_ts"] == before["last_failure_ts"]
        rows = [json.loads(line) for line in (state / "bridge" / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["classification"] == "model_call_incomplete"
        assert rows[-1]["outcome"] == "model_call_incomplete"
        exit_rows = [json.loads(line) for line in (state / "bridge" / "exits.jsonl").read_text(encoding="utf-8").splitlines()]
        assert exit_rows[-1]["outcome"] == "model_call_incomplete"
        assert exit_rows[-1]["classification"] == "model_call_incomplete"
        assert exit_rows[-1]["streak_updated"] is False
        assert after["total_records"] == before["total_records"]

    def test_systemd_cli_skips_record_exit_for_supplier_paused_and_streak_is_unchanged(self, tmp_path):
        from nanobot import crash_record

        state = tmp_path / "state"
        # A prior real failure establishes a non-zero streak to prove the
        # supplier-paused CLI call below does not touch it either way.
        crash_record.record_exit(state, outcome="failure", exit_status=1, error="NameError: x")
        before = crash_record._load_streak(state / "bridge" / "exit_streak.json")
        assert before["consecutive_failures"] == 1

        rc = crash_record.main([
            "--source", "systemd",
            "--exit-status", str(bridge.EXIT_SUPPLIER_PAUSED),
            "--exit-code", "exited",
            "--service-result", "exit-code",
            "--state-dir", str(state),
        ])
        assert rc == 0
        after = crash_record._load_streak(state / "bridge" / "exit_streak.json")
        assert after["consecutive_failures"] == 1  # unchanged -- no second failure recorded
        assert after["total_failures"] == 1

    def test_health_read_cycle_progress_excludes_paused_supplier_from_the_stall_count(self, tmp_path):
        """#1765 review: scripts/eeebot_dashboard.py's collect_metrics_uncached
        reads health.read_cycle_progress -- a run of paused-supplier rows must
        not trip the CRIT 'stalled' count-based alert the way a real run of
        failures would. hours_since_last_success is unaffected either way."""
        from nanobot.runtime import health

        state = tmp_path / "state"
        ledger_dir = state / "ledger"
        ledger_dir.mkdir(parents=True)
        rows = [{"phase": "outcome", "cycle_id": "c-success", "outcome": "success",
                 "ts": "2026-09-01T00:00:00Z"}]
        # 25 consecutive paused-supplier rows -- more than the 20-cycle
        # threshold, which a real failure streak of this length WOULD trip.
        for i in range(25):
            rows.append({
                "phase": "outcome", "cycle_id": f"c-outage-{i}", "outcome": "paused-supplier",
                "reason": "llm_supplier_paused",
                "ts": f"2026-09-01T00:{i + 1:02d}:00Z",
            })
        (ledger_dir / "cycles.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
        )
        progress = health.read_cycle_progress(state, since_ts="2026-08-01T00:00:00Z")
        assert progress["consecutive_non_integrating_cycles"] == 0
        # The count threshold is exempt, but the independent time-based alert
        # still fires because the last successful cycle is older than 8 hours.
        assert progress["state"] == "stalled"
        assert progress["alert"] is True
        assert progress["dominant_reason"] is None

    def test_incomplete_calls_still_trigger_time_based_stall_alert(self, tmp_path):
        from nanobot.runtime import health

        state = tmp_path / "state"
        ledger_dir = state / "ledger"
        ledger_dir.mkdir(parents=True)
        rows = [{"phase": "outcome", "cycle_id": "c-success", "outcome": "success",
                 "ts": "2026-09-01T00:00:00Z"}]
        for i in range(3):
            rows.append({
                "phase": "outcome", "cycle_id": f"c-incomplete-{i}",
                "outcome": "model_call_incomplete", "reason": "model_call_incomplete",
                "ts": f"2026-09-01T00:{i + 1:02d}:00Z",
            })
        (ledger_dir / "cycles.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8",
        )
        progress = health.read_cycle_progress(
            state, since_ts="2026-08-01T00:00:00Z",
            now=datetime(2026, 9, 3, tzinfo=timezone.utc).timestamp(),
        )
        assert progress["state"] == "stalled"
        assert progress["alert"] is True
        assert progress["consecutive_non_integrating_cycles"] == 0

    def test_health_a_real_failure_streak_of_the_same_length_still_alerts(self, tmp_path):
        """Control: this is not a change to the threshold itself -- a
        genuine run of 'failed' rows the same length still trips CRIT."""
        from nanobot.runtime import health

        state = tmp_path / "state"
        ledger_dir = state / "ledger"
        ledger_dir.mkdir(parents=True)
        rows = [{"phase": "outcome", "cycle_id": "c-success", "outcome": "success",
                 "ts": "2026-09-01T00:00:00Z"}]
        for i in range(25):
            rows.append({
                "phase": "outcome", "cycle_id": f"c-fail-{i}", "outcome": "failed",
                "reason": "gate_failed",
                "ts": f"2026-09-01T00:{i + 1:02d}:00Z",
            })
        (ledger_dir / "cycles.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
        )
        progress = health.read_cycle_progress(state, since_ts="2026-08-01T00:00:00Z")
        assert progress["consecutive_non_integrating_cycles"] == 25
        assert progress["state"] == "stalled"


# ─── goal-gap futility: excluded the same way push_pending is ─────────────


def test_paused_supplier_outcome_does_not_count_toward_family_futility(tmp_path):
    """#1765, mirrors test_push_pending_outcome_does_not_count_toward_family_futility:
    a paused-supplier row must not advance the family (defect/reflection/
    priority) run counter toward the N=6 threshold -- it says nothing about
    this attempt's own viability."""
    state = tmp_path / "state"
    item_id = "priority-test-paused-supplier"
    item = {"id": item_id, "kind": "priority", "summary": "test priority"}
    futility.futile_gap_ids(state, [item])

    rows = []
    now = datetime.now(timezone.utc)
    for i in range(6):
        cycle = f"c-paused-supplier-{i}"
        ts = (now + timedelta(seconds=i + 1)).isoformat()
        rows.append({"phase": "proposed", "cycle_id": cycle, "demand_id": item_id, "ts": ts})
        rows.append({"phase": "outcome", "cycle_id": cycle, "outcome": "paused-supplier", "ts": ts})

    futile_ids = futility.futile_gap_ids(state, [item], ledger_rows=rows)
    assert item_id not in futile_ids

    rec = json.loads((state / "demand" / "futility.json").read_text(encoding="utf-8"))[item_id]
    assert rec["attempt_count"] == 0
    assert rec["futile"] is False


# ─── scorecard: its own counter, excluded from failure metrics ────────────


class TestScorecardPausedSupplierCounters:
    def _rows(self, *, paused=2, failed=1, success=1):
        rows = []
        now = datetime.now(timezone.utc)
        t = 0
        for i in range(paused):
            cid = f"c-paused-{i}"
            start = (now + timedelta(seconds=t)).isoformat()
            t += 30
            end = (now + timedelta(seconds=t)).isoformat()
            rows.append({"phase": "started", "cycle_id": cid, "ts": start})
            rows.append({"phase": "outcome", "cycle_id": cid, "outcome": "paused-supplier", "ts": end})
            t += 5
        for i in range(failed):
            cid = f"c-failed-{i}"
            ts = (now + timedelta(seconds=t)).isoformat()
            t += 5
            rows.append({"phase": "outcome", "cycle_id": cid, "outcome": "failed", "reason": "gate_failed", "ts": ts})
        for i in range(success):
            cid = f"c-success-{i}"
            ts = (now + timedelta(seconds=t)).isoformat()
            t += 5
            rows.append({"phase": "outcome", "cycle_id": cid, "outcome": "success", "ts": ts})
        return rows

    def test_1765_breakdown_counts_events_tasks_and_keeps_legacy_rate(self):
        rows = [
            {"phase": "proposed", "cycle_id": "f1", "demand_id": "task-a", "ts": "2026-09-20T00:00:00Z"},
            {"phase": "outcome", "cycle_id": "f1", "outcome": "failed", "ts": "2026-09-20T00:01:00Z"},
            {"phase": "proposed", "cycle_id": "f2", "demand_id": "task-a", "ts": "2026-09-20T00:02:00Z"},
            {"phase": "outcome", "cycle_id": "f2", "outcome": "failed", "ts": "2026-09-20T00:03:00Z"},
            {"phase": "proposed", "cycle_id": "s1", "demand_id": "task-b", "ts": "2026-09-20T00:04:00Z"},
            {"phase": "outcome", "cycle_id": "s1", "outcome": "paused-supplier", "ts": "2026-09-20T00:05:00Z"},
            {"phase": "proposer_reject", "reason": "self_dedup", "demand_id": "task-c", "ts": "2026-09-20T00:06:00Z"},
        ]
        loop = scorecard._loop_section(rows, ledger_status="complete")
        assert loop["execution_failure_events"] == 2
        assert loop["execution_failure_tasks"] == 1
        assert loop["model_unavailable_events"] == 1
        assert loop["model_unavailable_tasks"] == 1
        assert loop["self_dedup_events"] == 1
        assert loop["self_dedup_tasks"] == 1
        assert loop["repeat_failure_rate"] == round(1 / 4, 4)
        assert loop["repeat_failure_rate_new"] == round(2 / 4, 4)
        assert loop["repeat_failure_rate"] == round(1 / 4, 4)
        assert loop["model_call_incomplete_events"] == 0
        assert loop["model_call_incomplete_tasks"] == 0
        assert loop["model_call_incomplete_share"] == 0.0
        assert loop["unknown_failure_cause_events"] == "unavailable"

    def test_model_call_incomplete_fields_are_populated_from_ledger(self):
        rows = [
            {"phase": "proposed", "cycle_id": "c-incomplete", "demand_id": "task-a"},
            {"phase": "outcome", "cycle_id": "c-incomplete", "outcome": "model_call_incomplete"},
        ]
        loop = scorecard._loop_section(rows, ledger_status="complete")
        assert loop["model_call_incomplete_events"] == 1
        assert loop["model_call_incomplete_tasks"] == 1
        assert loop["model_call_incomplete_share"] == 1.0
        assert loop["execution_failure_and_incomplete_events"] == 1
        assert loop["execution_failure_tasks"] == 0
        assert loop["execution_failure_events"] == 0
        assert loop["wasted_attempts"] == 0

    def test_model_call_incomplete_fields_stay_unavailable_if_ledger_unavailable(self):
        loop = scorecard._loop_section([], ledger_status="unavailable")
        assert loop["model_call_incomplete_events"] == "unavailable"
        assert loop["model_call_incomplete_tasks"] == "unavailable"
        assert loop["model_call_incomplete_share"] == "unavailable"

    def test_paused_supplier_counted_and_timed_separately_from_failed(self):
        loop = scorecard._loop_section(self._rows(paused=2, failed=1, success=1), ledger_status="complete")
        assert loop["paused_supplier_outcomes"] == 2
        assert loop["paused_supplier_seconds"] == pytest.approx(60.0, abs=1.0)
        assert loop["paused_supplier_seconds_unknown_count"] == 0
        # Excluded entirely from the failure-derived metrics.
        assert loop["wasted_attempts"] == 1  # the one genuine 'failed' row only
        assert loop["repeat_failures"] == 0

    def test_missing_started_row_counts_as_unknown_not_zero(self):
        rows = [
            {"phase": "outcome", "cycle_id": "c-orphan", "outcome": "paused-supplier",
             "ts": datetime.now(timezone.utc).isoformat()},
        ]
        loop = scorecard._loop_section(rows, ledger_status="complete")
        assert loop["paused_supplier_outcomes"] == 1
        assert loop["paused_supplier_seconds"] == 0.0
        assert loop["paused_supplier_seconds_unknown_count"] == 1

    def test_no_data_is_distinguishable_from_zero(self):
        """An unavailable ledger window reports 'unavailable', never a
        fabricated 0 that would read as 'no outages happened'."""
        loop = scorecard._loop_section([], ledger_status="unavailable")
        assert loop["paused_supplier_outcomes"] == "unavailable"
        assert loop["paused_supplier_seconds"] == "unavailable"
        assert loop["paused_supplier_seconds_unknown_count"] == "unavailable"

    def test_zero_outages_is_a_real_zero_not_unavailable(self):
        loop = scorecard._loop_section(self._rows(paused=0, failed=1, success=1), ledger_status="complete")
        assert loop["paused_supplier_outcomes"] == 0
        assert loop["paused_supplier_seconds"] == 0.0
