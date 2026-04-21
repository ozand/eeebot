"""Tests for /stop task cancellation."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_loop(*, exec_config=None):
    """Create a minimal AgentLoop with mocked dependencies."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace, exec_config=exec_config)
    return loop, bus


class TestHandleStop:
    @pytest.mark.asyncio
    async def test_stop_no_active_task(self):
        from nanobot.bus.events import InboundMessage

        loop, bus = _make_loop()
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "No active task" in out.content

    @pytest.mark.asyncio
    async def test_stop_cancels_active_task(self):
        from nanobot.bus.events import InboundMessage

        loop, bus = _make_loop()
        cancelled = asyncio.Event()

        async def slow_task():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(slow_task())
        await asyncio.sleep(0)
        loop._active_tasks["test:c1"] = [task]

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)

        assert cancelled.is_set()
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "stopped" in out.content.lower()

    @pytest.mark.asyncio
    async def test_stop_cancels_multiple_tasks(self):
        from nanobot.bus.events import InboundMessage

        loop, bus = _make_loop()
        events = [asyncio.Event(), asyncio.Event()]

        async def slow(idx):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                events[idx].set()
                raise

        tasks = [asyncio.create_task(slow(i)) for i in range(2)]
        await asyncio.sleep(0)
        loop._active_tasks["test:c1"] = tasks

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/stop")
        await loop._handle_stop(msg)

        assert all(e.is_set() for e in events)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert "2 task" in out.content


class TestDispatch:
    def test_exec_tool_not_registered_when_disabled(self):
        from nanobot.config.schema import ExecToolConfig

        loop, _bus = _make_loop(exec_config=ExecToolConfig(enable=False))

        assert loop.tools.get("exec") is None

    @pytest.mark.asyncio
    async def test_dispatch_processes_and_publishes(self):
        from nanobot.bus.events import InboundMessage, OutboundMessage

        loop, bus = _make_loop()
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="hello")
        loop._process_message = AsyncMock(
            return_value=OutboundMessage(channel="test", chat_id="c1", content="hi")
        )
        await loop._dispatch(msg)
        out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
        assert out.content == "hi"

    @pytest.mark.asyncio
    async def test_dispatch_cancel_restores_runtime_checkpoint(self):
        from nanobot.bus.events import InboundMessage

        loop, _bus = _make_loop()
        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="hello")
        session = MagicMock()
        session.messages = []
        session.metadata = {}
        loop.sessions.get_or_create.return_value = session
        loop._emit_runtime_checkpoint(
            session,
            assistant_message={"role": "assistant", "content": "Let me search for that."},
            completed_tool_results=[{"role": "tool", "tool_call_id": "tc_1", "content": "Search results: ..."}],
        )
        session.metadata[loop._PENDING_USER_TURN_KEY] = {"content": "hello"}

        async def _cancel(*_args, **_kwargs):
            raise asyncio.CancelledError()

        loop._process_message = _cancel
        with pytest.raises(asyncio.CancelledError):
            await loop._dispatch(msg)

        restored = loop.sessions.get_or_create(msg.session_key)
        assert any(m.get("content") == "Let me search for that." for m in restored.messages)
        assert any(m.get("content") == "Search results: ..." for m in restored.messages)
        assert loop._RUNTIME_CHECKPOINT_KEY not in restored.metadata
        assert loop._PENDING_USER_TURN_KEY not in restored.metadata

    @pytest.mark.asyncio
    async def test_processing_lock_serializes(self):
        from nanobot.bus.events import InboundMessage, OutboundMessage

        loop, bus = _make_loop()
        order = []

        async def mock_process(m, **kwargs):
            order.append(f"start-{m.content}")
            await asyncio.sleep(0.05)
            order.append(f"end-{m.content}")
            return OutboundMessage(channel="test", chat_id="c1", content=m.content)

        loop._process_message = mock_process
        msg1 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="a")
        msg2 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="b")

        t1 = asyncio.create_task(loop._dispatch(msg1))
        t2 = asyncio.create_task(loop._dispatch(msg2))
        await asyncio.gather(t1, t2)
        assert order == ["start-a", "end-a", "start-b", "end-b"]


class TestSubagentCancellation:
    @pytest.mark.asyncio
    async def test_cancel_by_session(self):
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        mgr = SubagentManager(provider=provider, workspace=MagicMock(), bus=bus)

        cancelled = asyncio.Event()

        async def slow():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(slow())
        await asyncio.sleep(0)
        mgr._running_tasks["sub-1"] = task
        mgr._session_tasks["test:c1"] = {"sub-1"}

        count = await mgr.cancel_by_session("test:c1")
        assert count == 1
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_cancel_by_session_no_tasks(self):
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        mgr = SubagentManager(provider=provider, workspace=MagicMock(), bus=bus)
        assert await mgr.cancel_by_session("nonexistent") == 0

    @pytest.mark.asyncio
    async def test_subagent_preserves_reasoning_fields_in_tool_turn(self, monkeypatch, tmp_path):
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus
        from nanobot.providers.base import LLMResponse, ToolCallRequest

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"

        captured_second_call: list[dict] = []

        call_count = {"n": 0}

        async def scripted_chat_with_retry(*, messages, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return LLMResponse(
                    content="thinking",
                    tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={})],
                    reasoning_content="hidden reasoning",
                    thinking_blocks=[{"type": "thinking", "thinking": "step"}],
                )
            captured_second_call[:] = messages
            return LLMResponse(content="done", tool_calls=[])
        provider.chat_with_retry = scripted_chat_with_retry
        mgr = SubagentManager(provider=provider, workspace=tmp_path, bus=bus)

        async def fake_execute(self, name, arguments):
            return "tool result"

        monkeypatch.setattr("nanobot.agent.tools.registry.ToolRegistry.execute", fake_execute)

        await mgr._run_subagent("sub-1", "do task", "label", {"channel": "test", "chat_id": "c1"})

        assistant_messages = [
            msg for msg in captured_second_call
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        ]
        assert len(assistant_messages) == 1
        assert assistant_messages[0]["reasoning_content"] == "hidden reasoning"
        assert assistant_messages[0]["thinking_blocks"] == [{"type": "thinking", "thinking": "step"}]

    @pytest.mark.asyncio
    async def test_subagent_writes_durable_telemetry(self, monkeypatch, tmp_path):
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus
        from nanobot.providers.base import LLMResponse

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="all done", tool_calls=[]))
        mgr = SubagentManager(provider=provider, workspace=tmp_path, bus=bus)

        async def fake_execute(self, name, arguments):
            return "tool result"

        async def fake_publish_inbound(_msg):
            return None

        monkeypatch.setattr("nanobot.agent.tools.registry.ToolRegistry.execute", fake_execute)
        monkeypatch.setattr(bus, "publish_inbound", fake_publish_inbound)
        monkeypatch.setattr(
            "nanobot.runtime.state.load_runtime_state_for_workspace",
            lambda _workspace: {
                "active_goal": "goal-1",
                "cycle_id": "cycle-1",
                "report_path": str(tmp_path / "state" / "reports" / "evolution-1.json"),
                "current_task_id": "record-reward",
                "task_reward_signal": {"value": 1.0, "source": "improvement_score"},
                "task_feedback_decision": {"mode": "force_remediation", "selection_source": "feedback_repeat_block_remediation"},
            },
        )

        await mgr._run_subagent(
            "sub-1",
            "finish this task",
            "label",
            {"channel": "test", "chat_id": "c1"},
            session_key="session-1",
        )

        telemetry_path = tmp_path / "state" / "subagents" / "sub-1.json"
        assert telemetry_path.exists()
        payload = json.loads(telemetry_path.read_text(encoding="utf-8"))
        assert payload["subagent_id"] == "sub-1"
        assert payload["task"] == "finish this task"
        assert payload["started_at"]
        assert payload["finished_at"]
        assert payload["status"] == "ok"
        assert payload["summary"] == "all done"
        assert payload["result"] == "all done"
        assert payload["origin"] == {"channel": "test", "chat_id": "c1"}
        assert payload["parent_context"]["session_key"] == "session-1"
        assert payload["parent_context"]["origin"] == {"channel": "test", "chat_id": "c1"}
        assert payload["goal_id"] == "goal-1"
        assert payload["cycle_id"] == "cycle-1"
        assert payload["report_path"] == str(tmp_path / "state" / "reports" / "evolution-1.json")
        assert payload["current_task_id"] == "record-reward"
        assert payload["task_reward_signal"]["value"] == 1.0
        assert payload["task_feedback_decision"]["mode"] == "force_remediation"

    @pytest.mark.asyncio
    async def test_subagent_writes_canonical_telemetry_to_host_state_root(self, monkeypatch, tmp_path):
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus
        from nanobot.providers.base import LLMResponse

        host_state = tmp_path / "host-state"
        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="all done", tool_calls=[]))
        monkeypatch.setenv("NANOBOT_RUNTIME_STATE_SOURCE", "host_control_plane")
        monkeypatch.setenv("NANOBOT_RUNTIME_STATE_ROOT", str(host_state))
        monkeypatch.setattr(
            "nanobot.runtime.state.load_runtime_state_for_workspace",
            lambda _workspace: {
                "active_goal": "goal-1",
                "cycle_id": "cycle-1",
                "report_path": str(host_state / "reports" / "evolution-1.json"),
                "current_task_id": "record-reward",
                "task_reward_signal": {"value": 1.0, "source": "improvement_score"},
                "task_feedback_decision": {"mode": "force_remediation", "selection_source": "feedback_repeat_block_remediation"},
            },
        )

        mgr = SubagentManager(provider=provider, workspace=tmp_path, bus=bus)

        async def fake_execute(self, name, arguments):
            return "tool result"

        async def fake_publish_inbound(_msg):
            return None

        monkeypatch.setattr("nanobot.agent.tools.registry.ToolRegistry.execute", fake_execute)
        monkeypatch.setattr(bus, "publish_inbound", fake_publish_inbound)

        await mgr._run_subagent(
            "sub-1",
            "finish this task",
            "label",
            {"channel": "test", "chat_id": "c1"},
            session_key="session-1",
        )

        telemetry_path = host_state / "subagents" / "sub-1.json"
        assert telemetry_path.exists()
        payload = json.loads(telemetry_path.read_text(encoding="utf-8"))
        assert payload["runtime_state_root"] == str(host_state)
        assert payload["runtime_state_source"] == "host_control_plane"
        assert payload["goal_id"] == "goal-1"
        assert payload["cycle_id"] == "cycle-1"
        assert payload["report_path"] == str(host_state / "reports" / "evolution-1.json")
        assert payload["current_task_id"] == "record-reward"
        assert payload["task_reward_signal"]["value"] == 1.0
        assert payload["task_feedback_decision"]["mode"] == "force_remediation"

    @pytest.mark.asyncio
    async def test_subagent_announce_result_uses_session_key_override(self, monkeypatch, tmp_path):
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        published = []

        async def _capture(msg):
            published.append(msg)

        monkeypatch.setattr(bus, 'publish_inbound', _capture)
        provider = MagicMock()
        provider.get_default_model.return_value = 'test-model'
        mgr = SubagentManager(provider=provider, workspace=tmp_path, bus=bus)

        await mgr._announce_result(
            task_id='sub-1',
            label='label',
            task='task',
            result='done',
            origin={'channel': 'cli', 'chat_id': 'direct', 'session_key': 'heartbeat'},
            status='ok',
        )
        assert published
        assert published[0].session_key_override == 'heartbeat'
