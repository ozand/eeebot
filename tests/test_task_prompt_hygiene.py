from nanobot.runtime.bridge import build_task


def test_iteration_and_skip_contract_are_explicit():
    req = {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"}
    prompt = build_task(req, "derived", "", max_iterations=23)
    assert "23 tool iterations" in prompt
    assert 'outcome: "skipped"' in prompt
    assert "pick next priority from memory/MEMORY.md" not in prompt
    assert "bookkeeping-only commits" in prompt


def test_system_mission_pointer_does_not_duplicate_charter():
    req = {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"}
    prompt = build_task(req, "derived priorities", "", charter_in_system=True)
    assert prompt.count("CHARTER_SENTINEL") == 0
    assert "see system context" in prompt
