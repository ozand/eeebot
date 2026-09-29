"""#2050 R2.1 hotfix: H1 (an interrupted open increment deadlocks with the
#716 recent-failure gate) and H2 (duplicate-rejection evidence is lost when
the one session it is handed to times out).

H1 drives real ``bridge._main_impl`` cycles with the REAL planning session
(``_run_planning_session``): only the SubagentManager is a fake, returning
the planner's final JSON and the executor's telemetry the way the real
manager writes them. ``resume_cycle_id`` is never set by the test -- it is
produced by ``_run_planning_session`` from the pending open-increment record
on a ``keep`` decision (bridge.py, "if _oi_decision == 'keep'" block) and
routed into the request by ``_main_impl_body`` (``_planning_result.get(
'resume_cycle_id')``).

H2 drives the real ``_run_planning_session`` consumer; the evidence is
written by the real ``planner_dedup_evidence.record_rejected_duplicate``.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from nanobot.runtime import bridge, open_increment, planner_dedup_evidence
from nanobot.runtime.commit_markers import CHECKPOINT_TRAILER
from nanobot.runtime.cycle_ledger import read_events
from tests.test_agent_chooses import _setup_planner_chooses_harness
from tests.test_cycle_ledger import _init_selfevo_repo

TITLE = "Add retry accounting to the nightly archive rotation script"


def _write_task_writing_contract() -> None:
    skill = bridge.RELEASE_ROOT / "nanobot" / "skills" / "task-writing" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("task-writing contract (test stub)\n", encoding="utf-8")


def _write_telemetry(state_dir: Path, task_id: str, payload: dict) -> None:
    (state_dir / "subagents").mkdir(parents=True, exist_ok=True)
    (state_dir / "subagents" / f"{task_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _manager(state_dir: Path, planner_answers: list[dict], executor_behaviours: list[str], log: dict):
    """A SubagentManager fake. Each planner spawn returns the next planner
    answer; each executor spawn plays the next behaviour:

    - ``checkpoint_then_error``: commits a real checkpoint to the cycle
      branch, then ends with its own telemetry ``status: error`` (our own
      defect) -- the bridge records ``interrupted_defect``;
    - ``llm_error``: the executor's model call is rejected (our own
      defect), nothing committed -- the bridge records executor_llm_error;
    - ``finish_without_commit``: ends ``status: ok`` with no commit.
    """

    class _Manager:
        def __init__(self, *, workspace, telemetry_component: str = "", **_kwargs):
            self.workspace = workspace
            self._telemetry_component = telemetry_component
            self._running_tasks: dict = {}

        async def spawn(self, **kwargs):
            if self._telemetry_component == "planner":
                index = len(log["planner"])
                log["planner"].append(kwargs.get("task", ""))
                task_id = f"planner-{index}"
                _write_telemetry(state_dir, task_id, {
                    "status": "ok", "result": json.dumps(planner_answers[index]),
                    "context_usage": {"iterations": [{}]},
                })
            else:
                index = len(log["executor"])
                log["executor"].append(kwargs.get("task", ""))
                behaviour = executor_behaviours[index]
                task_id = f"executor-{index}"
                if behaviour == "checkpoint_then_error":
                    (self.workspace / "scripts").mkdir(exist_ok=True)
                    (self.workspace / "scripts" / "partial.py").write_text("def partial():\n    return 1\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(self.workspace), "add", "scripts/partial.py"], check=True, capture_output=True)
                    subprocess.run(
                        ["git", "-C", str(self.workspace), "commit", "-m", "selfevo: checkpoint — scripts/partial.py",
                         "-m", CHECKPOINT_TRAILER],
                        check=True, capture_output=True,
                    )
                    _write_telemetry(state_dir, task_id, {
                        "status": "error",
                        "summary": "Error: unexpected exception in tool execution",
                        "result": "Error: unexpected exception in tool execution",
                    })
                elif behaviour == "llm_error":
                    # our own request rejected by the provider, nothing committed
                    text = "Error: LLM execution failed: litellm.BadRequestError: invalid temperature"
                    _write_telemetry(state_dir, task_id, {"status": "error", "summary": text, "result": text})
                else:
                    _write_telemetry(state_dir, task_id, {"status": "ok", "summary": "done", "result": "done"})

            async def _done():
                return None

            self._running_tasks[task_id] = asyncio.create_task(_done())
            return "fake spawned"

    return _Manager


def _outcomes(state_dir: Path) -> list[dict]:
    return [row for row in read_events(state_dir) if row.get("phase") == "outcome"]


def _harness(tmp_path: Path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    state_dir = _setup_planner_chooses_harness(base, monkeypatch)
    _init_selfevo_repo(base)
    _write_task_writing_contract()
    return state_dir


# --- H1 -------------------------------------------------------------------------

@pytest.mark.parametrize("keep_answer", [
    {"open_increment_decision": "keep"},  # confirm: the previous plan, reused
    {"open_increment_decision": "keep", "plan_action": "edit",
     "plan": TITLE + ", with the retry counter reset on success"},
], ids=["keep", "keep_edit"])
def test_h1_keep_of_an_interrupted_increment_starts_the_executor(tmp_path: Path, monkeypatch, keep_answer):
    """The R2 deadlock, reproduced: cycle 1's executor is interrupted by our
    own defect (interrupted_defect, pending open increment); cycle 2's
    planner must resolve it and keeps it -- the executor must START, not be
    suppressed as a recent failure of itself."""
    state_dir = _harness(tmp_path, monkeypatch)
    log = {"planner": [], "executor": []}
    monkeypatch.setattr(bridge, "SubagentManager", _manager(
        state_dir,
        planner_answers=[{"insight": "archive rotation retries are uncounted", "plan": TITLE, "iterations_planned": 1},
                         keep_answer],
        executor_behaviours=["checkpoint_then_error", "finish_without_commit"],
        log=log,
    ))

    assert asyncio.run(bridge._main_impl()) == 0
    pending = open_increment.pending_open_increment(state_dir)
    assert pending is not None and pending["reason"] == "interrupted_defect"
    interrupted_cycle = pending["cycle_id"]
    assert _outcomes(state_dir)[-1]["reason"] == "interrupted_defect"

    assert asyncio.run(bridge._main_impl()) == 0
    second = [row for row in _outcomes(state_dir) if row.get("cycle_id") == interrupted_cycle][-1]
    assert second.get("reason") != "recent_duplicate_failure", second
    assert len(log["executor"]) == 2, "the kept open increment's executor must start"
    assert not any(
        row.get("phase") == "dedup" and row.get("decision") == "skipped_recent_failure"
        for row in read_events(state_dir)
    )


def test_h1_a_genuinely_failed_continuation_is_suppressed_and_the_planner_is_told(tmp_path: Path, monkeypatch):
    """Architect review of #2051: the gate must NOT be bypassed for a
    continuation. record_attempt_finished never clears `pending`, so a
    continuation that genuinely FAILS (here gate_failed: smoke red, no
    repair budget) is still pending -- a second `keep` must be suppressed,
    recorded as rejected_duplicate, and that evidence must wait for the
    next planning session (H2), which then sees it. Without the gate the
    same failing increment would be re-run by the executor every cycle."""
    state_dir = _harness(tmp_path, monkeypatch)
    monkeypatch.setenv("SUBAGENT_BRIDGE_MAX_REVISIONS", "0")  # no repair turn
    monkeypatch.setattr(bridge, "_run_smoke_tests_with_shrink_guard", lambda *a, **k: (False, "pytest failed"))
    log = {"planner": [], "executor": []}
    monkeypatch.setattr(bridge, "SubagentManager", _manager(
        state_dir,
        planner_answers=[
            {"insight": "archive rotation retries are uncounted", "plan": TITLE, "iterations_planned": 1},
            {"open_increment_decision": "keep"},
            {"open_increment_decision": "keep"},
            "not a plan",  # the next session: sees the evidence, does not plan
        ],
        executor_behaviours=["checkpoint_then_error", "finish_without_commit", "finish_without_commit"],
        log=log,
    ))

    asyncio.run(bridge._main_impl())  # interrupted -> pending
    pending = open_increment.pending_open_increment(state_dir)
    assert pending is not None and pending["reason"] == "interrupted_defect"

    asyncio.run(bridge._main_impl())  # keep -> the continuation runs and genuinely fails
    assert len(log["executor"]) == 2, "a keep after an interruption must start the executor"
    assert _outcomes(state_dir)[-1]["reason"] == "gate_failed", _outcomes(state_dir)[-1]
    assert open_increment.pending_open_increment(state_dir) is not None, "a failed continuation stays pending"

    asyncio.run(bridge._main_impl())  # keep again -> must be suppressed
    assert len(log["executor"]) == 2, "a genuinely failed continuation must not be re-run by keep"
    assert _outcomes(state_dir)[-1]["reason"] == "recent_duplicate_failure"
    evidence = planner_dedup_evidence._state_path(state_dir)
    assert evidence.is_file(), "the refusal is recorded for the next planning session"

    asyncio.run(bridge._main_impl())  # the next session sees it (and, not planning, keeps it)
    assert "Last increment rejected as a duplicate" in log["planner"][3]
    assert evidence.is_file()


def test_h1_an_interrupted_attempt_is_not_recent_failure_history(tmp_path: Path, monkeypatch):
    """General rule (decided in _INTERRUPTED_ROLLBACK_REASONS): the result
    row the bridge writes for an interrupted attempt never counts as a
    recent failure -- also for a fresh plan after edit/delete."""
    state_dir = _harness(tmp_path, monkeypatch)
    log = {"planner": [], "executor": []}
    monkeypatch.setattr(bridge, "SubagentManager", _manager(
        state_dir,
        planner_answers=[{"insight": "archive rotation retries are uncounted", "plan": TITLE, "iterations_planned": 1}],
        executor_behaviours=["checkpoint_then_error"],
        log=log,
    ))
    assert asyncio.run(bridge._main_impl()) == 0
    assert _outcomes(state_dir)[-1]["reason"] == "interrupted_defect"
    assert bridge._recent_failure_match(TITLE, state_dir) is None


def test_h1_a_genuine_recent_failure_is_still_suppressed(tmp_path: Path, monkeypatch):
    """No regression of #716: an attempt that FAILED (executor_llm_error,
    retry budget spent), with NO pending open increment, still suppresses
    the same plan in the next cycle."""
    state_dir = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "LLM_ERROR_MAX_RETRIES", 1)  # retire after one attempt
    log = {"planner": [], "executor": []}
    fresh = {"insight": "archive rotation retries are uncounted", "plan": TITLE, "iterations_planned": 1}
    monkeypatch.setattr(bridge, "SubagentManager", _manager(
        state_dir, planner_answers=[fresh, fresh],
        executor_behaviours=["llm_error", "llm_error"], log=log,
    ))
    asyncio.run(bridge._main_impl())
    assert open_increment.pending_open_increment(state_dir) is None
    assert _outcomes(state_dir)[-1]["reason"] == "executor_llm_error", _outcomes(state_dir)[-1]

    asyncio.run(bridge._main_impl())
    assert len(log["executor"]) == 1, "the second, identical plan must be suppressed"
    assert _outcomes(state_dir)[-1]["reason"] == "recent_duplicate_failure"


# --- H2 -------------------------------------------------------------------------

from tests.test_run_planning_session import (  # noqa: E402
    _init_repo_with_origin,
    _make_fake_mgr_factory,
    _run,
    _stub_role_prompt,  # noqa: F401  (autouse fixture, same as the planning-session tests)
)

_EVIDENCE_LINE = "Last increment rejected as a duplicate"


def _evidence_file(state: Path) -> Path:
    """The file record_rejected_duplicate writes (the module's own path)."""
    return planner_dedup_evidence._state_path(state)


def _timeout(awaitable, *, timeout):
    async def _raise():
        awaitable.cancel()
        try:
            await awaitable
        except asyncio.CancelledError:
            pass
        raise asyncio.TimeoutError

    return _raise()


def _release_with_contract(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "release"
    skill = release / "nanobot" / "skills" / "task-writing" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# contract\n", encoding="utf-8")
    (release / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", release)


def test_h2_evidence_survives_timed_out_and_malformed_sessions(tmp_path: Path, monkeypatch):
    repo = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _release_with_contract(tmp_path, monkeypatch)
    planner_dedup_evidence.record_rejected_duplicate(
        state, "cycle-dup", "Wire the archive rotation retry counter", "abc1234", "existence_index_duplicate",
    )

    # session 1: times out
    mgr = _make_fake_mgr_factory(state, {"plan": "x"})
    monkeypatch.setattr(bridge, "SubagentManager", mgr)
    with monkeypatch.context() as m:
        m.setattr(asyncio, "wait_for", _timeout)
        first = asyncio.run(_run(state_dir=state, selfevo_repo=repo, denied_paths=set(), cycle_id="cycle-t"))
    assert first["ran"] is False
    assert _EVIDENCE_LINE in (mgr.last_task or "")
    assert _evidence_file(state).is_file(), "a timed-out session must not use it up"

    # session 2: malformed final answer
    mgr = _make_fake_mgr_factory(state, "not json at all")
    monkeypatch.setattr(bridge, "SubagentManager", mgr)
    asyncio.run(_run(state_dir=state, selfevo_repo=repo, denied_paths=set(), cycle_id="cycle-m"))
    assert _EVIDENCE_LINE in (mgr.last_task or "")
    assert _evidence_file(state).is_file(), "a malformed session must not use it up"

    # session 3: produces a plan -- consumes it
    plan = {"insight": "the retry counter already exists", "plan": "count archive rotation skips instead",
            "iterations_planned": 5}
    mgr = _make_fake_mgr_factory(state, plan)
    monkeypatch.setattr(bridge, "SubagentManager", mgr)
    third = asyncio.run(_run(state_dir=state, selfevo_repo=repo, denied_paths=set(), cycle_id="cycle-p"))
    assert third["plan"] == plan
    assert _EVIDENCE_LINE in (mgr.last_task or "")
    assert not _evidence_file(state).is_file(), "a session that planned consumes it"
