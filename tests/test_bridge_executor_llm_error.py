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
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot import crash_record
from nanobot.runtime import bridge
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)

TRANSPORT_ERROR = (
    "Error: LLM execution failed: Error calling LLM: litellm.InternalServerError: "
    "InternalServerError: OpenAIException - litellm.InternalServerError: "
    "OpenAIException - Connection error. No fallback model group found for original model group"
)


class _LLMDeadSubagentManager:
    """The real manager on 2026-09-04: spawn registers a task, the task's LLM
    call raises, ``_run_subagent`` writes ``status: error`` telemetry and
    returns without touching the repo."""

    task_id = "0b221168"

    def __init__(self, *, workspace, **_kwargs):
        self.workspace = workspace
        self._running_tasks: dict = {}

    async def spawn(self, **_kwargs):
        telemetry_dir = bridge.STATE_DIR / "subagents"
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        (telemetry_dir / f"{self.task_id}.json").write_text(json.dumps({
            "task_id": self.task_id,
            "status": "error",
            "summary": TRANSPORT_ERROR,
            "result": TRANSPORT_ERROR,
            "started_at": "2026-09-04T04:01:31Z",
            "finished_at": "2026-09-04T04:04:45Z",
        }), encoding="utf-8")

        async def _done():
            return None

        # bridge captures next(iter(mgr._running_tasks)) right after spawn.
        self._running_tasks[self.task_id] = asyncio.ensure_future(_done())
        return "fake subagent spawned (LLM dead)"


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


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


def _result_for(state_dir, request_id):
    matches = list((state_dir / "subagents").rglob(f"result-{request_id}.json"))
    assert len(matches) == 1, matches
    return json.loads(matches[0].read_text(encoding="utf-8"))


class TestExecutorLLMErrorIsAFailure:
    def test_dead_llm_cycle_is_blocked_failed_unmarked_and_exits_nonzero(self, tmp_path, monkeypatch):
        state_dir = _wire(tmp_path, monkeypatch, _LLMDeadSubagentManager)
        # Production requests point at a proposal artifact; the result row's
        # backlog_title (what _recent_failure_match matches on) comes from its
        # next_bounded_candidate.title — seed one exactly like the proposer does.
        title = "Add markdown catalog link path resolver to workspace_validation_helpers.py"
        artifact = tmp_path / "improvements" / "llm-proposed-cycle-dead.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
        _seed_bridge_request(
            state_dir, "req-dead", "cycle-dead", task_title=title, source_artifact=str(artifact),
        )

        rc = asyncio.run(bridge._main_impl())

        # (1) recorded status: `blocked` with the reason set — both tests
        # _recent_failure_match applies. But while the request still has
        # retry budget the row must NOT feed suppression, or the #716 branch
        # retires the request on its next offer and the retry never runs
        # (review finding on PR #1282). Once the budget is spent it counts.
        res = _result_for(state_dir, "req-dead")
        assert res["result_status"] == "blocked"
        assert res["rollback"]["reason"] == "executor_llm_error"
        assert res["commits_pushed"] == 0
        assert res["backlog_title"] == title
        assert any("EXECUTOR LLM ERROR (#1280)" in s and "Connection error" in s for s in res.get("key_learnings") or [])
        assert bridge._recent_failure_match(title, state_dir) is None
        assert bridge._llm_error_retries_exhausted("req-dead") is False
        # Same row, budget spent: the failure proxy sees it like any other.
        retry_path = state_dir / "subagent_bridge" / "retry_req-dead.json"
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
        assert not (bridge_state / "handled_req-dead.txt").exists()
        retry = json.loads((bridge_state / "retry_req-dead.json").read_text(encoding="utf-8"))
        assert retry["count"] == 1 and retry["max"] == bridge.LLM_ERROR_MAX_RETRIES

    def test_retry_is_bounded_then_the_request_is_retired(self, tmp_path, monkeypatch, capsys):
        """Production-shaped request: a source_artifact whose
        next_bounded_candidate.title becomes the result row's backlog_title.
        The first version of this test seeded no artifact, so backlog_title was
        empty, the #716 suppression had nothing to match, and the retry passed
        for the wrong reason; with the artifact the review reproduced the
        request being retired on attempt 2 of 3 by the suppression branch."""
        state_dir = _wire(tmp_path, monkeypatch, _LLMDeadSubagentManager)
        monkeypatch.setattr(bridge, "LLM_ERROR_MAX_RETRIES", 3)
        title = "Add markdown catalog link path resolver to workspace_validation_helpers.py"
        artifact = tmp_path / "improvements" / "llm-proposed-cycle-loop.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({"next_bounded_candidate": {"title": title}}), encoding="utf-8")
        _seed_bridge_request(
            state_dir, "req-loop", "cycle-loop", task_title=title, source_artifact=str(artifact),
        )
        bridge_state = state_dir / "subagent_bridge"

        for attempt in (1, 2):
            rc = asyncio.run(bridge._main_impl())
            out = capsys.readouterr().out
            assert rc == bridge.EXIT_EXECUTOR_LLM_ERROR, out
            assert "matches recent failure/rejection" not in out, out  # suppression must not fire yet
            assert f"request left pending for retry ({attempt}/3)" in out
            assert not (bridge_state / "handled_req-loop.txt").exists()
            assert json.loads((bridge_state / "retry_req-loop.json").read_text())["count"] == attempt
            assert _result_for(state_dir, "req-loop")["backlog_title"] == title
            assert bridge._recent_failure_match(title, state_dir) is None

        rc = asyncio.run(bridge._main_impl())
        out = capsys.readouterr().out
        assert rc == bridge.EXIT_EXECUTOR_LLM_ERROR, out
        assert "request retired after 3 failed LLM attempts" in out
        assert (bridge_state / "handled_req-loop.txt").exists(), "third failure must retire the request"
        assert json.loads((bridge_state / "retry_req-loop.json").read_text())["count"] == 3
        # Budget spent: the title now counts for the 24 h suppression window,
        # so a re-minted proposal with the same title is held back.
        assert bridge._llm_error_retries_exhausted("req-loop") is True
        assert bridge._recent_failure_match(title, state_dir) == title

        # Fourth run: the request is already handled — no fourth spawn.
        rc = asyncio.run(bridge._main_impl())
        out = capsys.readouterr().out
        assert rc == 0
        assert "already_handled" in out
        assert json.loads((bridge_state / "retry_req-loop.json").read_text())["count"] == 3

    def test_healthy_cycle_is_unchanged(self, tmp_path, monkeypatch):
        """Control: the pre-#1280 path — a subagent that commits real work —
        still records completed, writes the marker, and exits 0."""
        state_dir = _wire(tmp_path, monkeypatch, _FakeSubagentManager)
        _seed_bridge_request(state_dir, "req-ok", "cycle-ok", task_title="add feature")

        rc = asyncio.run(bridge._main_impl())

        assert rc == 0
        res = _result_for(state_dir, "req-ok")
        assert res["result_status"] == "completed"
        assert res["rollback"]["reason"] in ("", None)
        assert (state_dir / "subagent_bridge" / "handled_req-ok.txt").exists()
        assert not (state_dir / "subagent_bridge" / "retry_req-ok.json").exists()
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
