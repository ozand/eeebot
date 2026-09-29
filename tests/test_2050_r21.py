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


def test_h1_continuation_is_not_suppressed_by_another_recent_failure(tmp_path: Path, monkeypatch):
    """The gate exemption itself (not the interrupted-row rule): cycle 0's
    plan genuinely FAILS (executor_llm_error, budget spent). Cycle 1 plans an
    unrelated increment that is interrupted (pending). Cycle 2 keeps it and
    EDITS the plan so its title now overlaps cycle 0's failure -- still the
    pending increment's continuation, so the executor must start."""
    state_dir = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "LLM_ERROR_MAX_RETRIES", 1)
    failed_title = "Rotate archive retry counters nightly"
    log = {"planner": [], "executor": []}
    monkeypatch.setattr(bridge, "SubagentManager", _manager(
        state_dir,
        planner_answers=[
            {"insight": "retry counters grow unbounded", "plan": failed_title, "iterations_planned": 1},
            {"insight": "the backup schedule is undocumented", "plan": "Document the backup schedule format",
             "iterations_planned": 1},
            {"open_increment_decision": "keep", "plan_action": "edit",
             "plan": "Rotate archive retry counters while documenting the backup schedule"},
        ],
        executor_behaviours=["llm_error", "checkpoint_then_error", "finish_without_commit"],
        log=log,
    ))
    asyncio.run(bridge._main_impl())
    assert _outcomes(state_dir)[-1]["reason"] == "executor_llm_error"
    asyncio.run(bridge._main_impl())
    pending = open_increment.pending_open_increment(state_dir)
    assert pending is not None and pending["reason"] == "interrupted_defect"
    # precondition: the edited title really matches cycle 0's genuine failure
    assert bridge._recent_failure_match(
        "Rotate archive retry counters while documenting the backup schedule", state_dir,
    ) == failed_title

    asyncio.run(bridge._main_impl())
    last = [row for row in _outcomes(state_dir) if row.get("cycle_id") == pending["cycle_id"]][-1]
    assert last.get("reason") != "recent_duplicate_failure", last
    assert len(log["executor"]) == 3, "the kept, edited open increment's executor must start"


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
