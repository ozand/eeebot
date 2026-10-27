"""ADR-044: the planner receives only its declared read/list profile."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.toolsets import EXECUTOR_PROFILE, PLANNER_PROFILE, ToolProfile
from nanobot.bus.queue import MessageBus


class Provider:
    def get_default_model(self):
        return "test-model"


def manager(tmp_path: Path, **kwargs) -> SubagentManager:
    return SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), **kwargs)


def test_planner_profile_declares_exact_tools_operations_and_existing_paths():
    profile = PLANNER_PROFILE
    assert profile.tool_names == ("read_file", "list_dir")
    assert profile.file_operations == ("read", "list")
    assert profile.file_path_policy == "existing_unrestricted_absolute_paths"
    assert profile.workspace_restricted is False
    text = profile.allowed_actions()
    assert "read_file and list_dir" in text
    assert "absolute paths are accepted outside it" in text
    assert all(word not in text for word in ("write_file", "edit_file", "exec", "search_memory", "spawn"))


def test_profile_is_source_for_registration_and_allowed_actions(tmp_path):
    planner = manager(tmp_path, tool_profile="planner", restrict_to_workspace=True)
    assert planner.registered_tool_names() == ("read_file", "list_dir")
    assert planner._allowed_dir is None  # profile wins over legacy true flag
    assert planner.allowed_actions_text() == PLANNER_PROFILE.allowed_actions()

    executor = manager(tmp_path, tool_profile="executor", restrict_to_workspace=True)
    assert executor.registered_tool_names() == EXECUTOR_PROFILE.tool_names
    assert executor._allowed_dir == tmp_path  # executor retains its legacy flag behavior

    default = manager(tmp_path, restrict_to_workspace=True)
    assert default.tool_profile is EXECUTOR_PROFILE
    assert default._allowed_dir == tmp_path
    assert default.registered_tool_names() == SubagentManager.declared_tool_names()


def test_invalid_profiles_fail_closed(tmp_path):
    for invalid in ("unknown", "", 3, [], {}, ToolProfile(
        name="other", operations_by_tool=(("read_file", "read"),),
        file_path_policy="existing_unrestricted_absolute_paths", workspace_restricted=False,
    )):
        with pytest.raises(ValueError):
            manager(tmp_path, tool_profile=invalid)


def test_planner_registry_forbids_mutating_tools_without_side_effects(tmp_path):
    captured = {}

    class Response:
        def __init__(self, tool_calls=()):
            self.content = ""
            self.finish_reason = "tool_calls" if tool_calls else "stop"
            self.has_tool_calls = bool(tool_calls)
            self.tool_calls = list(tool_calls)
            self.usage = {}

    class CapturingProvider(Provider):
        def __init__(self, responses):
            self.responses = iter(responses)

        async def chat_with_retry(self, *, messages, tools, model):
            captured["messages"] = messages
            captured["tools"] = tools
            return next(self.responses)

    planner = manager(tmp_path, tool_profile="planner", role_system_prompt="planner role")
    planner.provider = CapturingProvider([Response()])

    async def run_planner():
        task_id = await planner.spawn("plan")
        await asyncio.gather(*planner._running_tasks.values())
        return task_id

    task_id = asyncio.run(run_planner())
    assert task_id
    names = tuple(item["function"]["name"] for item in captured["tools"])
    assert names == ("read_file", "list_dir")
    prompt = captured["messages"][0]["content"]
    assert planner.allowed_actions_text() in prompt

    from nanobot.agent.tools.filesystem import ListDirTool, ReadFileTool
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.providers.base import ToolCallRequest

    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path))
    registry.register(ListDirTool(workspace=tmp_path))
    rejected = []

    calls = [
        ToolCallRequest(id=f"call-{name}", name=name, arguments={})
        for name in ("write_file", "edit_file", "exec", "search_memory", "spawn", "web_search", "web_fetch")
    ]

    async def attempt_forbidden_calls():
        for call in calls:
            result = await registry.execute(call.name, call.arguments)
            assert "not found" in result
            rejected.append(call.name)

    asyncio.run(attempt_forbidden_calls())
    assert set(rejected) == {"write_file", "edit_file", "exec", "search_memory", "spawn", "web_search", "web_fetch"}
    assert not (tmp_path / "state" / "existence_index").exists()


def test_planner_read_and_list_use_preserved_path_policy(tmp_path):
    from nanobot.agent.tools.filesystem import ListDirTool, ReadFileTool

    planner = manager(tmp_path, tool_profile="planner", restrict_to_workspace=True)
    external = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    external.write_text("outside-readable", encoding="utf-8")
    (tmp_path / "inside.txt").write_text("inside-readable", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path, allowed_dir=planner._allowed_dir)
    listing = ListDirTool(workspace=tmp_path, allowed_dir=planner._allowed_dir)
    assert "inside-readable" in asyncio.run(read.execute("inside.txt"))
    assert "outside-readable" in asyncio.run(read.execute(str(external)))
    assert "inside.txt" in asyncio.run(listing.execute(str(tmp_path)))
    assert "outside.txt" in asyncio.run(listing.execute(str(tmp_path.parent)))


def test_planner_bridge_wiring_selects_explicit_profile():
    import inspect

    from nanobot.runtime import bridge

    source = inspect.getsource(bridge._run_planning_session)
    assert "tool_profile='planner'" in source
    assert "restrict_to_workspace=" not in source
