import json

from nanobot.runtime.bridge import build_task


def _write_artifact(tmp_path, *, target_path="scripts/thing.py", curriculum_level=None, name="artifact.json"):
    nbc = {
        "title": "Add test scaffolding",
        "backlog_instructions": f"Do the thing.\n\nTarget path: {target_path}",
    }
    if curriculum_level is not None:
        nbc["curriculum_level"] = curriculum_level
    payload = {
        "next_bounded_candidate": nbc,
        "recommended_next_action": f"Implement and commit: Add test scaffolding (target: {target_path})",
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _base_req(tmp_path, *, target_path="scripts/thing.py", semantic_task_id="llm-proposed-improvement", **overrides):
    req = {
        "task_title": "Add test scaffolding",
        "request_id": "r1",
        "cycle_id": "c1",
        "goal_id": "g1",
        "semantic_task_id": semantic_task_id,
        "task": f"Do the thing.\n\nTarget path: {target_path}",
        "source_artifact": _write_artifact(tmp_path, target_path=target_path),
    }
    req.update(overrides)
    return req


# ---------------------------------------------------------------------------
# #1727: six build_task prompt-hygiene defects
# ---------------------------------------------------------------------------


def test_1727_single_concrete_task_heading_both_branches_present(tmp_path):
    """AC: a fixture with both next_bounded_candidate and
    recommended_next_action produces exactly one heading."""
    req = _base_req(tmp_path)
    prompt = build_task(req, "derived", "")
    assert prompt.count("## Concrete task to implement") == 1


def test_1727_previous_attempts_omitted_when_no_match(tmp_path):
    """AC: three results files for other tasks and none for this one
    produce no '## Previous attempts' section."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    results_dir.mkdir(parents=True)
    for i in range(3):
        (results_dir / f"r{i}.json").write_text(json.dumps({
            "materialized_from": "bridge_llm_execution",
            "semantic_task_id": f"other-task-{i}",
            "target_path": f"scripts/other_{i}.py",
            "commits_pushed": 1,
            "key_learnings": [f"Committed 1 change(s) to: scripts/other_{i}.py."],
        }), encoding="utf-8")

    req = _base_req(tmp_path, semantic_task_id="this-task", target_path="scripts/this_task.py")
    prompt = build_task(req, "derived", "", state_dir=state_dir)
    assert "## Previous attempts for this task" not in prompt


def test_1727_previous_attempts_one_matching_no_reward_sentence(tmp_path):
    """AC: a fixture with one matching result produces one attempt line
    without the reward-signal sentence."""
    state_dir = tmp_path / "state"
    results_dir = state_dir / "subagents" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "r0.json").write_text(json.dumps({
        "materialized_from": "bridge_llm_execution",
        "target_path": "scripts/this_task.py",
        "commits_pushed": 1,
        "key_learnings": ["Committed 1 change(s) to: scripts/this_task.py."],
        "created_at": "2026-09-01T00:00:00",
    }), encoding="utf-8")

    req = _base_req(tmp_path, target_path="scripts/this_task.py")
    prompt = build_task(req, "derived", "", state_dir=state_dir)
    assert "## Previous attempts for this task" in prompt
    assert prompt.count("- Attempt ") == 1
    assert "Reward signal" not in prompt


def test_1727_source_artifact_one_line_no_json_title_bounded(tmp_path):
    """AC: '## Source artifact' contains a path line and no fenced JSON;
    the task title appears at most twice (Task line and Concrete task)."""
    req = _base_req(tmp_path)
    prompt = build_task(req, "derived", "")
    assert "Source artifact: " in prompt
    assert "```json" not in prompt
    assert "## Source artifact" not in prompt
    assert prompt.count("Add test scaffolding") <= 2


def test_1727_goal_id_absent(tmp_path):
    """AC: 'Goal ID' is absent (this PR's decision — see PR body: it
    cannot carry the real per-task demand id without new plumbing)."""
    req = _base_req(tmp_path)
    prompt = build_task(req, "derived", "")
    assert "Goal ID" not in prompt


def test_1727_system_mission_one_line_non_priority_task(tmp_path):
    """AC: '## System mission' is one line for a non-priority task; no
    full priority text appears."""
    req = _base_req(tmp_path)
    prompt = build_task(req, "derived", "")
    assert "This task is not an operator priority; priorities are handled by the proposer." in prompt


def test_1727_system_mission_one_line_priority_task(tmp_path):
    """AC: '## System mission' names P<n> for a priority task; no full
    priority text appears."""
    req = _base_req(
        tmp_path,
        source_artifact=_write_artifact(tmp_path, curriculum_level=17, name="artifact-priority.json"),
    )
    prompt = build_task(req, "derived", "")
    assert "This task is operator priority P17." in prompt


def test_1727_repair_turn_also_produces_single_concrete_task_heading(tmp_path):
    """Constraint: the repair turn (bridge.py ~L3488) calls build_task with
    the same req — must also produce the single-section form."""
    req = _base_req(tmp_path)
    prompt = build_task(req, "derived", "", repair_context="FAILED tests/test_x.py - AssertionError")
    assert prompt.count("## Concrete task to implement") == 1
    assert "## Repair context" in prompt


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
