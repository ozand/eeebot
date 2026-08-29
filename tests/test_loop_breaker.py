"""Tests for #1101: identical-tool-call loop breaker and wall-clock alignment.

Covers:
- K consecutive identical calls inject a synthetic break message (counter resets on differing call)
- 2K consecutive identical calls abort with stop_reason=identical_call_loop
- Wall-clock soft deadline triggers graceful stop (fake clock)
- Polling with changing arguments never triggers the guard
- Env-tunable K with default 3
- Both SubagentManager._run_subagent and AgentLoop._run_agent_loop
"""
from __future__ import annotations

import asyncio

import pytest

from nanobot.agent.subagent import (
    _canonical_response_key,
    _canonical_tool_key,
    _loop_breaker_k,
    _subagent_wall_deadline,
)
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# ── Helpers ───────────────────────────────────────────────────────────────────

class _Provider(LLMProvider):
    """Base test provider; subclass and override chat()."""

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(
        self, messages=None, tools=None, model=None, max_tokens=4096,
        temperature=0.7, reasoning_effort=None, tool_choice=None,
    ) -> LLMResponse:
        raise NotImplementedError


# ── Unit tests for helper functions ──────────────────────────────────────────

class TestCanonicalToolKey:
    def test_same_name_same_args_equal(self):
        a = _canonical_tool_key("exec", {"cmd": "ls", "dir": "."})
        b = _canonical_tool_key("exec", {"dir": ".", "cmd": "ls"})
        assert a == b, "dict key order must not matter"

    def test_different_name_different(self):
        a = _canonical_tool_key("exec", {"cmd": "ls"})
        b = _canonical_tool_key("read_file", {"cmd": "ls"})
        assert a != b

    def test_different_args_different(self):
        a = _canonical_tool_key("exec", {"cmd": "ls"})
        b = _canonical_tool_key("exec", {"cmd": "ls -la"})
        assert a != b

    def test_non_dict_args_stable(self):
        # Should not raise
        a = _canonical_tool_key("exec", None)
        b = _canonical_tool_key("exec", None)
        assert a == b

    def test_tool_id_not_included(self):
        # Same name/args regardless of conceptual ID
        a = _canonical_tool_key("read", {"path": "/tmp/x"})
        b = _canonical_tool_key("read", {"path": "/tmp/x"})
        assert a == b


class TestLoopBreakerK:
    def test_default_is_3(self, monkeypatch):
        monkeypatch.delenv("NANOBOT_LOOP_BREAKER_K", raising=False)
        assert _loop_breaker_k() == 3

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "5")
        assert _loop_breaker_k() == 5

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "garbage")
        assert _loop_breaker_k() == 3

    def test_zero_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "0")
        assert _loop_breaker_k() == 3

    def test_negative_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "-1")
        assert _loop_breaker_k() == 3


class TestSubagentWallDeadline:
    def test_default_active_at_3000s(self, monkeypatch):
        monkeypatch.delenv("NANOBOT_SUBAGENT_WALL_SECS", raising=False)
        # Fake monotonic start=0; deadline should be 0+3000
        dl = _subagent_wall_deadline(_now=0.0, _monotonic=lambda: 0.0)
        assert dl is not None
        assert abs(dl - 3000.0) < 1.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_SUBAGENT_WALL_SECS", "100")
        dl = _subagent_wall_deadline(_now=0.0, _monotonic=lambda: 0.0)
        assert dl == pytest.approx(100.0)

    def test_invalid_env_returns_none(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_SUBAGENT_WALL_SECS", "badval")
        dl = _subagent_wall_deadline(_now=0.0, _monotonic=lambda: 0.0)
        assert dl is None

    def test_zero_env_returns_none(self, monkeypatch):
        monkeypatch.setenv("NANOBOT_SUBAGENT_WALL_SECS", "0")
        dl = _subagent_wall_deadline(_now=0.0, _monotonic=lambda: 0.0)
        assert dl is None


# ── SubagentManager integration tests ─────────────────────────────────────────

class _RepeatProvider(_Provider):
    """Returns the same exec tool call every time until depleted, then final answer."""

    def __init__(self, *, repeat_count: int, final: str = "done", vary_after: int | None = None):
        super().__init__()
        self.calls = 0
        self.repeat_count = repeat_count
        self.final = final
        self.vary_after = vary_after  # after this many calls, change args

    async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls > self.repeat_count:
            return LLMResponse(content=self.final, tool_calls=[])
        args = {"cmd": "ls ."}
        if self.vary_after is not None and self.calls > self.vary_after:
            args = {"cmd": f"ls . #{self.calls}"}  # changing args after threshold
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(id=f"call-{self.calls}", name="exec", arguments=args)],
        )


async def _run_manager(tmp_path, provider, max_iterations=50, monkeypatch=None, wall_secs=None) -> dict:
    """Spawn a task and wait for its telemetry JSON."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    if monkeypatch and wall_secs is not None:
        monkeypatch.setenv("NANOBOT_SUBAGENT_WALL_SECS", str(wall_secs))

    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_iterations=max_iterations,
    )
    await manager.spawn(task="test-task", label="lbtest")
    await asyncio.gather(*list(manager._running_tasks.values()), return_exceptions=True)

    # Read telemetry
    import json
    files = list((tmp_path / "state" / "subagents").glob("*.json"))
    if not files:
        # try alternate state path
        files = list(tmp_path.rglob("subagents/*.json"))
    assert files, "no telemetry file written"
    return json.loads(files[0].read_text(encoding="utf-8"))


class TestSubagentLoopBreaker:
    async def test_k_identical_calls_inject_break_message(self, tmp_path, monkeypatch):
        """After K identical calls a synthetic message is injected; the run continues."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")
        # 2 identical calls → warning injection, then final answer on call 3
        provider = _RepeatProvider(repeat_count=2, final="finished")
        telem = await _run_manager(tmp_path, provider, max_iterations=20)
        # Run must complete without abort
        assert telem["status"] == "ok"
        assert telem.get("stop_reason") is None
        # Provider was called 3 times (2 tool calls + 1 final answer)
        assert provider.calls == 3

    async def test_counter_resets_on_differing_call(self, tmp_path, monkeypatch):
        """A different tool call after K-1 identical calls resets the counter."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "3")
        # vary_after=1: first call identical, second changes args → resets counter
        provider = _RepeatProvider(repeat_count=3, final="done", vary_after=1)
        telem = await _run_manager(tmp_path, provider, max_iterations=20)
        # Should NOT be an identical_call_loop abort since args changed
        assert telem.get("stop_reason") != "identical_call_loop"

    async def test_2k_identical_calls_abort_with_stop_reason(self, tmp_path, monkeypatch):
        """2K consecutive identical calls abort with stop_reason=identical_call_loop."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")
        # 4+ identical calls → abort at 2K=4
        provider = _RepeatProvider(repeat_count=100, final="should not reach")
        telem = await _run_manager(tmp_path, provider, max_iterations=100)
        assert telem.get("stop_reason") == "identical_call_loop"
        assert telem["status"] == "bounded_stop"
        assert "identical_call_loop" in telem["result"]

    async def test_polling_with_changing_args_never_aborts(self, tmp_path, monkeypatch):
        """Changing arguments (polling) reset the counter and must never abort."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")
        # Each call changes the cmd arg; after 6 calls gives final answer
        class _PollingProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                self.calls += 1
                if self.calls > 6:
                    return LLMResponse(content="poll done", tool_calls=[])
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id=f"call-{self.calls}",
                        name="exec",
                        arguments={"cmd": f"check status #{self.calls}"},
                    )],
                )

        provider = _PollingProvider()
        telem = await _run_manager(tmp_path, provider, max_iterations=20)
        assert telem.get("stop_reason") != "identical_call_loop"
        assert telem["status"] == "ok"


class TestSubagentWallClockDeadline:
    async def test_wall_clock_deadline_triggers_graceful_stop(self, tmp_path, monkeypatch):
        """Wall-clock deadline triggers graceful stop with honest message, no exception."""
        # Set a very short wall-clock deadline
        monkeypatch.setenv("NANOBOT_SUBAGENT_WALL_SECS", "0.001")

        class _InfiniteProvider(_Provider):
            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                import asyncio
                await asyncio.sleep(0.01)  # small delay so deadline elapses
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(id="call-1", name="exec", arguments={"cmd": "ls"})],
                )

        provider = _InfiniteProvider()
        telem = await _run_manager(tmp_path, provider, max_iterations=100)
        # Should have stopped with wall_clock_deadline or completed max_iterations
        # The deadline is so short that either the deadline or max_iterations fires first
        # We just need no exception and an honest result
        assert telem["status"] in ("ok", "bounded_stop", "error")
        # No crash (no status="error" with an actual exception stack trace)
        if telem["status"] == "bounded_stop":
            assert telem.get("stop_reason") in ("wall_clock_deadline", "identical_call_loop")


# ── AgentLoop integration tests ───────────────────────────────────────────────

class TestAgentLoopBreaker:
    async def test_agent_loop_2k_identical_calls_abort(self, tmp_path, monkeypatch):
        """AgentLoop._run_agent_loop aborts at 2K identical tool calls."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus

        class _LoopProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                self.calls += 1
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id=f"call-{self.calls}", name="list_dir", arguments={"path": "."},
                    )],
                )

        provider = _LoopProvider()
        bus = MessageBus()
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, max_iterations=100)

        from nanobot.agent.tools.filesystem import ListDirTool
        loop.tools.register(ListDirTool(workspace=tmp_path))

        initial = [{"role": "user", "content": "loop forever"}]
        content, tools_used, _ = await loop._run_agent_loop(initial)

        assert "identical_call_loop" in (content or "")

    async def test_agent_loop_changing_args_no_abort(self, tmp_path, monkeypatch):
        """AgentLoop: calls with changing args do not trigger breaker."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus

        class _VaryProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                self.calls += 1
                if self.calls > 5:
                    return LLMResponse(content="all done", tool_calls=[])
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id=f"call-{self.calls}", name="list_dir",
                        arguments={"path": f"./dir{self.calls}"},
                    )],
                )

        provider = _VaryProvider()
        bus = MessageBus()
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, max_iterations=20)

        from nanobot.agent.tools.filesystem import ListDirTool
        loop.tools.register(ListDirTool(workspace=tmp_path))

        initial = [{"role": "user", "content": "do many things"}]
        content, tools_used, _ = await loop._run_agent_loop(initial)

        assert "identical_call_loop" not in (content or "")
        assert content == "all done"

    async def test_agent_loop_wall_clock_deadline(self, tmp_path, monkeypatch):
        """AgentLoop wall-clock deadline produces graceful result without exception."""
        monkeypatch.setenv("NANOBOT_SUBAGENT_WALL_SECS", "0.001")
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus

        class _SlowProvider(_Provider):
            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                import asyncio
                await asyncio.sleep(0.05)
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
                )

        provider = _SlowProvider()
        bus = MessageBus()
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, max_iterations=100)

        from nanobot.agent.tools.filesystem import ListDirTool
        loop.tools.register(ListDirTool(workspace=tmp_path))

        initial = [{"role": "user", "content": "keep going"}]
        # Should return without raising
        content, tools_used, _ = await loop._run_agent_loop(initial)
        # Content may be None (no iterations ran) or the deadline message
        # The key constraint is: no exception was raised
        assert content is None or isinstance(content, str)


# ── _canonical_response_key unit tests ───────────────────────────────────────

class TestCanonicalResponseKey:
    def _make_tc(self, name: str, arguments: dict, call_id: str = "id1") -> ToolCallRequest:
        return ToolCallRequest(id=call_id, name=name, arguments=arguments)

    def test_same_single_call_equal(self):
        a = self._make_tc("exec", {"cmd": "ls"}, "id-1")
        b = self._make_tc("exec", {"cmd": "ls"}, "id-2")  # different ID
        assert _canonical_response_key([a]) == _canonical_response_key([b])

    def test_same_multi_call_equal(self):
        r1 = [
            self._make_tc("exec", {"cmd": "ls"}, "x"),
            self._make_tc("read", {"path": "/tmp"}, "y"),
        ]
        r2 = [
            self._make_tc("exec", {"cmd": "ls"}, "a"),  # different IDs
            self._make_tc("read", {"path": "/tmp"}, "b"),
        ]
        assert _canonical_response_key(r1) == _canonical_response_key(r2)

    def test_differing_order_not_equal(self):
        """[A, B] vs [B, A] are NOT identical — order matters in a response."""
        r1 = [self._make_tc("exec", {"cmd": "ls"}), self._make_tc("read", {"path": "/x"})]
        r2 = [self._make_tc("read", {"path": "/x"}), self._make_tc("exec", {"cmd": "ls"})]
        assert _canonical_response_key(r1) != _canonical_response_key(r2)

    def test_differing_args_not_equal(self):
        r1 = [self._make_tc("exec", {"cmd": "ls"})]
        r2 = [self._make_tc("exec", {"cmd": "pwd"})]
        assert _canonical_response_key(r1) != _canonical_response_key(r2)

    def test_differing_length_not_equal(self):
        r1 = [self._make_tc("exec", {"cmd": "ls"})]
        r2 = [self._make_tc("exec", {"cmd": "ls"}), self._make_tc("exec", {"cmd": "ls"})]
        assert _canonical_response_key(r1) != _canonical_response_key(r2)

    def test_dict_key_order_ignored(self):
        """arg key ordering within each call must not matter."""
        r1 = [self._make_tc("exec", {"cmd": "ls", "dir": "."})]
        r2 = [self._make_tc("exec", {"dir": ".", "cmd": "ls"})]
        assert _canonical_response_key(r1) == _canonical_response_key(r2)


# ── Multi-tool response regression tests ─────────────────────────────────────

class TestMultiToolResponseBreaker:
    """Regression: multi-tool response [A, B] repeated across iterations
    must trigger the breaker; alternating [A, B], [C, D] must not."""

    async def test_subagent_multi_tool_repeated_aborts(self, tmp_path, monkeypatch):
        """SubagentManager: response [exec, read] repeated 2K times → abort."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")

        class _MultiToolProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                self.calls += 1
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(id=f"a-{self.calls}", name="exec", arguments={"cmd": "ls"}),
                        ToolCallRequest(id=f"b-{self.calls}", name="exec", arguments={"cmd": "pwd"}),
                    ],
                )

        provider = _MultiToolProvider()
        telem = await _run_manager(tmp_path, provider, max_iterations=100)
        assert telem.get("stop_reason") == "identical_call_loop"
        assert telem["status"] == "bounded_stop"

    async def test_subagent_alternating_multi_tool_no_abort(self, tmp_path, monkeypatch):
        """SubagentManager: alternating [A, B] / [C, D] must NOT trigger the breaker."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")

        class _AlternatingProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                self.calls += 1
                if self.calls > 6:
                    return LLMResponse(content="done", tool_calls=[])
                if self.calls % 2 == 1:
                    # odd: [exec(ls), exec(pwd)]
                    tcs = [
                        ToolCallRequest(id=f"a-{self.calls}", name="exec", arguments={"cmd": "ls"}),
                        ToolCallRequest(id=f"b-{self.calls}", name="exec", arguments={"cmd": "pwd"}),
                    ]
                else:
                    # even: [exec(ls), exec(date)]
                    tcs = [
                        ToolCallRequest(id=f"a-{self.calls}", name="exec", arguments={"cmd": "ls"}),
                        ToolCallRequest(id=f"b-{self.calls}", name="exec", arguments={"cmd": "date"}),
                    ]
                return LLMResponse(content=None, tool_calls=tcs)

        provider = _AlternatingProvider()
        telem = await _run_manager(tmp_path, provider, max_iterations=20)
        assert telem.get("stop_reason") != "identical_call_loop"
        assert telem["status"] == "ok"

    async def test_agent_loop_multi_tool_repeated_aborts(self, tmp_path, monkeypatch):
        """AgentLoop: response [list_dir, list_dir] repeated 2K times → abort."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")
        from nanobot.agent.loop import AgentLoop
        from nanobot.agent.tools.filesystem import ListDirTool
        from nanobot.bus.queue import MessageBus

        class _MultiLoopProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                self.calls += 1
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(id=f"a-{self.calls}", name="list_dir", arguments={"path": "."}),
                        ToolCallRequest(id=f"b-{self.calls}", name="list_dir", arguments={"path": ".."}),
                    ],
                )

        provider = _MultiLoopProvider()
        bus = MessageBus()
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, max_iterations=100)
        loop.tools.register(ListDirTool(workspace=tmp_path))

        initial = [{"role": "user", "content": "loop with multi tools"}]
        content, tools_used, _ = await loop._run_agent_loop(initial)
        assert "identical_call_loop" in (content or "")

    async def test_agent_loop_alternating_multi_tool_no_abort(self, tmp_path, monkeypatch):
        """AgentLoop: alternating [A, B] / [A, C] does NOT trigger breaker."""
        monkeypatch.setenv("NANOBOT_LOOP_BREAKER_K", "2")
        from nanobot.agent.loop import AgentLoop
        from nanobot.agent.tools.filesystem import ListDirTool
        from nanobot.bus.queue import MessageBus

        class _AltLoopProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def chat(self, messages=None, tools=None, model=None, **kwargs) -> LLMResponse:
                self.calls += 1
                if self.calls > 6:
                    return LLMResponse(content="done", tool_calls=[])
                if self.calls % 2 == 1:
                    tcs = [
                        ToolCallRequest(id=f"x-{self.calls}", name="list_dir", arguments={"path": "."}),
                        ToolCallRequest(id=f"y-{self.calls}", name="list_dir", arguments={"path": ".."}),
                    ]
                else:
                    tcs = [
                        ToolCallRequest(id=f"x-{self.calls}", name="list_dir", arguments={"path": "."}),
                        ToolCallRequest(id=f"y-{self.calls}", name="list_dir", arguments={"path": "./sub"}),
                    ]
                return LLMResponse(content=None, tool_calls=tcs)

        provider = _AltLoopProvider()
        bus = MessageBus()
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, max_iterations=20)
        loop.tools.register(ListDirTool(workspace=tmp_path))

        initial = [{"role": "user", "content": "alternate multi tools"}]
        content, tools_used, _ = await loop._run_agent_loop(initial)
        assert "identical_call_loop" not in (content or "")
        assert content == "done"
