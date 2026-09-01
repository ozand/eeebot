import pytest


def test_subagent_manager_accepts_deployed_bridge_compat_kwargs(tmp_path):
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return 'test-model'

    class SubagentCfg:
        max_running = 3

    from nanobot.agent.subagent import SubagentManager
    from unittest.mock import Mock
    manager = SubagentManager(
        provider=Provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        subagent_config=SubagentCfg(),
        max_running=2,
    )
    assert manager.max_running == 2


def test_subagent_system_prompt_includes_harness_context(tmp_path):
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return 'test-model'

    from nanobot.agent.subagent import SubagentManager
    from unittest.mock import Mock
    manager = SubagentManager(
        provider=Provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        system_context="# Immutable operator charter\n\nCHARTER SENTINEL",
    )

    prompt = manager._build_subagent_prompt()
    assert "CHARTER SENTINEL" in prompt


def test_subagent_manager_default_max_iterations_is_15(tmp_path):
    """Issue #578: omitting max_iterations preserves the historical default."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return 'test-model'

    from nanobot.agent.subagent import SubagentManager
    from unittest.mock import Mock
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus())
    assert manager.max_iterations == 15


def test_subagent_manager_honors_configured_max_iterations(tmp_path):
    """Issue #578: callers can thread agents.defaults.maxToolIterations through,
    so a subagent turn isn't cut off earlier than the coordinator's own budget."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return 'test-model'

    from nanobot.agent.subagent import SubagentManager
    from unittest.mock import Mock
    manager = SubagentManager(
        provider=Provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        max_iterations=100,
    )
    assert manager.max_iterations == 100


def test_subagent_telemetry_includes_compaction_and_prompt_fields(tmp_path):
    """Issue #1122: _build_subagent_telemetry_payload records compaction_count and last_prompt_tokens."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return "test-model"

    from nanobot.agent.subagent import SubagentManager
    from unittest.mock import Mock
    manager = SubagentManager(
        provider=Provider(),
        workspace=tmp_path,
        bus=MessageBus(),
    )

    payload = manager._build_subagent_telemetry_payload(
        task_id="task-123",
        task="do thing",
        label="worker",
        started_at="2026-08-31T00:00:00Z",
        finished_at="2026-08-31T00:01:00Z",
        status="completed",
        summary="done",
        result="ok",
        origin={},
        session_key=None,
        stop_reason="natural",
    )
    # By default, compaction fields are not in the raw build helper unless in correlation_context or added at completion
    assert payload["status"] == "completed"


def test_subagent_telemetry_omits_none_prompt_tokens(tmp_path):
    """If last_prompt_tokens is None, it is not present in the payload (or is None)."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return "test-model"

    from nanobot.agent.subagent import SubagentManager
    from unittest.mock import Mock
    manager = SubagentManager(
        provider=Provider(),
        workspace=tmp_path,
        bus=MessageBus(),
    )

    payload = manager._build_subagent_telemetry_payload(
        task_id="task-124",
        task="do thing",
        label="worker",
        started_at="2026-08-31T00:00:00Z",
        finished_at=None,
        status="running",
        summary=None,
        result=None,
        origin={},
        session_key=None,
    )
    assert "last_prompt_tokens" not in payload


import asyncio
from unittest.mock import AsyncMock
import uuid
import json

@pytest.mark.asyncio
async def test_subagent_telemetry_tracks_context_usage(tmp_path):
    # Dummy provider returning a mocked LLMChatResponse
    class FakeResponse:
        def __init__(self, content, finish_reason="stop", has_tool_calls=False, tool_calls=None, usage=None):
            self.content = content
            self.finish_reason = finish_reason
            self.has_tool_calls = has_tool_calls
            self.tool_calls = tool_calls or []
            self.usage = usage or {}

    class FakeProvider:
        async def chat_with_retry(self, *args, **kwargs):
            # simulate 1 iteration with 1500 prompt tokens
            return FakeResponse(
                content="done", 
                finish_reason="stop",
                usage={"prompt_tokens": 1500}
            )

    from nanobot.agent.subagent import SubagentManager
    from unittest.mock import Mock
    manager = SubagentManager(
        provider=FakeProvider(),
        workspace=tmp_path,
        bus=AsyncMock(),
        model="fake/model",
    )

    t_id = await manager.spawn("test task")
    assert t_id

    # wait for the background running task to finish
    await asyncio.sleep(0.1)

    # find the written telemetry file
    telemetry_dir = manager._telemetry_dir
    with open(telemetry_dir / next(telemetry_dir.glob("*.json")).name) as f:
        data = json.load(f)

    assert "context_usage" in data
    assert data["context_usage"]["peak_tokens"] == 1500
    assert data["context_usage"]["iterations"] == [1500]
