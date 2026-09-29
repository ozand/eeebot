"""#1280: a cycle whose executor LLM call never returned must not be recorded
as ``completed``.

On 2026-09-04 the local model was unreachable for ~4.5 h. Twelve subagents
died with ``LLM execution failed … litellm.InternalServerError … Connection
error``; eleven of their cycles were recorded ``result_status: completed``,
ledger ``partial``, ``exit_streak.json`` stayed at ``consecutive_failures: 0``,
and every request was retired by the unconditional ``handled_`` marker so
nothing was retried when the model came back.

Drives ``bridge._main_impl`` end to end with a fake SubagentManager whose
spawn writes exactly the telemetry the real one writes on that failure, and
asserts all three: the recorded status (and the gate seeing it), the exit
code that becomes the streak failure, and the absence of the marker with a
bounded retry.

#1765: ``TRANSPORT_ERROR`` below (litellm.InternalServerError / "Connection
error" — the exact 2026-09-04 incident text) is a SUPPLIER-side failure by
#1765's own classifier and now correctly records ``outcome: "paused-supplier"``
rather than ``"failed"`` — see ``tests/test_paused_supplier_1765.py`` for
that behaviour (no error card, no retry counter ever, unconditional
re-offer). The tests in ``TestExecutorLLMErrorIsAFailure`` below were
rewritten to use ``CLIENT_DEFECT_ERROR`` instead, a genuine OUR-OWN-DEFECT
shape (context length exceeded) that #1765 leaves classified ``"failed"`` —
preserving their original intent (the bounded retry-then-retire contract,
which #1765 explicitly keeps for the class it did not touch) against a
fixture the new classifier still calls a real failure.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import pytest

from nanobot import crash_record
from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)

logger = logging.getLogger(__name__)

TRANSPORT_ERROR = (
    "Error: LLM execution failed: Error calling LLM: litellm.InternalServerError: "
    "InternalServerError: OpenAIException - litellm.InternalServerError: "
    "OpenAIException - Connection error. No fallback model group found for original model group"
)

#: #1765: a genuine OUR-OWN-REQUEST defect — the supplier answered and
#: rejected it — kept classified ``"failed"`` by the new classifier (no
#: connection/timeout/5xx/rate-limit/not-found signal anywhere in the text).
CLIENT_DEFECT_ERROR = (
    "Error: LLM execution failed: Error calling LLM: litellm.BadRequestError: "
    "OpenAIException - This model's maximum context length is 8192 tokens. "
    "However, you requested 9214 tokens in the messages, please reduce."
)


class _LLMDeadSubagentManager:
    """The real manager on 2026-09-04: spawn registers a task, the task's LLM
    call raises, ``_run_subagent`` writes ``status: error`` telemetry and
    returns without touching the repo."""

    task_id = "0b221168"
    error_text = TRANSPORT_ERROR

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace
        self._running_tasks: dict = {}

    async def spawn(self, **_kwargs):
        telemetry_dir = bridge.STATE_DIR / "subagents"
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        (telemetry_dir / f"{self.task_id}.json").write_text(json.dumps({
            "task_id": self.task_id,
            "status": "error",
            "summary": self.error_text,
            "result": self.error_text,
            "started_at": "2026-09-04T04:01:31Z",
            "finished_at": "2026-09-04T04:04:45Z",
        }), encoding="utf-8")

        async def _done():
            return None

        # bridge captures next(iter(mgr._running_tasks)) right after spawn.
        self._running_tasks[self.task_id] = asyncio.ensure_future(_done())
        return "fake subagent spawned (LLM dead)"


class _LLMBadRequestSubagentManager(_LLMDeadSubagentManager):
    """#1765: same transport (a dead LLM call, zero commits) but a genuine
    OUR-OWN-REQUEST defect — stays classified ``"failed"``."""

    task_id = "c3213aa1"
    error_text = CLIENT_DEFECT_ERROR


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


def _stub_planning_session(monkeypatch, plan_text: str, candidate_id: "str | None" = None):
    """ADR-035 rule 1 (#1942): req/task now come from the planning session's
    own plan, never a rotation-picked queue file -- stand in a plan whose
    text IS the title these tests seed, so a repeated call keeps deriving
    the SAME _retry_key_for(candidate_id, plan_text) across cycles, exactly
    as the retired stable-queue-filename keying used to provide."""
    async def _fake_planning_session(**_kwargs):
        return {
            'ran': True, 'iterations_used': 1, 'iterations_planned': 1,
            'tampered_files': [], 'plan': {'plan': plan_text, 'candidate_id': candidate_id},
        }

    monkeypatch.setattr(bridge, "_run_planning_session", _fake_planning_session)


def _llm_call_rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_executor_call_telemetry_uses_exact_model(tmp_path, monkeypatch):
    from nanobot.observability import llm_telemetry

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "llm_calls"))
    model = "openai/an/provider/model-with-route"
    with llm_telemetry.call_context("cycle-executor", "executor"):
        llm_telemetry.record_llm_call(
            model=model, duration_ms=12.5, usage={}, finish_reason="error", retries=0,
        )
    rows = _llm_call_rows(tmp_path / "llm_calls" / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")
    assert rows[-1]["component"] == "executor"
    assert rows[-1]["cycle_id"] == "cycle-executor"
    assert rows[-1]["model"] == model


@pytest.mark.asyncio
async def test_executor_telemetry_recording_failure_is_logged(caplog, monkeypatch):
    from nanobot.providers import base

    class Provider(base.LLMProvider):
        async def chat(self, **_kwargs):
            return base.LLMResponse(content="ok")

        def get_default_model(self):
            return "test-model"

    def _fail(**_kwargs):
        raise OSError("llm_calls unavailable")

    monkeypatch.setattr(base, "record_llm_call", _fail)
    with caplog.at_level(logging.WARNING):
        response = await Provider().chat_with_retry(messages=[{"role": "user", "content": "x"}])
    assert response.content == "ok"
    assert "llm call telemetry recording failed: llm_calls unavailable" in caplog.text


def _result_for(state_dir, request_id=None):
    """ADR-035 rule 1 (#1942): result files are keyed by the per-cycle
    request_id (a fresh uuid every run), never a stable seeded id -- returns
    the most-recently-written result instead. ``request_id`` is accepted
    and ignored for call-site compatibility with tests written before this."""
    del request_id
    matches = sorted(
        (state_dir / "subagents").rglob("result-*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    assert matches, "no result written"
    return json.loads(matches[-1].read_text(encoding="utf-8"))


class TestExecutorLLMErrorIsAFailure:
    def test_dead_llm_cycle_is_blocked_failed_unmarked_and_exits_nonzero(self, tmp_path, monkeypatch):
        state_dir = _wire(tmp_path, monkeypatch, _LLMBadRequestSubagentManager)
        title = "Add markdown catalog link path resolver to workspace_validation_helpers.py"
        _seed_bridge_request(state_dir, "req-dead", "cycle-dead", task_title=title)
        _stub_planning_session(monkeypatch, title)
        # ADR-035 rule 1 (#1942): req/task come from the plan now, so the
        # retry/handled bookkeeping keys on _retry_key_for's stable hash of
        # the plan's own title, not a queue file's request_id.
        key = bridge._retry_key_for(None, title)

        rc = asyncio.run(bridge._main_impl())

        # (1) recorded status: `blocked` with the reason set — both tests
        # _recent_failure_match applies. But while the request still has
        # retry budget the row must NOT feed suppression, or the #716 branch
        # retires the request on its next offer and the retry never runs
        # (review finding on PR #1282). Once the budget is spent it counts.
        res = _result_for(state_dir)
        assert res["result_status"] == "blocked"
        assert res["rollback"]["reason"] == "executor_llm_error"
        assert res["commits_pushed"] == 0
        # ADR-035 rule 1: no source_artifact anymore, so backlog_title is
        # empty; semantic_task_id carries the plan's own title instead.
        assert res["backlog_title"] == ""
        assert res["semantic_task_id"] == title
        assert res["retry_key"] == key
        assert any("EXECUTOR LLM ERROR (#1280)" in s and "maximum context length" in s for s in res.get("key_learnings") or [])
        assert "model=" in res["key_learnings"][0] or any("model=" in s for s in res.get("key_learnings") or [])
        assert bridge._recent_failure_match(title, state_dir) is None
        assert bridge._llm_error_retries_exhausted(key) is False
        # Same row, budget spent: the failure proxy sees it like any other.
        retry_path = state_dir / "subagent_bridge" / f"retry_{key}.json"
        retry_path.write_text(json.dumps({"count": bridge.LLM_ERROR_MAX_RETRIES}), encoding="utf-8")
        assert bridge._recent_failure_match(title, state_dir) == title
        retry_path.write_text(json.dumps({"count": 1, "max": bridge.LLM_ERROR_MAX_RETRIES}), encoding="utf-8")

        rows = _read_ledger(state_dir)
        outcome = [r for r in rows if r["phase"] == "outcome"][-1]
        assert outcome["outcome"] == "failed"
        assert outcome["reason"] == "executor_llm_error"

        # (2) the streak: the process exits non-zero, and the __main__ guard's
        # own expression turns that into a `failure` row.
        assert rc == bridge.EXIT_EXECUTOR_LLM_ERROR != 0
        streak = crash_record.record_exit(
            state_dir, outcome="success" if rc == 0 else "failure", exit_status=rc,
        )
        assert streak["consecutive_failures"] == 1
        assert streak["last_outcome"] == "failure"
        assert streak["last_exit_status"] == bridge.EXIT_EXECUTOR_LLM_ERROR

        # (3) no handled_ marker: the request is re-offered; a retry counter
        # records the attempt.
        bridge_state = state_dir / "subagent_bridge"
        assert not (bridge_state / f"handled_{key}.txt").exists()
        retry = json.loads((bridge_state / f"retry_{key}.json").read_text(encoding="utf-8"))
        assert retry["count"] == 1 and retry["max"] == bridge.LLM_ERROR_MAX_RETRIES

    def test_retry_is_bounded_then_the_request_is_retired(self, tmp_path, monkeypatch, capsys):
        """The planner keeps re-proposing the SAME title every cycle (ADR-035
        rule 1, #1942: there is no queue left to retire an entry from — the
        planning session is stubbed here to always return the identical
        plan, standing in for "the planner has not yet noticed the failure
        and moved on"). _retry_key_for's stable hash of that title is what
        makes the bounded retry-then-retire contract (#1280) survive across
        cycles now that request_id/cycle_id no longer do."""
        state_dir = _wire(tmp_path, monkeypatch, _LLMBadRequestSubagentManager)
        monkeypatch.setattr(bridge, "LLM_ERROR_MAX_RETRIES", 3)
        title = "Add markdown catalog link path resolver to workspace_validation_helpers.py"
        _seed_bridge_request(state_dir, "req-loop", "cycle-loop", task_title=title)
        _stub_planning_session(monkeypatch, title)
        key = bridge._retry_key_for(None, title)
        bridge_state = state_dir / "subagent_bridge"

        for attempt in (1, 2):
            rc = asyncio.run(bridge._main_impl())
            out = capsys.readouterr().out
            assert rc == bridge.EXIT_EXECUTOR_LLM_ERROR, out
            assert "matches recent failure/rejection" not in out, out  # suppression must not fire yet
            assert f"request left pending for retry ({attempt}/3)" in out
            assert not (bridge_state / f"handled_{key}.txt").exists()
            assert json.loads((bridge_state / f"retry_{key}.json").read_text())["count"] == attempt
            assert _result_for(state_dir)["semantic_task_id"] == title
            assert bridge._recent_failure_match(title, state_dir) is None

        rc = asyncio.run(bridge._main_impl())
        out = capsys.readouterr().out
        assert rc == bridge.EXIT_EXECUTOR_LLM_ERROR, out
        assert "request retired after 3 failed LLM attempts" in out
        assert (bridge_state / f"handled_{key}.txt").exists(), "third failure must retire the request"
        assert json.loads((bridge_state / f"retry_{key}.json").read_text())["count"] == 3
        # Budget spent: the title now counts for the 24 h suppression window,
        # so a re-minted proposal with the same title is held back.
        assert bridge._llm_error_retries_exhausted(key) is True
        assert bridge._recent_failure_match(title, state_dir) == title

        # Fourth run: the planner proposes the same title again, but the
        # pre-spawn recent-failure gate now suppresses it before any spawn
        # -- never reaching _decide_handled_marker, so the retry counter is
        # untouched (no fifth-cycle "already_handled" print left to check;
        # that belonged to the retired rotation queue).
        rc = asyncio.run(bridge._main_impl())
        out = capsys.readouterr().out
        assert rc == 0
        assert "matches recent failure/rejection" in out
        assert json.loads((bridge_state / f"retry_{key}.json").read_text())["count"] == 3

    def test_lost_counter_and_result_rows_between_attempts_cannot_reoffer_indefinitely(self, tmp_path, monkeypatch, capsys):
        """#1282 review, the demonstrated case: when BOTH the retry counter and
        the failed result rows vanish between offers (results migrate into the
        pruned archive within a cycle; the counter is one file), the #716
        suppression finds nothing, so the writer's own fallback is the only
        thing left that can stop an unbounded re-offer. Its witness is the
        ledger: a recorded executor_llm_error outcome for this retry_key means
        the candidate was previously live, and a missing counter then retires
        it — the same direction the reader takes. Counter-only loss is NOT
        tested here because that case already retires via the suppression
        branch and would pass while proving nothing.

        ADR-035 rule 1 (#1942) changed what "retired" means at the edges: the
        planner keeps proposing (there is no queue for a handled_ marker to
        remove an entry from), so a lost-state retirement recurs every time
        the planner re-proposes the same title while its own evidence
        (results, ledger rows for this retry_key) is gone -- but it recurs as
        the SAME bounded, correctly-classified retired_state_lost outcome,
        never as an unbounded/incorrectly-restarted retry count. That
        bounded-recurrence property is what this test now pins for the third
        offer, replacing the retired rotation queue's "already_handled, no
        third spawn" behavior."""
        state_dir = _wire(tmp_path, monkeypatch, _LLMBadRequestSubagentManager)
        monkeypatch.setattr(bridge, "LLM_ERROR_MAX_RETRIES", 3)
        title = "Add markdown catalog link path resolver to workspace_validation_helpers.py"
        _seed_bridge_request(state_dir, "req-lost", "cycle-lost", task_title=title)
        _stub_planning_session(monkeypatch, title)
        key = bridge._retry_key_for(None, title)
        bridge_state = state_dir / "subagent_bridge"

        def lose_state():
            (bridge_state / f"retry_{key}.json").unlink(missing_ok=True)
            for p in (state_dir / "subagents").rglob("result-*.json"):
                p.unlink()

        rc = asyncio.run(bridge._main_impl())
        out = capsys.readouterr().out
        assert rc == bridge.EXIT_EXECUTOR_LLM_ERROR
        assert "request left pending for retry (1/3)" in out
        assert json.loads((bridge_state / f"retry_{key}.json").read_text())["count"] == 1
        lose_state()
        # The ledger still knows this retry_key failed on the LLM once.
        assert bridge._recorded_llm_error_attempts(state_dir, key) == 1
        assert bridge._recent_failure_match(title, state_dir) is None  # nothing left to match

        # Second offer with all retry state gone: must retire, not restart at 1.
        rc = asyncio.run(bridge._main_impl())
        out = capsys.readouterr().out
        assert rc == bridge.EXIT_EXECUTOR_LLM_ERROR, out
        assert "retired_state_lost" in out and "missing after 1 recorded attempt" in out, out
        assert (bridge_state / f"handled_{key}.txt").exists(), "lost state must retire, never re-offer"
        assert not (bridge_state / f"retry_{key}.json").exists()

        # Third offer: the planner proposes the same title again (nothing
        # gates on handled_<key>.txt pre-spawn), and with the ledger/result
        # evidence lost again, retires the SAME way again -- bounded,
        # correctly classified, never restarting the retry count from 1.
        lose_state()
        rc = asyncio.run(bridge._main_impl())
        out = capsys.readouterr().out
        assert rc == bridge.EXIT_EXECUTOR_LLM_ERROR, out
        assert "retired_state_lost" in out, out
        assert not (bridge_state / f"retry_{key}.json").exists()

    def test_healthy_cycle_is_unchanged(self, tmp_path, monkeypatch):
        """Control: the pre-#1280 path — a subagent that commits real work —
        still records completed, writes the marker, and exits 0."""
        state_dir = _wire(tmp_path, monkeypatch, _FakeSubagentManager)
        title = "add feature"
        _seed_bridge_request(state_dir, "req-ok", "cycle-ok", task_title=title)
        _stub_planning_session(monkeypatch, title)
        key = bridge._retry_key_for(None, title)

        rc = asyncio.run(bridge._main_impl())

        assert rc == 0
        res = _result_for(state_dir)
        assert res["result_status"] == "completed"
        assert res["rollback"]["reason"] in ("", None)
        assert (state_dir / "subagent_bridge" / f"handled_{key}.txt").exists()
        assert not (state_dir / "subagent_bridge" / f"retry_{key}.json").exists()
        assert [r for r in _read_ledger(state_dir) if r["phase"] == "outcome"][-1]["outcome"] == "success"


class TestHelpers:
    def test_executor_llm_error_reads_only_the_failure_shape(self, tmp_path):
        sub = tmp_path / "subagents"
        sub.mkdir()
        (sub / "ok.json").write_text(json.dumps({"status": "completed", "summary": "done"}))
        (sub / "other-error.json").write_text(json.dumps({"status": "error", "summary": "Error: disk full"}))
        (sub / "dead.json").write_text(json.dumps({"status": "error", "summary": TRANSPORT_ERROR}))
        assert bridge._executor_llm_error(tmp_path, "ok") == ""
        assert bridge._executor_llm_error(tmp_path, "other-error") == ""
        assert bridge._executor_llm_error(tmp_path, "dead").startswith("Error: LLM execution failed")
        assert bridge._executor_llm_error(tmp_path, "missing") == ""
        assert bridge._executor_llm_error(tmp_path, None) == ""

    def test_decide_handled_marker_writes_marker_unless_llm_error(self, tmp_path):
        marker = tmp_path / "handled_req.txt"
        assert bridge._decide_handled_marker(marker, "req.json", llm_error=False) == "handled"
        assert marker.exists()
        marker.unlink()
        assert bridge._decide_handled_marker(marker, "req.json", llm_error=True) == "retry"
        assert not marker.exists()
        assert json.loads((tmp_path / "retry_req.json").read_text())["count"] == 1

    def test_decide_handled_marker_lost_state_retires_in_the_readers_direction(self, tmp_path, capsys):
        """Writer and reader agree: a counter that cannot be read means the
        budget is spent. Unreadable → retire (someone wrote it). Missing →
        retire when the ledger witnesses a prior attempt, first attempt when
        it does not. Each lost-state retirement is printed."""
        from nanobot.runtime.cycle_ledger import record_cycle_outcome

        marker = tmp_path / "handled_req.txt"
        counter = tmp_path / "retry_req.json"

        counter.write_text("{not json", encoding="utf-8")
        assert bridge._decide_handled_marker(marker, "req.json", llm_error=True, state_dir=tmp_path, retry_key="c1") == "retired_state_lost"
        assert marker.exists()
        assert "unreadable" in capsys.readouterr().out
        marker.unlink()
        counter.unlink()

        # Missing, no recorded attempt for this retry_key: a genuine first attempt.
        assert bridge._decide_handled_marker(marker, "req.json", llm_error=True, state_dir=tmp_path, retry_key="c1") == "retry"
        assert not marker.exists()
        counter.unlink()

        # Missing, but the ledger says this retry_key already died once: retire.
        record_cycle_outcome(tmp_path, "c1", "failed", "executor_llm_error", [], "selfevo/cycle-c1", retry_key="c1")
        assert bridge._recorded_llm_error_attempts(tmp_path, "c1") == 1
        assert bridge._decide_handled_marker(marker, "req.json", llm_error=True, state_dir=tmp_path, retry_key="c1") == "retired_state_lost"
        assert marker.exists()
        assert "missing after 1 recorded attempt" in capsys.readouterr().out
        assert bridge._llm_error_retries_exhausted("req") is True  # reader: missing counter → spent
