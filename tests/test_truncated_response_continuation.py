"""#1774 -- a response cut at the completion ceiling is a continuation signal.

Before this, `finish_reason == "length"` was never inspected. A cut response
carries no complete tool call, so `has_tool_calls` was False and the executor
loop took the fragment as the cycle's final answer. Measured on host eeepc over
9,714 executor calls (2026-09-10..18): 23 responses ended with
`finish_reason="length"`, `completion_tokens` was exactly 8,192 on every one,
and **21 of the 23 were the last executor call of their cycle** -- the cycle
ended silently with most of its iteration budget unspent.

It is the OUTPUT ceiling, not context pressure: one of the 23 had ~33,000
tokens of input headroom left. `AgentDefaults.max_tokens` is out of scope here
(#1774 non-goal); this suite covers the loop's handling only.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest


class FakeResponse:
    def __init__(self, content, finish_reason="stop", has_tool_calls=False, tool_calls=None, usage=None):
        self.content = content
        self.finish_reason = finish_reason
        self.has_tool_calls = has_tool_calls
        self.tool_calls = tool_calls or []
        self.usage = usage or {}
        self.reasoning_content = None
        self.thinking_blocks = None


class ScriptedProvider:
    """Returns a fixed sequence of responses and records what it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.seen_messages = []

    async def chat_with_retry(self, *args, **kwargs):
        self.seen_messages.append(list(kwargs.get("messages") or []))
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(content="fallback", finish_reason="stop")


async def _run(tmp_path, provider):
    from nanobot.agent.subagent import SubagentManager

    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=AsyncMock(),
        model="fake/model",
    )
    await manager.spawn("test task")
    await asyncio.sleep(0.2)
    written = sorted(manager._telemetry_dir.glob("*.json"))
    assert len(written) == 1, [p.name for p in written]
    return json.loads(written[0].read_text(encoding="utf-8")), manager


@pytest.mark.asyncio
async def test_truncated_response_does_not_end_the_cycle(tmp_path):
    """The headline defect: a cut fragment was the cycle's answer."""
    provider = ScriptedProvider([
        FakeResponse(content="I will start by exam", finish_reason="length"),
        FakeResponse(content="finished properly", finish_reason="stop"),
    ])
    data, _ = await _run(tmp_path, provider)

    assert data["result"] == "finished properly"
    assert data["result"] != "I will start by exam"
    assert len(provider.seen_messages) == 2, "the loop stopped instead of continuing"


@pytest.mark.asyncio
async def test_the_partial_text_is_preserved_for_the_continuation(tmp_path):
    """Discarding the fragment would make the continuation a retry from nothing."""
    provider = ScriptedProvider([
        FakeResponse(content="half a thought", finish_reason="length"),
        FakeResponse(content="done", finish_reason="stop"),
    ])
    await _run(tmp_path, provider)

    second_call = provider.seen_messages[1]
    contents = [str(m.get("content") or "") for m in second_call]
    assert any("half a thought" in c for c in contents), contents
    assert any("cut off before it finished" in c for c in contents), contents


@pytest.mark.asyncio
async def test_repeated_truncation_stops_with_its_own_reason(tmp_path):
    """Bounded so it cannot spin, and the stop is NOT a silent completion."""
    from nanobot.agent.subagent import _MAX_CONSECUTIVE_TRUNCATIONS

    provider = ScriptedProvider([
        FakeResponse(content=f"chunk {i}", finish_reason="length")
        for i in range(_MAX_CONSECUTIVE_TRUNCATIONS + 2)
    ])
    data, _ = await _run(tmp_path, provider)

    assert data["stop_reason"] == "response_truncated"
    assert data["status"] == "bounded_stop"
    assert "completion ceiling" in data["result"]
    assert len(provider.seen_messages) == _MAX_CONSECUTIVE_TRUNCATIONS


@pytest.mark.asyncio
async def test_telemetry_records_the_truncation_count_and_recovery(tmp_path):
    provider = ScriptedProvider([
        FakeResponse(content="cut", finish_reason="length"),
        FakeResponse(content="done", finish_reason="stop"),
    ])
    data, _ = await _run(tmp_path, provider)

    assert data["truncation"] == {"count": 1, "continued": True}


@pytest.mark.asyncio
async def test_telemetry_records_zero_for_an_untruncated_cycle(tmp_path):
    """Zeros are written, not omitted: a cycle that saw no truncation and a
    cycle from a release that could not record one must not read the same."""
    provider = ScriptedProvider([FakeResponse(content="done", finish_reason="stop")])
    data, _ = await _run(tmp_path, provider)

    assert data["truncation"] == {"count": 0, "continued": False}


@pytest.mark.asyncio
async def test_a_truncated_response_that_did_parse_a_tool_call_is_not_intercepted(tmp_path):
    """Defensive: `length` with usable tool calls stays on the normal path.

    The measured responses carried none, but a provider that emits a complete
    tool call and then runs out of room must not have that call thrown away.
    """
    class _Call:
        id = "call-1"
        name = "list_dir"
        arguments: dict = {}

        def to_openai_tool_call(self):
            return {"id": "1", "type": "function",
                    "function": {"name": "list_dir", "arguments": "{}"}}

    provider = ScriptedProvider([
        FakeResponse(content="", finish_reason="length", has_tool_calls=True, tool_calls=[_Call()]),
        FakeResponse(content="done", finish_reason="stop"),
    ])
    data, _ = await _run(tmp_path, provider)

    assert data["result"] == "done"
    # Intercepted responses never reach the tool executor, so a continuation
    # note in the second call's messages would prove the wrong branch ran.
    contents = [str(m.get("content") or "") for m in provider.seen_messages[1]]
    assert not any("cut off before it finished" in c for c in contents), contents
    assert data["truncation"] == {"count": 0, "continued": False}


@pytest.mark.asyncio
async def test_truncation_run_resets_after_a_clean_response(tmp_path):
    """`consecutive` bounds a RUN, not the cycle: two separate single
    truncations must not add up to an abort."""
    from nanobot.agent.subagent import _MAX_CONSECUTIVE_TRUNCATIONS

    assert _MAX_CONSECUTIVE_TRUNCATIONS >= 2
    provider = ScriptedProvider([
        FakeResponse(content="cut one", finish_reason="length"),
        FakeResponse(content="ok", finish_reason="stop"),
    ])
    data, _ = await _run(tmp_path, provider)

    assert data.get("stop_reason") is None
    assert data["truncation"]["count"] == 1
    assert data["truncation"]["continued"] is True
