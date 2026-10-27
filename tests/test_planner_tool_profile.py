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
    role = Path("roles/planner.md").read_text(encoding="utf-8")
    assert role.count("{{TOOL_CAPABILITIES}}") == 1
    prompt = role.replace("{{TOOL_CAPABILITIES}}", planner.allowed_actions_text())
    assert "read_file and list_dir" in prompt
    assert "absolute paths are accepted outside it" in prompt

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


def test_planner_registry_forbids_mutating_tools_without_side_effects(tmp_path, monkeypatch):
    captured = {}

    class Response:
        def __init__(self, tool_calls=()):
            self.content = ""
            self.finish_reason = "tool_calls" if tool_calls else "stop"
            self.has_tool_calls = bool(tool_calls)
            self.tool_calls = list(tool_calls)
            self.usage = {}
            self.reasoning_content = None
            self.thinking_blocks = None

    from nanobot.providers.base import ToolCallRequest

    forbidden_names = ("write_file", "edit_file", "exec", "search_memory", "spawn", "web_search", "web_fetch")
    forbidden_args = {
        "write_file": {"path": "side-effect.txt", "content": "bad"},
        "edit_file": {"path": "side-effect.txt", "old_text": "x", "new_text": "bad"},
        "exec": {"command": "python -c \\\"from pathlib import Path; Path('exec-marker').touch()\\\""},
        "search_memory": {"query": "probe"},
        "spawn": {"task": "bad"},
        "web_search": {"query": "probe"},
        "web_fetch": {"url": "https://example.invalid"},
    }
    forbidden_calls = [
        ToolCallRequest(id=f"call-{name}", name=name, arguments=forbidden_args[name])
        for name in forbidden_names
    ]

    class CapturingProvider(Provider):
        def __init__(self):
            self.calls = 0

        async def chat_with_retry(self, *, messages, tools, model):
            self.calls += 1
            captured["messages"] = messages
            captured["tools"] = tools
            if self.calls == 1:
                return Response(forbidden_calls)
            return Response()

    from nanobot.agent import subagent as subagent_module
    real_registry = subagent_module.ToolRegistry
    registries = []

    class CapturingRegistry(real_registry):
        def __init__(self):
            super().__init__()
            registries.append(self)

        async def execute(self, name, params):
            if name not in self.tool_names:
                return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"
            return await super().execute(name, params)

    planner = manager(
        tmp_path, tool_profile="planner",
        role_system_prompt=Path("roles/planner.md").read_text(encoding="utf-8"),
    )
    provider = CapturingProvider()
    planner.provider = provider
    monkeypatch.setattr(subagent_module, "ToolRegistry", CapturingRegistry)

    def forbidden_exec(*args, **kwargs):
        raise AssertionError("planner capability profile attempted to construct ExecTool")

    def forbidden_memory_search(*args, **kwargs):
        raise AssertionError("planner capability profile attempted to construct MemorySearchTool")

    monkeypatch.setattr(subagent_module, "ExecTool", forbidden_exec)
    monkeypatch.setattr(subagent_module, "MemorySearchTool", forbidden_memory_search)

    async def run_planner():
        task_id = await planner.spawn("plan")
        await asyncio.gather(*planner._running_tasks.values())
        return task_id

    task_id = asyncio.run(run_planner())
    assert task_id
    assert provider.calls >= 1
    assert len(registries) == 1
    registry = registries[0]
    names = tuple(item["function"]["name"] for item in captured["tools"])
    assert names == tuple(registry.tool_names) == ("read_file", "list_dir")
    prompt = captured["messages"][0]["content"]
    assert "{{TOOL_CAPABILITIES}}" not in prompt
    assert planner.allowed_actions_text() in prompt
    tool_results = [message.get("content", "") for message in captured["messages"] if message.get("role") == "tool"]
    assert len(tool_results) == len(forbidden_names)
    assert all("not found" in result for result in tool_results)
    assert not (tmp_path / "side-effect.txt").exists()
    assert not (tmp_path / "exec-marker").exists()
    assert not (tmp_path / "state" / "existence_index").exists()
    assert not any(name in registry.tool_names for name in forbidden_names)
    assert Path("roles/planner.md").read_text(encoding="utf-8").count("{{TOOL_CAPABILITIES}}") == 1


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
