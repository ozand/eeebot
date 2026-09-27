"""Failing-first regression tests for ADR-034 review findings F3-F8 (#1984)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nanobot.runtime import bridge, goal_review, health, llm_proposer, strategist_inputs
from nanobot.runtime.operator_documents import resolve_operator_priorities


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    return state


def _operator_text(state: Path, text: str, goal_id: str = "goal-test") -> None:
    path = state / "goals" / "goal_text.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "goal-text-v1", "goal_id": goal_id, "text": text}), encoding="utf-8")


def _priority_text(number: int = 7, title: str = "Operator priority", body: str = "Do the private task.") -> str:
    return f"Current priority targets:\n(A) Priority {number} — {title}: {body}\n"


def test_f3_unreadable_operator_priorities_do_not_stop_bridge(tmp_path, monkeypatch, capsys):
    state = _state(tmp_path)
    bridge_root = state / "subagent_bridge"
    (state / "subagents" / "requests").mkdir(parents=True)
    (state / "subagents" / "requests" / "pending.json").write_text(json.dumps({"request_status": "queued"}), encoding="utf-8")
    class ReachedRequestLookup(Exception): pass
    monkeypatch.setattr(bridge, "find_pending_request", lambda: (_ for _ in ()).throw(ReachedRequestLookup()))
    class FakeManager:
        def __init__(self, *args, **kwargs): pass
        def __getattr__(self, _name): return lambda *args, **kwargs: None
        async def spawn(self, **kwargs):
            from nanobot.agent.subagent import SubagentResult
            return SubagentResult(success=True, output="done")
    monkeypatch.setattr(bridge, "SubagentManager", FakeManager)
    monkeypatch.setattr(bridge, "STATE_DIR", state)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", bridge_root)
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "workspace")
    release = tmp_path / "release"
    release.mkdir()
    (release / "goals.md").write_text("A valid immutable charter.", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", release)
    goals = state / "goals"
    goals.mkdir()
    (goals / "goal_text.json").write_text("{malformed", encoding="utf-8")

    try:
        asyncio.run(bridge._main_impl())
    except ReachedRequestLookup:
        pass
    else:
        raise AssertionError("unreadable priority metadata blocked before request lookup")


def test_f4_unreadable_derived_state_is_not_depth_zero_or_none(tmp_path):
    state = _state(tmp_path)
    derived = state / "goals" / "derived_priorities.json"
    derived.parent.mkdir(parents=True)
    derived.write_text("{malformed", encoding="utf-8")
    assert health.read_derived_priorities_queue(state)["status"] == "unavailable"

    context = llm_proposer.build_context(state, None)
    assert "derived priorities" in context.lower()
    assert "unavailable; resolver state: unreadable" in context.lower()


def test_f4_derived_view_carries_operator_priority_status_without_text(tmp_path):
    state = _state(tmp_path)
    _operator_text(state, _priority_text(title="SYNTHETIC_PRIVATE_TITLE", body="SYNTHETIC_PRIVATE_BODY"))
    view = __import__("nanobot.runtime.demand", fromlist=["build_derived_view"]).build_derived_view(state)
    assert view["operator_priorities_status"]["state"] == "present"
    serialized = json.dumps(view)
    assert "SYNTHETIC_PRIVATE_TITLE" not in serialized
    assert "SYNTHETIC_PRIVATE_BODY" not in serialized


def test_f5_proposer_preserves_priority_before_truncated_charter_and_marks_cut(tmp_path, monkeypatch):
    state = _state(tmp_path)
    _operator_text(state, _priority_text(title="SYNTHETIC_PRIORITY_CANARY", body="Do this."))
    release = tmp_path / "release"
    release.mkdir()
    (release / "goals.md").write_text("CHARTER-CANARY " + ("charter text " * 1500), encoding="utf-8")
    monkeypatch.setattr(llm_proposer, "_release_root_from_env", lambda: release)
    monkeypatch.setattr(llm_proposer, "_load_goal_text", lambda _state: "CHARTER-CANARY " + ("charter text " * 1500))
    context = llm_proposer.build_context(state, None)
    assert "SYNTHETIC_PRIORITY_CANARY" in context
    assert "truncated" in context.lower()


def test_f6_strategist_charter_reports_truncation_and_original_length(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    charter = "C" * 6000
    (release / "goals.md").write_text(charter, encoding="utf-8")
    monkeypatch.setattr(llm_proposer, "_release_root_from_env", lambda: release)
    from nanobot.runtime.operator_documents import DocumentResolution, STATE_TEXT
    monkeypatch.setattr("nanobot.runtime.operator_documents.resolve_charter", lambda _root: DocumentResolution(state=STATE_TEXT, text=charter))
    text, meta = strategist_inputs.charter_input(tmp_path / "state")
    assert text.startswith(charter[:4000 - 96])
    assert meta["status"] == "truncated"
    assert meta["original_chars"] == len(charter)


def test_f7_existing_completed_paragraph_reaches_operator_resolver(tmp_path):
    state = _state(tmp_path)
    _operator_text(state, "Completed (do not repeat): Priority 7 — Already shipped.\n" + _priority_text(8, "Next", "Next task."))
    resolved = resolve_operator_priorities(state)
    # Existing Completion prose is rendered even when it is not a parseable
    # structured entry; it must not be silently lost from the prompt.
    prompt = llm_proposer.build_context(state, None)
    assert "- 7. Already shipped" in prompt


def test_f8_goal_review_numbering_and_dedup_use_operator_priorities(tmp_path, monkeypatch):
    state = _state(tmp_path)
    _operator_text(state, _priority_text(18, "Existing operator label", "Existing body."))
    release = tmp_path / "release"
    release.mkdir()
    (release / "goals.md").write_text("Charter with no numbered priorities.", encoding="utf-8")
    monkeypatch.setenv("RELEASE_ROOT", str(release))
    monkeypatch.setenv(goal_review.ENABLED_ENV, "1")
    from tests.test_goal_review import GAP, VALID_PRIORITY, _seed_valid_evidence, _write_snapshot
    _write_snapshot(state, [GAP])
    _seed_valid_evidence(state, monkeypatch=monkeypatch, repo=tmp_path)
    duplicate = {**VALID_PRIORITY, "label": "Existing operator label"}
    fresh = {**VALID_PRIORITY, "label": "Fresh derived label"}
    monkeypatch.setattr(goal_review, "_call_llm", lambda _context: {"priorities": [duplicate, fresh]})

    result = goal_review.maybe_goal_review(state, None, release_root=release)
    assert result == ["Priority 19 — Fresh derived label"]
