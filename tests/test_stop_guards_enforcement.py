"""Coordinator-level enforcement test for R11 lane switching (#558).

Imports the coordinator, so it runs under the CI matrix (3.11–3.13).
"""
from nanobot.runtime.coordinator import _switch_off_stalled_lane


def _plan():
    return {
        "current_task_id": "t1",
        "tasks": [
            {"task_id": "t1", "title": "Stalled lane"},
            {"task_id": "t2", "title": "Fresh lane"},
        ],
    }


def test_switch_off_stalled_lane_repoints_to_alternative():
    decision = {"selected_task_id": "t1", "selected_task_label": "Stalled lane [task_id=t1]"}
    prev = {"stall": {"stop": True}}
    out = _switch_off_stalled_lane(decision, _plan(), prev)
    assert out["selected_task_id"] == "t2"
    assert out["mode"] == "switch_stalled_lane"
    assert out["selection_source"] == "switch_stalled_lane"
    assert "t2" in out["selected_task_label"]


def test_no_switch_when_not_stalled():
    decision = {"selected_task_id": "t1"}
    prev = {"stall": {"stop": False}}
    assert _switch_off_stalled_lane(decision, _plan(), prev) is decision


def test_no_switch_when_no_alternative_task():
    decision = {"selected_task_id": "t1"}
    plan = {"current_task_id": "t1", "tasks": [{"task_id": "t1", "title": "only"}]}
    prev = {"stall": {"stop": True}}
    # Unchanged decision object returned — stall stays recorded, nothing to switch to.
    assert _switch_off_stalled_lane(decision, plan, prev) is decision


def test_no_switch_without_previous_experiment():
    decision = {"selected_task_id": "t1"}
    assert _switch_off_stalled_lane(decision, _plan(), None) is decision


def test_switch_prefers_backlog_progression_over_bookkeeping():
    # #568: on stall, prefer real dispatch progress over bookkeeping lanes, even
    # when the bookkeeping task appears earlier in the candidate list.
    decision = {"selected_task_id": "t1"}
    plan = {
        "current_task_id": "t1",
        "tasks": [
            {"task_id": "t1", "title": "Stalled lane"},
            {"task_id": "refresh-approval-gate", "title": "Bookkeeping"},
            {"task_id": "materialize-synthesized-improvement", "title": "Real dispatch"},
        ],
    }
    prev = {"stall": {"stop": True}}
    out = _switch_off_stalled_lane(decision, plan, prev)
    assert out["selected_task_id"] == "materialize-synthesized-improvement"


def test_switch_unchanged_behavior_when_only_bookkeeping_present():
    # regression guard: bookkeeping-only candidate list behaves as before (first distinct task).
    decision = {"selected_task_id": "t1"}
    plan = {
        "current_task_id": "t1",
        "tasks": [
            {"task_id": "t1", "title": "Stalled lane"},
            {"task_id": "refresh-approval-gate", "title": "Bookkeeping A"},
            {"task_id": "record-reward", "title": "Bookkeeping B"},
        ],
    }
    prev = {"stall": {"stop": True}}
    out = _switch_off_stalled_lane(decision, plan, prev)
    assert out["selected_task_id"] == "refresh-approval-gate"
