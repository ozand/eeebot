from nanobot.runtime.bridge import build_task


def test_iteration_and_skip_contract_are_explicit():
    req = {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"}
    prompt = build_task(req, "derived", "", max_iterations=23)
    assert "23 tool iterations" in prompt
    assert 'outcome: "skipped"' in prompt
    assert "pick next priority from memory/MEMORY.md" not in prompt
    assert "bookkeeping-only commits" in prompt
    assert "python3 -m pytest <affected test file>" in prompt
    assert "pytest is installed; run the tests you touch" in prompt
    assert "pytest is not installed" not in prompt


def test_task_prompt_uses_import_fallback_when_pytest_is_absent(monkeypatch):
    monkeypatch.setattr(
        "nanobot.runtime.bridge.importlib.util.find_spec",
        lambda name: None if name == "pytest" else object(),
    )
    req = {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"}
    prompt = build_task(req, "derived", "")
    assert "pytest is not installed — use python3 -c imports as smoke tests" in prompt
    assert "python3 -m pytest <affected test file>" not in prompt


def test_system_mission_pointer_does_not_duplicate_charter():
    req = {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"}
    prompt = build_task(req, "derived priorities", "", charter_in_system=True)
    assert prompt.count("CHARTER_SENTINEL") == 0
    assert "see system context" in prompt
