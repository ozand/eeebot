"""Tests for the phase-1 subagent tool-harness (#643).

Covers: workspace confinement, shared truncation, errors-as-tool-results,
the turn loop (tool-call -> result -> second turn -> stop; max_iterations;
tool-call budget), the JSONL journal, and the ``tool_harness`` profile
integration into ``subagent_materializer.materialize_subagent_requests``
(default no-tools path stays byte-identical for other profiles).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.stop_guards import STOP_REASON_GATE_CLEAN, STOP_REASON_MAX_ITERATIONS
from nanobot.runtime.subagent_materializer import materialize_subagent_requests
from nanobot.runtime.tool_harness import (
    STOP_REASON_LLM_ERROR,
    HarnessBudget,
    PathEscapeError,
    WorkspaceOperations,
    before_tool_call,
    run_harness_loop,
    run_tool_harness_request,
    tool_grep,
    tool_ls,
    tool_read,
    truncate_text,
)


class ScriptedProvider(LLMProvider):
    """Fake LLM client driving the turn loop, same style as test_provider_retry.py."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat(self, messages=None, tools=None, model=None, max_tokens=4096, temperature=0.7, reasoning_effort=None, tool_choice=None) -> LLMResponse:
        self.calls += 1
        self.seen_messages.append(list(messages or []))
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "test-model"


def _tc(call_id: str, name: str, arguments: dict) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_truncate_text_under_limits_is_unchanged():
    text = "line\n" * 10
    body, meta = truncate_text(text, max_lines=2000, max_bytes=50_000)
    assert body == text
    assert meta == {"truncated": False, "total_lines": 11, "total_bytes": len(text.encode())}


def test_truncate_text_over_line_limit_is_deterministic_head_tail():
    lines = [f"line-{i}" for i in range(5000)]
    text = "\n".join(lines)
    body, meta = truncate_text(text, max_lines=100, max_bytes=1_000_000)
    assert meta["truncated"] is True
    assert meta["total_lines"] == 5000
    assert meta["total_bytes"] == len(text.encode())
    assert "line-0" in body
    assert "line-4999" in body
    assert "omitted" in body
    # deterministic: running again gives the same output
    body2, meta2 = truncate_text(text, max_lines=100, max_bytes=1_000_000)
    assert body == body2
    assert meta == meta2


def test_truncate_text_over_byte_limit():
    text = "x" * 200_000
    body, meta = truncate_text(text, max_lines=1_000_000, max_bytes=1000)
    assert meta["truncated"] is True
    assert meta["total_bytes"] == 200_000
    assert len(body.encode("utf-8")) < 200_000


# ---------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------


def test_workspace_operations_resolves_inside_root(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    ops = WorkspaceOperations(tmp_path)
    assert ops.resolve("a.txt") == (tmp_path / "a.txt").resolve()


def test_workspace_operations_rejects_dotdot_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    ops = WorkspaceOperations(workspace)
    with pytest.raises(PathEscapeError):
        ops.resolve("../secret.txt")


def test_workspace_operations_rejects_absolute_outside_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    ops = WorkspaceOperations(workspace)
    with pytest.raises(PathEscapeError):
        ops.resolve(str(outside))


def test_workspace_operations_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leaked.txt").write_text("secret", encoding="utf-8")
    link = workspace / "escape_link"
    link.symlink_to(outside_dir)
    ops = WorkspaceOperations(workspace)
    with pytest.raises(PathEscapeError):
        ops.resolve("escape_link/leaked.txt")


def test_before_tool_call_vetoes_path_escape_and_never_touches_disk(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ops = WorkspaceOperations(workspace)
    budget = HarnessBudget(max_iterations=8, max_tool_calls=24)
    allowed, reason = before_tool_call("read", {"path": "../etc/passwd"}, ops=ops, budget=budget)
    assert allowed is False
    assert "outside the workspace root" in reason


def test_before_tool_call_vetoes_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leaked.txt").write_text("secret", encoding="utf-8")
    link = workspace / "escape_link"
    link.symlink_to(outside_dir)
    ops = WorkspaceOperations(workspace)
    budget = HarnessBudget()
    allowed, reason = before_tool_call("read", {"path": "escape_link/leaked.txt"}, ops=ops, budget=budget)
    assert allowed is False
    assert "outside the workspace root" in reason


def test_tool_read_confinement_veto_surfaces_as_tool_result_via_loop(tmp_path):
    """A confinement escape never crashes the tool function itself either."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ops = WorkspaceOperations(workspace)
    outcome = tool_read(ops, {"path": "../secret.txt"})
    assert outcome.ok is False
    assert "outside the workspace root" in outcome.text


# ---------------------------------------------------------------------------
# Errors-as-tool-results (no exceptions propagate)
# ---------------------------------------------------------------------------


def test_tool_read_nonexistent_path_is_a_result_not_an_exception(tmp_path):
    ops = WorkspaceOperations(tmp_path)
    outcome = tool_read(ops, {"path": "does_not_exist.txt"})
    assert outcome.ok is False
    assert "no such file" in outcome.text


def test_tool_grep_invalid_regex_is_a_result_not_an_exception(tmp_path):
    ops = WorkspaceOperations(tmp_path)
    outcome = tool_grep(ops, {"pattern": "("})
    assert outcome.ok is False
    assert "invalid regex" in outcome.text


def test_tool_ls_nonexistent_dir_is_a_result_not_an_exception(tmp_path):
    ops = WorkspaceOperations(tmp_path)
    outcome = tool_ls(ops, {"path": "no_such_dir"})
    assert outcome.ok is False
    assert "no such directory" in outcome.text


# ---------------------------------------------------------------------------
# Tool behavior
# ---------------------------------------------------------------------------


def test_tool_read_line_numbers_and_offset_limit(tmp_path):
    (tmp_path / "f.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    ops = WorkspaceOperations(tmp_path)
    outcome = tool_read(ops, {"path": "f.py", "offset": 2, "limit": 2})
    assert outcome.ok is True
    assert "2\tb" in outcome.text
    assert "3\tc" in outcome.text
    assert "1\ta" not in outcome.text


def test_tool_grep_finds_matches_and_caps_line_length(tmp_path):
    (tmp_path / "f.py").write_text("needle here\nno match\nx" + ("y" * 600) + " needle\n", encoding="utf-8")
    ops = WorkspaceOperations(tmp_path)
    outcome = tool_grep(ops, {"pattern": "needle"})
    assert outcome.ok is True
    assert "f.py:1:" in outcome.text
    assert "f.py:3:" in outcome.text
    # long line is capped with an ellipsis
    assert "…" in outcome.text


def test_tool_grep_no_matches(tmp_path):
    (tmp_path / "f.py").write_text("nothing here\n", encoding="utf-8")
    ops = WorkspaceOperations(tmp_path)
    outcome = tool_grep(ops, {"pattern": "zzzznotfound"})
    assert outcome.ok is True
    assert "no matches" in outcome.text


def test_tool_ls_sorted_dirs_marked(tmp_path):
    (tmp_path / "b_file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a_dir").mkdir()
    ops = WorkspaceOperations(tmp_path)
    outcome = tool_ls(ops, {})
    lines = outcome.text.splitlines()
    assert lines[0].startswith("d ") and "a_dir" in lines[0]
    assert lines[1].startswith("- ") and "b_file.txt" in lines[1]


# ---------------------------------------------------------------------------
# Turn loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_tool_call_then_second_turn_then_stop(tmp_path):
    (tmp_path / "f.py").write_text("hello world\n", encoding="utf-8")
    ops = WorkspaceOperations(tmp_path)
    budget = HarnessBudget(max_iterations=8, max_tool_calls=24)
    journal_path = tmp_path / "journal.jsonl"

    provider = ScriptedProvider([
        LLMResponse(content=None, tool_calls=[_tc("call-1", "read", {"path": "f.py"})]),
        LLMResponse(content="Found: hello world", tool_calls=[]),
    ])

    result = await run_harness_loop(
        provider,
        model="test-model",
        messages=[{"role": "user", "content": "inspect f.py"}],
        ops=ops,
        budget=budget,
        journal_path=journal_path,
    )

    assert result["stop_reason"] == STOP_REASON_GATE_CLEAN
    assert result["tool_calls_count"] == 1
    assert provider.calls == 2
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "hello world" in tool_msgs[0]["content"]

    journal_lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(journal_lines) == 1
    entry = json.loads(journal_lines[0])
    assert entry["tool"] == "read"
    assert entry["decision"] == "allow"
    assert entry["truncated"] is False


@pytest.mark.asyncio
async def test_loop_stops_at_max_iterations(tmp_path):
    ops = WorkspaceOperations(tmp_path)
    budget = HarnessBudget(max_iterations=2, max_tool_calls=100)
    journal_path = tmp_path / "journal.jsonl"

    # Model keeps calling ls forever — the loop must cut it off at max_iterations.
    responses = [
        LLMResponse(content=None, tool_calls=[_tc(f"call-{i}", "ls", {})])
        for i in range(10)
    ]
    provider = ScriptedProvider(responses)

    result = await run_harness_loop(
        provider,
        model="test-model",
        messages=[{"role": "user", "content": "loop forever"}],
        ops=ops,
        budget=budget,
        journal_path=journal_path,
    )

    assert result["stop_reason"] == STOP_REASON_MAX_ITERATIONS
    assert provider.calls == 2  # exactly max_iterations turns taken


@pytest.mark.asyncio
async def test_loop_stops_at_tool_call_budget(tmp_path):
    ops = WorkspaceOperations(tmp_path)
    budget = HarnessBudget(max_iterations=100, max_tool_calls=2)
    journal_path = tmp_path / "journal.jsonl"

    # One turn asks for 3 tool calls at once — budget should cut it off mid-turn.
    provider = ScriptedProvider([
        LLMResponse(content=None, tool_calls=[
            _tc("call-1", "ls", {}),
            _tc("call-2", "ls", {}),
            _tc("call-3", "ls", {}),
        ]),
    ])

    result = await run_harness_loop(
        provider,
        model="test-model",
        messages=[{"role": "user", "content": "ls three times"}],
        ops=ops,
        budget=budget,
        journal_path=journal_path,
    )

    assert result["stop_reason"] == "budget_tool_calls"
    assert result["tool_calls_count"] == 2

    journal_lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(journal_lines) == 2  # third call never got a journal entry — loop stopped first


@pytest.mark.asyncio
async def test_loop_veto_is_journaled_and_loop_continues(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ops = WorkspaceOperations(workspace)
    budget = HarnessBudget(max_iterations=8, max_tool_calls=24)
    journal_path = tmp_path / "journal.jsonl"

    provider = ScriptedProvider([
        LLMResponse(content=None, tool_calls=[_tc("call-1", "read", {"path": "../secret.txt"})]),
        LLMResponse(content="ok, giving up on that path", tool_calls=[]),
    ])

    result = await run_harness_loop(
        provider,
        model="test-model",
        messages=[{"role": "user", "content": "read ../secret.txt"}],
        ops=ops,
        budget=budget,
        journal_path=journal_path,
    )

    assert result["stop_reason"] == STOP_REASON_GATE_CLEAN
    assert provider.calls == 2  # loop continued to a second turn after the veto

    journal_lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(journal_lines[0])
    assert entry["decision"] == "veto"
    assert "outside the workspace root" in entry["veto_reason"]

    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert "vetoed" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_loop_llm_error_breaks_immediately_without_raising(tmp_path):
    """A degraded LLMResponse(finish_reason="error") must not look like gate_clean.

    Live-found bug (#643 phase-1 verification): chat_with_retry never raises —
    after exhausting retries it returns an error-content LLMResponse. Before
    this fix the loop treated that as an ordinary no-tool-call turn and broke
    with stop_reason="gate_clean", so an LLM outage was recorded as completed.
    """
    ops = WorkspaceOperations(tmp_path)
    budget = HarnessBudget(max_iterations=8, max_tool_calls=24)
    journal_path = tmp_path / "journal.jsonl"

    # Non-transient wording (no "connection"/"timeout"/etc marker) so
    # chat_with_retry's own transient-error retry does not also kick in —
    # this test is only about the harness loop's finish_reason check.
    provider = ScriptedProvider([
        LLMResponse(content="Error calling LLM: invalid api key", tool_calls=[], finish_reason="error"),
    ])

    result = await run_harness_loop(
        provider,
        model="test-model",
        messages=[{"role": "user", "content": "inspect something"}],
        ops=ops,
        budget=budget,
        journal_path=journal_path,
    )

    assert result["stop_reason"] == STOP_REASON_LLM_ERROR
    assert provider.calls == 1
    assert "Error calling LLM" in result["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_loop_normal_run_unaffected_by_error_check(tmp_path):
    """finish_reason defaults to "stop" — the new check must not alter normal runs."""
    ops = WorkspaceOperations(tmp_path)
    budget = HarnessBudget(max_iterations=8, max_tool_calls=24)
    journal_path = tmp_path / "journal.jsonl"

    provider = ScriptedProvider([
        LLMResponse(content="all clear, nothing to report", tool_calls=[]),
    ])

    result = await run_harness_loop(
        provider,
        model="test-model",
        messages=[{"role": "user", "content": "inspect something"}],
        ops=ops,
        budget=budget,
        journal_path=journal_path,
    )

    assert result["stop_reason"] == STOP_REASON_GATE_CLEAN
    assert provider.calls == 1


# ---------------------------------------------------------------------------
# Sync entrypoint: run_tool_harness_request
# ---------------------------------------------------------------------------


def test_run_tool_harness_request_end_to_end(tmp_path):
    workspace = tmp_path / "eeebot-self-evolving"
    workspace.mkdir()
    (workspace / "notes.md").write_text("target content\n", encoding="utf-8")
    state_root = tmp_path / "state"

    provider = ScriptedProvider([
        LLMResponse(content=None, tool_calls=[_tc("call-1", "grep", {"pattern": "target"})]),
        LLMResponse(content="Found target content in notes.md", tool_calls=[]),
    ])

    result = run_tool_harness_request(
        {"request_id": "req-abc123", "task_title": "find target"},
        state_root=state_root,
        workspace_root=workspace,
        provider=provider,
        model="test-model",
    )

    assert result["ok"] is True
    assert result["stop_reason"] == STOP_REASON_GATE_CLEAN
    assert result["tool_calls_count"] == 1
    assert "Found target content" in result["stdout"]
    journal_path = Path(result["tool_call_journal"])
    assert journal_path.exists()
    assert journal_path.parent == state_root / "subagents" / "tool_calls"


def test_run_tool_harness_request_llm_error_is_ok_false_not_an_exception(tmp_path):
    workspace = tmp_path / "eeebot-self-evolving"
    workspace.mkdir()
    state_root = tmp_path / "state"

    provider = ScriptedProvider([
        LLMResponse(content="Error calling LLM: model group down", tool_calls=[], finish_reason="error"),
    ])

    result = run_tool_harness_request(
        {"request_id": "req-err", "task_title": "find target"},
        state_root=state_root,
        workspace_root=workspace,
        provider=provider,
        model="test-model",
    )

    assert result["ok"] is False
    assert result["stop_reason"] == STOP_REASON_LLM_ERROR
    assert "Error calling LLM" in result["stdout"]


# ---------------------------------------------------------------------------
# Integration: subagent_materializer profile == "tool_harness"
# ---------------------------------------------------------------------------


def _write_request(request_dir: Path, name: str, payload: dict) -> Path:
    request_dir.mkdir(parents=True, exist_ok=True)
    path = request_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_materialize_tool_harness_profile_runs_the_harness(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    workspace = tmp_path / "eeebot-self-evolving"
    workspace.mkdir()
    (workspace / "target.py").write_text("def foo(): pass\n", encoding="utf-8")

    _write_request(state_root / "subagents" / "requests", "request-1.json", {
        "request_id": "req-1",
        "profile": "tool_harness",
        "status": "queued",
        "task_title": "inspect target.py",
    })

    provider = ScriptedProvider([
        LLMResponse(content=None, tool_calls=[_tc("call-1", "read", {"path": "target.py"})]),
        LLMResponse(content="target.py defines foo()", tool_calls=[]),
    ])

    real_run_tool_harness_request = run_tool_harness_request

    def _fake_run_tool_harness_request(request, *, state_root, **kwargs):
        return real_run_tool_harness_request(request, state_root=state_root, workspace_root=workspace, provider=provider, model="test-model")

    monkeypatch.setattr(
        "nanobot.runtime.tool_harness.run_tool_harness_request",
        _fake_run_tool_harness_request,
    )

    summary = materialize_subagent_requests(state_root=state_root)
    assert summary["terminalized_count"] == 1
    result = summary["results"][0]

    assert result["materialized_from"] == "tool_harness"
    assert result["result_status"] == "completed"
    assert result["executor"]["provider"] == "tool_harness_phase1"
    assert result["tool_calls_count"] == 1
    assert result["stop_reason"] == STOP_REASON_GATE_CLEAN
    assert Path(result["tool_call_journal"]).exists()


def test_materialize_tool_harness_profile_llm_error_is_blocked_with_distinct_reason(tmp_path, monkeypatch):
    """Live-found bug (#643): an LLM outage must not materialize as completed/gate_clean."""
    state_root = tmp_path / "state"
    workspace = tmp_path / "eeebot-self-evolving"
    workspace.mkdir()

    _write_request(state_root / "subagents" / "requests", "request-1.json", {
        "request_id": "req-1",
        "profile": "tool_harness",
        "status": "queued",
        "task_title": "inspect target.py",
    })

    provider = ScriptedProvider([
        LLMResponse(content="Error calling LLM: model group down", tool_calls=[], finish_reason="error"),
    ])

    real_run_tool_harness_request = run_tool_harness_request

    def _fake_run_tool_harness_request(request, *, state_root, **kwargs):
        return real_run_tool_harness_request(request, state_root=state_root, workspace_root=workspace, provider=provider, model="test-model")

    monkeypatch.setattr(
        "nanobot.runtime.tool_harness.run_tool_harness_request",
        _fake_run_tool_harness_request,
    )

    summary = materialize_subagent_requests(state_root=state_root)
    assert summary["terminalized_count"] == 1
    result = summary["results"][0]

    assert result["result_status"] == "blocked"
    assert result["terminal_reason"] == "tool_harness_llm_error"
    assert result["stop_reason"] == STOP_REASON_LLM_ERROR


def test_materialize_research_only_profile_is_byte_identical_to_before(tmp_path):
    """The default no-tools path (no configured executor) must be unaffected."""
    state_root = tmp_path / "state"
    _write_request(state_root / "subagents" / "requests", "request-1.json", {
        "request_id": "req-1",
        "profile": "research_only",
        "status": "queued",
        "task_title": "review something",
    })

    summary = materialize_subagent_requests(state_root=state_root)
    result = summary["results"][0]

    # Unaffected: same blocked-stub shape research_only always got when no
    # executor is configured.
    assert result["materialized_from"] == "queued_request_terminalizer"
    assert result["executor"] is None
    assert result["result_status"] == "blocked"
    assert result["tool_calls_count"] is None
    assert result["tool_call_journal"] is None
    assert result["stop_reason"] is None
