"""Tests for runtime-interpreted slash commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus


def _make_loop(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "gpt-5.3-codex"
    provider.chat_with_retry = AsyncMock()
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="gpt-5.3-codex")
    return loop, provider


@pytest.mark.asyncio
async def test_cap_status_is_handled_before_llm_chat(tmp_path) -> None:
    loop, provider = _make_loop(tmp_path)
    msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/cap_status")

    response = await loop._process_message(msg)

    assert response is not None
    assert "autonomy:" in response.content
    assert "model:" in response.content
    assert "gpt-5.3-codex" in response.content
    assert "workspace" in response.content
    provider.chat_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_tiny_runtime_check_is_handled_before_llm_chat(tmp_path) -> None:
    loop, provider = _make_loop(tmp_path)
    msg = InboundMessage(
        channel="telegram",
        sender_id="u1",
        chat_id="c1",
        content="/workspace experiment tiny-runtime-check",
    )

    response = await loop._process_message(msg)

    assert response is not None
    assert "action_id: workspace.experiment.tiny_runtime_check" in response.content
    assert "written: True" in response.content
    assert "executed: True" in response.content
    assert "verified: True" in response.content
    assert (tmp_path / "state" / "telegram_live_probe" / "tiny-runtime-check.json").exists()
    provider.chat_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_sub_run_micro_is_handled_before_llm_chat(tmp_path) -> None:
    loop, provider = _make_loop(tmp_path)
    msg = InboundMessage(
        channel="telegram",
        sender_id="u1",
        chat_id="c1",
        content="/sub_run --profile research_only --budget micro ping-telegram-live-2026-04-29T02:30Z",
    )

    response = await loop._process_message(msg)

    assert response is not None
    assert "bounded" in response.content.lower()
    assert "profile: research_only" in response.content
    assert "budget: micro" in response.content
    assert "task: ping-telegram-live-2026-04-29T02:30Z" in response.content
    assert (tmp_path / "state" / "telegram_live_probe" / "sub_run_micro.json").exists()
    provider.chat_with_retry.assert_not_called()
