"""Coordinator-level enforcement test for R11 lane switching (#558).

Imports the coordinator, so it runs under the CI matrix (3.11–3.13).
"""
from nanobot.runtime.coordinator import _switch_off_stalled_lane


def _plan():
    # run-bounded-turn / record-reward are real known task_ids (KNOWN_TASK_IDS);
    # placeholder ids like "t1"/"t2" are now rejected as orphaned by
    # _task_is_selectable (#580 follow-up), so fixtures must use real ids.
    return {
        "current_task_id": "run-bounded-turn",
        "tasks": [
            {"task_id": "run-bounded-turn", "title": "Stalled lane"},
            {"task_id": "record-reward", "title": "Fresh lane"},
        ],
    }


def test_switch_off_stalled_lane_repoints_to_alternative():
    decision = {"selected_task_id": "run-bounded-turn", "selected_task_label": "Stalled lane [task_id=run-bounded-turn]"}
    prev = {"stall": {"stop": True}}
    out = _switch_off_stalled_lane(decision, _plan(), prev)
    assert out["selected_task_id"] == "record-reward"
    assert out["mode"] == "switch_stalled_lane"
    assert out["selection_source"] == "switch_stalled_lane"
    assert "record-reward" in out["selected_task_label"]


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


def test_decision_already_off_stalled_lane_is_returned_unchanged():
    # #586: the stalled lane is the PREVIOUS experiment's lane
    # (record-reward), not whatever the incoming decision selects. A decision
    # that already selects a different task is the escape from the stall —
    # overriding it re-traps the coordinator.
    decision = {
        "mode": "synthesize_next_candidate",
        "selected_task_id": "synthesize-next-improvement-candidate",
    }
    prev = {"current_task_id": "record-reward", "stall": {"stop": True}}
    out = _switch_off_stalled_lane(decision, _plan(), prev)
    assert out is decision
    assert out["selected_task_id"] == "synthesize-next-improvement-candidate"
    assert out["mode"] == "synthesize_next_candidate"


def test_decision_still_on_stalled_lane_is_overridden():
    # Decision selects the same lane the previous experiment stalled on ->
    # existing override behavior (alt selection, backlog-progression
    # preference, _task_is_selectable filtering) still applies.
    decision = {"selected_task_id": "record-reward"}
    plan = {
        "current_task_id": "record-reward",
        "tasks": [
            {"task_id": "record-reward", "title": "Stalled lane"},
            {"task_id": "refresh-approval-gate", "title": "Bookkeeping"},
            {"task_id": "materialize-synthesized-improvement", "title": "Real dispatch"},
        ],
    }
    prev = {"current_task_id": "record-reward", "stall": {"stop": True}}
    out = _switch_off_stalled_lane(decision, plan, prev)
    assert out["selected_task_id"] == "materialize-synthesized-improvement"
    assert out["selected_task_id"] != "record-reward"
    assert out["mode"] == "switch_stalled_lane"


def test_decision_none_with_stalled_prev_lane_still_switches():
    # Regression for the fallback path: no incoming decision at all.
    prev = {"current_task_id": "run-bounded-turn", "stall": {"stop": True}}
    out = _switch_off_stalled_lane(None, _plan(), prev)
    assert out is not None
    assert out["selected_task_id"] == "record-reward"
    assert out["mode"] == "switch_stalled_lane"


def test_live_replay_record_reward_stall_synthesize_next_candidate_unchanged():
    # Live replay (2026-07-04 host incident, #586): previous_experiment lane
    # is record-reward and stalled; the state machine already produced a
    # forward move to synthesize_next_candidate. That decision must survive
    # untouched so synthesis actually runs instead of bouncing back to
    # refresh-approval-gate.
    decision = {
        "mode": "synthesize_next_candidate",
        "selected_task_id": "synthesize-next-improvement-candidate",
    }
    prev = {"current_task_id": "record-reward", "stall": {"stop": True}}
    plan = {
        "current_task_id": "record-reward",
        "tasks": [
            {"task_id": "materialize-synthesized-improvement", "status": "done"},
            {"task_id": "synthesize-next-improvement-candidate", "status": "done"},
            {"task_id": "inspect-pass-streak", "status": "done"},
            {"task_id": "refresh-approval-gate", "status": "pending"},
            {"task_id": "run-bounded-turn", "status": "pending"},
            {"task_id": "record-reward", "status": "active"},
            {"task_id": "subagent-verify-materialized-improvement", "status": "pending"},
            {"task_id": "exploit-successful-improvement-path", "status": "pending"},
        ],
    }
    out = _switch_off_stalled_lane(decision, plan, prev)
    assert out["selected_task_id"] == "synthesize-next-improvement-candidate"
    assert out["mode"] == "synthesize_next_candidate"
