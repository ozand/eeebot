"""Tests for runtime self-evolution probe slash commands in nanobot.runtime.probes."""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.bus.events import InboundMessage
from nanobot.runtime.probes import handle_probe_command


def _make_msg(content: str = "hello") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        chat_id="chat-42",
        sender_id="user-1",
        content=content,
        metadata={"source": "test"},
    )


def test_handle_probe_command_cap_status(tmp_path: Path):
    workspace = tmp_path / "workspace"
    msg = _make_msg("/cap_status")
    res = handle_probe_command(workspace, "test-model", msg, "/cap_status")

    assert res is not None
    assert res.channel == "telegram"
    assert res.chat_id == "chat-42"
    assert "autonomy: runtime-command-router" in res.content
    assert "model: test-model" in res.content
    assert f"workspace: {workspace}" in res.content
    assert "telegram_runtime_dispatch: True" in res.content


def test_handle_probe_command_tiny_runtime_check(tmp_path: Path):
    workspace = tmp_path / "workspace"
    msg = _make_msg("/workspace experiment tiny-runtime-check")
    res = handle_probe_command(workspace, "test-model", msg, "/workspace experiment tiny-runtime-check")

    assert res is not None
    assert res.channel == "telegram"
    assert res.chat_id == "chat-42"
    assert "action_id: workspace.experiment.tiny_runtime_check" in res.content
    assert "written: True" in res.content
    assert "executed: True" in res.content
    assert "verified: True" in res.content

    artifact = workspace / "state" / "telegram_live_probe" / "tiny-runtime-check.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["action_id"] == "workspace.experiment.tiny_runtime_check"
    assert data["channel"] == "telegram"
    assert data["chat_id"] == "chat-42"
    assert data["written"] is True
    assert data["executed"] is True
    assert data["verified"] is True


def test_handle_probe_command_sub_run_bounded_and_unbounded(tmp_path: Path):
    workspace = tmp_path / "workspace"
    msg = _make_msg("/sub_run")

    # Bounded branch: profile=research_only, budget=micro, non-empty task
    res_bounded = handle_probe_command(
        workspace,
        "test-model",
        msg,
        "/sub_run --profile research_only --budget micro evaluate test coverage",
    )
    assert res_bounded is not None
    assert "bounded: True" in res_bounded.content
    assert "profile: research_only" in res_bounded.content
    assert "budget: micro" in res_bounded.content
    assert "task: evaluate test coverage" in res_bounded.content
    assert "execution_state: accepted_for_bounded_runtime_dispatch" in res_bounded.content

    artifact = workspace / "state" / "telegram_live_probe" / "sub_run_micro.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["bounded"] is True
    assert data["execution_state"] == "accepted_for_bounded_runtime_dispatch"
    assert data["task"] == "evaluate test coverage"

    # Unbounded branch: different profile/budget
    res_unbounded = handle_probe_command(
        workspace,
        "test-model",
        msg,
        "/sub_run --profile full_agent --budget large do whatever",
    )
    assert res_unbounded is not None
    assert "bounded: False" in res_unbounded.content
    assert "profile: full_agent" in res_unbounded.content
    assert "budget: large" in res_unbounded.content
    assert "execution_state: rejected_by_policy" in res_unbounded.content

    data_unbounded = json.loads(artifact.read_text(encoding="utf-8"))
    assert data_unbounded["bounded"] is False
    assert data_unbounded["execution_state"] == "rejected_by_policy"


def test_handle_probe_command_fallthrough_none(tmp_path: Path):
    workspace = tmp_path / "workspace"
    msg = _make_msg("just a regular user chat message")

    assert handle_probe_command(workspace, "test-model", msg, "hello bot") is None
    assert handle_probe_command(workspace, "test-model", msg, "/unknown_command") is None
    assert handle_probe_command(workspace, "test-model", msg, "/sub_run invalid args") is None
