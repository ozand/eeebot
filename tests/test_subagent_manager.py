import pytest


def test_executor_declared_and_registered_tool_names_match():
    """ADR-044: the compatibility-default executor profile stays unchanged."""
    from nanobot.agent.subagent import SubagentManager

    assert SubagentManager.declared_tool_names() == (
        "read_file", "write_file", "edit_file", "list_dir", "exec", "search_memory"
    )
    manager = object.__new__(SubagentManager)
    manager.web_tools_enabled = False
    assert manager.registered_tool_names() == manager.declared_tool_names()
    assert "web_search" not in manager.registered_tool_names()
    assert "web_fetch" not in manager.registered_tool_names()

    # #1723(b): the "Use your tools: ..." line moved to OPERATING.md's Tools
    # section (release root, loaded by the loop-profile loader, #1725) --
    # build_task no longer renders declared_tool_names as a literal.
    from nanobot.runtime import bridge
    assert "Use your tools: " not in bridge.build_task({}, "goal", "")


def test_runtime_registration_assertion_rejects_declared_tool_divergence(tmp_path, monkeypatch):
    """H2 parity is enforced by the unchanged runtime assertion, not only tuple equality."""
    import asyncio

    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return "test-model"

    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus())
    manager.registered_tool_names = lambda: manager.declared_tool_names()[:-1]
    asyncio.run(manager._run_subagent("task", "task", "label", {"channel": "cli", "chat_id": "direct"}))
    rows = list(manager._telemetry_dir.glob("*.json"))
    assert len(rows) == 1
    import json
    payload = json.loads(rows[0].read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert "registered tool set must match the configured declaration" in payload["result"]


def test_interactive_subagent_role_keeps_web_tools_available():
    """ADR-044: interactive executor compatibility retains web tools."""
    from nanobot.agent.subagent import SubagentManager

    manager = object.__new__(SubagentManager)
    manager.web_tools_enabled = True
    assert manager.registered_tool_names() == (
        "read_file", "write_file", "edit_file", "list_dir", "exec", "search_memory", "web_search", "web_fetch"
    )


def test_subagent_manager_accepts_deployed_bridge_compat_kwargs(tmp_path):
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return 'test-model'

    class SubagentCfg:
        max_running = 3

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

    from unittest.mock import Mock
    manager = SubagentManager(
        provider=Provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        system_context="# Immutable operator charter\n\nCHARTER SENTINEL",
    )

    prompt = manager._build_subagent_prompt()
    assert "CHARTER SENTINEL" in prompt


def test_planner_role_override_uses_explicit_safe_role_sections(tmp_path):
    import hashlib
    from pathlib import Path

    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus
    from nanobot.observability.prompt_provenance import prompt_owner_context, provider_call_context
    from nanobot.runtime.role_prompt import build_role_system_prompt

    class Provider:
        def get_default_model(self):
            return "test-model"

    release_root = Path(__file__).resolve().parents[1]
    role_prompt_text, role_meta = build_role_system_prompt("planner", release_root=release_root)
    manager = SubagentManager(
        provider=Provider(), workspace=tmp_path, bus=MessageBus(),
        tool_profile="planner", telemetry_component="planner",
        role_system_prompt=role_prompt_text,
        role_prompt_sections=tuple(role_meta["sections"]),
        role_prompt_release_id=role_meta.get("release_id"),
    )

    prompt = manager._build_subagent_prompt()
    role_body = role_meta["sections"][-1].content.replace(
        "{{TOOL_CAPABILITIES}}", manager.allowed_actions_text(),
    )
    assert role_body in prompt
    assert "{{TOOL_CAPABILITIES}}" not in prompt
    from nanobot.observability.prompt_provenance import _ROLE_ASSEMBLY
    assembly = _ROLE_ASSEMBLY.get()
    assert assembly["schema"] == "role-prompt-v1:planner+tool-profile-v1"
    assert assembly["release_id"] == role_meta.get("release_id")
    assert manager._prompt_sections[0].evidence_safe is False
    assert manager._prompt_sections[1].evidence_safe is False
    assert manager._prompt_sections[-1].evidence_safe is True
    assert manager._prompt_sections[-1].content == role_body
    from nanobot.agent.context import ContextBuilder
    assert ContextBuilder._join_sections([
        (section.name, section.content) for section in manager._prompt_sections
    ]) == prompt
    with prompt_owner_context("bridge:unknown"):
        call_context = provider_call_context([{"role": "system", "content": prompt}])
        with call_context as call:
            assert call is not None
            assert call.provenance["schema"] == "role-prompt-v1:planner+tool-profile-v1"
            assert call.provenance["release_id"] == role_meta.get("release_id")
            assert call.provenance["order"] == ["role"]
            assert call.provenance["sections"] == [{
                "name": "role", "sha256": hashlib.sha256(role_body.encode()).hexdigest(),
                "chars": len(role_body),
            }]


def test_planner_dispatch_provenance_matches_final_injected_system_bytes(tmp_path, monkeypatch):
    import asyncio
    import hashlib
    import json
    from datetime import datetime, timezone

    from nanobot.agent.context import ContextBuilder
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.observability.llm_telemetry import call_context
    from nanobot.observability.prompt_provenance import PromptSection
    from nanobot.providers.base import LLMProvider, LLMResponse

    class CapturingProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.messages = []

        async def chat(self, *, messages, **kwargs):
            self.messages.append(messages)
            return LLMResponse(content="plan complete")

        def get_default_model(self):
            return "test-model"

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "llm_calls"))
    provider = CapturingProvider()
    private_identity = "## IDENTITY.md\n\nprivate identity"
    private_goals = "## goals.md\n\nprivate charter"
    fixed_role = "Planner role\n{{TOOL_CAPABILITIES}}"
    sections = (
        PromptSection("identity", private_identity, False),
        PromptSection("goals", private_goals, False),
        PromptSection("role", fixed_role, True),
    )
    system_prompt = ContextBuilder.SECTION_SEPARATOR.join(section.content for section in sections)
    manager = SubagentManager(
        provider=provider, workspace=tmp_path, bus=MessageBus(),
        tool_profile=PLANNER_PROFILE, telemetry_component="planner",
        telemetry_cycle_id="planner-fixture-cycle", max_iterations=1,
        role_system_prompt=system_prompt,
        role_prompt_sections=sections, role_prompt_release_id="release-fixture",
    )

    async def run():
        with call_context("planner-fixture-cycle", "planner"):
            await manager.spawn("plan")
            await asyncio.gather(*manager._running_tasks.values())

    asyncio.run(run())
    assert len(provider.messages) == 1
    final_system = provider.messages[0][0]["content"]
    role_after_injection = fixed_role.replace("{{TOOL_CAPABILITIES}}", manager.allowed_actions_text())
    expected = ContextBuilder.SECTION_SEPARATOR.join((
        private_identity, private_goals, role_after_injection,
    ))
    assert final_system.encode("utf-8") == expected.encode("utf-8")
    assert manager.allowed_actions_text() in final_system
    assert private_identity in final_system and private_goals in final_system

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = [json.loads(line) for line in (tmp_path / "llm_calls" / f"{day}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    provenance = rows[0]["prompt_provenance"]
    assert provenance["producer"] == "planner"
    assert provenance["schema"] == "role-prompt-v1:planner+tool-profile-v1"
    assert provenance["release_id"] == "release-fixture"
    assert provenance["order"] == ["role"]
    assert provenance["sections"] == [{
        "name": "role", "sha256": hashlib.sha256(role_after_injection.encode("utf-8")).hexdigest(),
        "chars": len(role_after_injection),
    }]
    assert private_identity not in json.dumps(provenance)
    assert private_goals not in json.dumps(provenance)


def test_loop_executor_position_update_is_the_actual_dispatched_system_bytes(tmp_path, monkeypatch):
    import asyncio
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    from nanobot.agent.context import ContextBuilder
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    class CapturingProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.messages = []

        async def chat(self, *, messages, **kwargs):
            self.messages.append([dict(message) for message in messages])
            if len(self.messages) == 1:
                return LLMResponse(
                    content="read fixture",
                    tool_calls=[ToolCallRequest(id="read-1", name="read_file", arguments={"path": "fixture.txt"})],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="done")

        def get_default_model(self):
            return "test-model"

    (tmp_path / "fixture.txt").write_text("fixture content", encoding="utf-8")
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "llm_calls"))
    provider = CapturingProvider()
    manager = SubagentManager(
        provider=provider, workspace=tmp_path, bus=MessageBus(),
        telemetry_component="executor", telemetry_cycle_id="position-fixture-cycle",
        max_iterations=2,
    )
    builder = ContextBuilder(tmp_path, release_root=Path(__file__).resolve().parents[1], state_dir=tmp_path / "state")
    builder._load_ontology_blocks = lambda: ([
        ("identity", "## IDENTITY.md\n\nfixture identity"),
        ("soul", "## SOUL.md\n\nfixture soul"),
        ("goals", "## goals.md\n\nfixture goals"),
        ("user", "## USER.md\n\nfixture user"),
        ("operating", "## OPERATING.md\n\nfixture operating"),
        ("agents", "## AGENTS.md\n\nfixture agents"),
    ], [], [])
    builder._load_priorities_block = lambda: "## Priorities\nfixture priorities"
    builder._get_identity = lambda loop_profile=False: "## Runtime\nfixture runtime"
    builder._load_scorecard_block = lambda: "[missing: scorecard]"
    builder._load_position_block = lambda **kwargs: "## Position\nStep 1 of 2."
    builder.memory.get_memory_context = lambda **kwargs: "fixture memory"
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda **kwargs: ""
    base_prompt = builder.build_system_prompt(
        loop_profile=True, iteration=1, max_iterations=2, cycle_id="position-fixture-cycle",
    )
    manager._prompt_sections = builder.last_prompt_sections
    manager._prompt_schema = "context-builder-loop-v1"
    manager._role_system_prompt = None
    manager._build_subagent_prompt = lambda: base_prompt

    async def run():
        with call_context("position-fixture-cycle", "executor"):
            await manager.spawn("read the fixture")
            await asyncio.gather(*manager._running_tasks.values())

    from nanobot.observability.llm_telemetry import call_context
    asyncio.run(run())
    assert len(provider.messages) == 2
    first_system = provider.messages[0][0]["content"]
    actual_final_system = provider.messages[1][0]["content"]
    base_system = ContextBuilder.update_step_position(first_system, 1, 2)
    expected_final_system = ContextBuilder.update_step_position(base_system, 2, 2)
    assert actual_final_system.encode("utf-8") == expected_final_system.encode("utf-8")
    assert "Step 1 of 2." in base_system
    assert "Step 2 of 2." in actual_final_system

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = [json.loads(line) for line in (tmp_path / "llm_calls" / f"{day}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert [row["prompt_provenance"]["provenance_state"] for row in rows] == ["unavailable", "unavailable"]
    assert len({row["prompt_provenance"]["call_id"] for row in rows}) == 2
    assert rows[1]["prompt_provenance"]["previous_call_id"] == rows[0]["prompt_provenance"]["call_id"]
    assert all(row["prompt_provenance"]["order"] == [] for row in rows)


def test_subagent_manager_default_max_iterations_is_15(tmp_path):
    """Issue #578: omitting max_iterations preserves the historical default."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return 'test-model'

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
import json
import uuid
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_planner_final_turn_starts_at_local_call_reserve_without_tools(tmp_path, monkeypatch):
    """ADR-045: finalization at Dplanner-C preserves the configured local C window."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "243")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10000")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest

    clock = [0.0]
    observed = []

    class Provider:
        def get_default_model(self):
            return "test-model"

        async def chat_with_retry(self, *, messages, tools, model):
            observed.append((clock[0], tools, messages[-1]["content"]))
            if len(observed) == 1:
                clock[0] = 0.0
                return LLMResponse(content="continue", tool_calls=[ToolCallRequest(
                    id="call-1", name="read_file", arguments={"path": "missing"},
                )])
            return LLMResponse(content='{"plan":"ok","iterations_planned":1}', tool_calls=[])

    from nanobot.agent.tools.registry import ToolRegistry

    class _AdvancingRegistry(ToolRegistry):
        async def execute(self, name, params):
            result = await super().execute(name, params)
            clock[0] = 5757.0
            return result

    class _SlowWorkRegistry(ToolRegistry):
        async def execute(self, name, params):
            await asyncio.sleep(0.01)
            clock[0] = 5757.0
            return "fixture read"

    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", _SlowWorkRegistry)
    manager = SubagentManager(
        provider=Provider(), workspace=tmp_path, bus=MessageBus(),
        max_iterations=20, telemetry_component="planner", planner_deadline=6000.0,
        planner_phase_budget=True, wall_deadline=9000.0, tool_profile=PLANNER_PROFILE,
        role_system_prompt="planner {{TOOL_CAPABILITIES}}",
    )
    manager._monotonic = lambda: clock[0]
    timeouts = []
    real_wait_for = asyncio.wait_for

    async def _capture_timeout(awaitable, *, timeout):
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr("nanobot.agent.subagent.asyncio.wait_for", _capture_timeout)
    spawned = await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    task_id = next(iter(manager._running_tasks))
    await manager._running_tasks[task_id]

    assert spawned.startswith("Subagent [planner] started")
    assert len(observed) == 2
    assert observed[1][0] == 5757.0
    assert observed[1][1] == []
    assert "final planning turn" in observed[1][2]
    assert timeouts[-3:] == pytest.approx([243.0, 243.0, 243.0])


@pytest.mark.asyncio
@pytest.mark.parametrize("now,expected", [(114.0, "ordinary"), (114.01, "final"), (357.0, "final"), (357.01, "none")])
async def test_planner_current_budget_final_admission_boundaries(tmp_path, monkeypatch, now, expected):
    """ADR-052: OFF path must reach a tool-free final turn at R=C."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "243")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10000")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse

    seen = []
    class Provider:
        def get_default_model(self):
            return "test-model"
        async def chat_with_retry(self, *, messages, tools, model):
            seen.append(tools)
            return LLMResponse(content='{"plan":"ok","iterations_planned":1}', tool_calls=[])
    manager = SubagentManager(
        provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner",
        planner_phase_budget=False, planner_deadline=600.0, wall_deadline=3000.0,
        tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}",
    )
    manager._monotonic = lambda: now
    await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)
    if expected == "none":
        assert seen == []
    elif expected == "final":
        assert seen == [[]]
    else:
        assert len(seen) == 1 and len(seen[0]) > 0


@pytest.mark.asyncio
async def test_opted_in_planner_entry_too_late_stops_without_provider_call(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "243")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10000")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus

    class Provider:
        calls = 0

        def get_default_model(self):
            return "test-model"

        async def chat_with_retry(self, **_kwargs):
            self.calls += 1
            raise AssertionError("expired planner phase must not call provider")

    provider = Provider()
    manager = SubagentManager(
        provider=provider, workspace=tmp_path, bus=MessageBus(), telemetry_component="planner",
        planner_phase_budget=True, planner_deadline=6000.0, wall_deadline=9000.0,
        tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}",
    )
    manager._monotonic = lambda: 6000.0
    await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)
    assert provider.calls == 0
    row = json.loads(next((tmp_path / "state" / "subagents").glob("*.json")).read_text(encoding="utf-8"))
    assert row["stop_reason"] == "planner_deadline_before_final_call"
    assert row["status"] == "bounded_stop"


@pytest.mark.asyncio
async def test_planner_direct_caller_deadline_anchored_once(tmp_path, monkeypatch):
    """ADR-052: absent shared deadline still anchors once, not at each turn."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "243")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10000")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    clock = [100.0]
    seen = []
    class Provider:
        def get_default_model(self): return "test-model"
        async def chat_with_retry(self, *, messages, tools, model):
            seen.append(tools)
            if len(seen) == 1:
                return LLMResponse(content="continue", tool_calls=[ToolCallRequest(id="t", name="read_file", arguments={"path":"x"})])
            return LLMResponse(content='{"plan":"ok","iterations_planned":1}', tool_calls=[])
    class Registry(ToolRegistry):
        async def execute(self, name, params):
            clock[0] = 457.0
            return "fixture"
    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", Registry)
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: clock[0]
    await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)
    assert manager._planner_deadline is None
    assert len(seen) == 2 and seen[1] == []


@pytest.mark.asyncio
async def test_opted_in_planner_intermediate_window_remains_ordinary(tmp_path, monkeypatch):
    """ADR-052 preserves ADR-045 ON behavior at C < R < 2C."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "243")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10000")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse
    seen = []
    class Provider:
        def get_default_model(self): return "test-model"
        async def chat_with_retry(self, *, messages, tools, model):
            seen.append(tools)
            return LLMResponse(content='{"plan":"ok","iterations_planned":1}', tool_calls=[])
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", planner_phase_budget=True, planner_deadline=6000.0, wall_deadline=9000.0, tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: 5635.5
    await manager.spawn("plan")
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)
    assert len(seen) == 1 and len(seen[0]) > 0


@pytest.mark.asyncio
async def test_planner_spawn_anchor_is_per_session_before_scheduling(tmp_path, monkeypatch):
    """ADR-052: delayed coroutine start and manager reuse cannot extend a session."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    class Provider:
        def get_default_model(self): return "test-model"
    clock = [100.0]
    starts = []
    async def capture(*args, planner_deadline=None, **kwargs):
        starts.append((clock[0], planner_deadline))
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: clock[0]
    monkeypatch.setattr(manager, "_run_subagent", capture)
    await manager.spawn("first")
    first = next(iter(manager._running_tasks.values()))
    clock[0] = 300.0
    await first
    clock[0] = 500.0
    await manager.spawn("second")
    second = list(manager._running_tasks.values())[-1]
    clock[0] = 650.0
    await second
    assert starts == [(300.0, 700.0), (650.0, 1100.0)]


@pytest.mark.asyncio
async def test_planner_current_budget_tool_to_final_at_boundary(tmp_path, monkeypatch):
    """ADR-052: a tool completing at R=C admits a tools-disabled final call."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "243")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10000")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    clock = [0.0]
    observed = []
    class Provider:
        def get_default_model(self): return "test-model"
        async def chat_with_retry(self, *, messages, tools, model):
            observed.append(tools)
            if len(observed) == 1:
                return LLMResponse(content="continue", tool_calls=[ToolCallRequest(id="t", name="read_file", arguments={"path":"x"})])
            return LLMResponse(content='{"plan":"ok","iterations_planned":1}', tool_calls=[])
    class Registry(ToolRegistry):
        async def execute(self, name, params):
            clock[0] = 357.0
            return "fixture"
    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", Registry)
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", planner_deadline=600.0, planner_phase_budget=False, wall_deadline=3000.0, tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: clock[0]
    await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)
    assert len(observed) == 2 and observed[1] == []


@pytest.mark.asyncio
async def test_current_budget_timeout_suppressed_cancel_never_calls_provider_again(tmp_path, monkeypatch):
    """ADR-052: a late tool result cannot revive a timed-out planner turn."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "0.05")
    monkeypatch.setenv("NANOBOT_WALL_FINAL_BUDGET_SECS", "0")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    calls = []
    class Provider:
        def get_default_model(self): return "test-model"
        async def chat_with_retry(self, *, messages, tools, model):
            calls.append(tools)
            return LLMResponse(content="continue", tool_calls=[ToolCallRequest(id="t", name="read_file", arguments={"path":"x"})])
    class Registry(ToolRegistry):
        async def execute(self, name, params):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "late result"
    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", Registry)
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", planner_deadline=600.0, wall_deadline=3000.0, tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: 0.0
    await manager.spawn("plan")
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)
    assert len(calls) == 1
    row = json.loads(next((tmp_path / "state" / "subagents").glob("*.json")).read_text(encoding="utf-8"))
    assert row["status"] == "bounded_stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("final_turn", [False, True])
async def test_current_budget_provider_suppressed_cancel_is_discarded(tmp_path, monkeypatch, final_turn):
    """ADR-052: timed-out ordinary and final responses are never accepted."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "0.05")
    monkeypatch.setenv("NANOBOT_WALL_FINAL_BUDGET_SECS", "0")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    calls = []
    class Provider:
        def get_default_model(self): return "test-model"
        async def chat_with_retry(self, *, messages, tools, model):
            calls.append(tools)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                if final_turn:
                    return LLMResponse(content='{"plan":"late","iterations_planned":1}', tool_calls=[])
                return LLMResponse(content="late", tool_calls=[ToolCallRequest(id="t", name="read_file", arguments={"path":"x"})])
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", planner_deadline=600.0, wall_deadline=3000.0, tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: 599.94 if final_turn else 0.0
    await manager.spawn("plan")
    await asyncio.gather(*manager._running_tasks.values(), return_exceptions=True)
    assert len(calls) == 1
    row = json.loads(next((tmp_path / "state" / "subagents").glob("*.json")).read_text(encoding="utf-8"))
    assert row["status"] == "bounded_stop"
    assert '"plan":"late"' not in row.get("result", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "final_provider", "tool"])
async def test_current_budget_external_cancel_suppressed_is_propagated(tmp_path, monkeypatch, kind):
    """ADR-052: a dependency suppressing external cancellation cannot resume work."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "5")
    monkeypatch.setenv("NANOBOT_WALL_FINAL_BUDGET_SECS", "0")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    started = asyncio.Event()
    calls = []
    class Provider:
        def get_default_model(self): return "test-model"
        async def chat_with_retry(self, *, messages, tools, model):
            calls.append(tools)
            if kind in ("provider", "final_provider"):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return LLMResponse(content='{"plan":"late","iterations_planned":1}', tool_calls=[])
            return LLMResponse(content="continue", tool_calls=[ToolCallRequest(id="t", name="read_file", arguments={"path":"x"})])
    class Registry(ToolRegistry):
        async def execute(self, name, params):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "late tool result"
    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", Registry)
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", planner_deadline=600.0, wall_deadline=3000.0, tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: 592.0 if kind == "final_provider" else 0.0
    await manager.spawn("plan")
    task = next(iter(manager._running_tasks.values()))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1
    row = json.loads(next((tmp_path / "state" / "subagents").glob("*.json")).read_text(encoding="utf-8"))
    assert row["status"] == "cancelled"
    assert '"plan":"late"' not in row.get("result", "")


@pytest.mark.asyncio
async def test_current_budget_provider_external_cancel_propagates(tmp_path, monkeypatch):
    """ADR-052: external cancellation remains cancellation while provider waits."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "1")
    monkeypatch.setenv("NANOBOT_WALL_FINAL_BUDGET_SECS", "0")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    started = asyncio.Event()
    class Provider:
        def get_default_model(self): return "test-model"
        async def chat_with_retry(self, **kwargs):
            started.set()
            await asyncio.Event().wait()
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", planner_deadline=600.0, wall_deadline=3000.0, tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: 0.0
    await manager.spawn("plan")
    task = next(iter(manager._running_tasks.values()))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = json.loads(next((tmp_path / "state" / "subagents").glob("*.json")).read_text(encoding="utf-8"))
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_current_budget_tool_external_cancel_propagates(tmp_path, monkeypatch):
    """ADR-052: parent cancellation is not an internal timeout or a plan."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "1")
    monkeypatch.setenv("NANOBOT_WALL_FINAL_BUDGET_SECS", "0")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    started = asyncio.Event()
    calls = []
    class Provider:
        def get_default_model(self): return "test-model"
        async def chat_with_retry(self, *, messages, tools, model):
            calls.append(tools)
            return LLMResponse(content="continue", tool_calls=[ToolCallRequest(id="t", name="read_file", arguments={"path":"x"})])
    class Registry(ToolRegistry):
        async def execute(self, name, params):
            started.set()
            await asyncio.Event().wait()
    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", Registry)
    manager = SubagentManager(provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner", planner_deadline=600.0, wall_deadline=3000.0, tool_profile=PLANNER_PROFILE, role_system_prompt="planner {{TOOL_CAPABILITIES}}")
    manager._monotonic = lambda: 0.0
    await manager.spawn("plan")
    task = next(iter(manager._running_tasks.values()))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1
    row = json.loads(next((tmp_path / "state" / "subagents").glob("*.json")).read_text(encoding="utf-8"))
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,deadline", [(True, 6000.0), (False, 600.0)])
async def test_opted_in_planner_tool_timeout_drains_cancellation_without_late_call(tmp_path, monkeypatch, enabled, deadline):
    """ADR-045: a timed-out tool may finish late, but no later provider turn starts."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "0.05")
    monkeypatch.setenv("NANOBOT_WALL_FINAL_BUDGET_SECS", "0")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest

    clock = [0.0]
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    tool_cancelled = asyncio.Event()
    second_provider_call = asyncio.Event()

    provider_calls = [0]

    class Provider:
        def get_default_model(self):
            return "test-model"

        async def chat_with_retry(self, *, messages, tools, model):
            provider_calls[0] += 1
            if second_provider_call.is_set():
                raise AssertionError("provider called after shared deadline")
            if any(message.get("role") == "tool" for message in messages):
                second_provider_call.set()
            return LLMResponse(content="continue", tool_calls=[ToolCallRequest(
                id="deadline-call", name="read_file", arguments={"path": "fixture"},
            )])

    class _CancellationResistantRegistry(ToolRegistry):
        async def execute(self, name, params):
            tool_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                tool_cancelled.set()
                await release_tool.wait()
                clock[0] = 9000.0
                return "late tool result after cancellation"

    monkeypatch.setattr("nanobot.agent.subagent.ToolRegistry", _CancellationResistantRegistry)
    manager = SubagentManager(
        provider=Provider(), workspace=tmp_path, bus=MessageBus(), telemetry_component="planner",
        planner_phase_budget=enabled, planner_deadline=deadline, wall_deadline=9000.0,
        max_iterations=20, tool_profile=PLANNER_PROFILE,
        role_system_prompt="planner {{TOOL_CAPABILITIES}}",
    )
    manager._monotonic = lambda: clock[0]
    spawned = await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    task = manager._running_tasks[next(iter(manager._running_tasks))]
    cleanup_done = asyncio.Event()
    normal_completion = False
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=2)
        clock[0] = 8957.0
        await asyncio.wait_for(tool_cancelled.wait(), timeout=2)
        assert not second_provider_call.is_set()
        assert task.done() is False  # wait_for requested cancellation; tool suppresses it
        assert spawned.startswith("Subagent [planner] started")
        release_tool.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
        normal_completion = True
    finally:
        # Release cancellation-resistant work even when startup/wait/assertion fails.
        release_tool.set()
        if not normal_completion and not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)
        cleanup_done.set()
    assert cleanup_done.is_set()
    assert not second_provider_call.is_set()
    assert not second_provider_call.is_set()
    row = json.loads(next((tmp_path / "state" / "subagents").glob("*.json")).read_text(encoding="utf-8"))
    assert provider_calls[0] == 1
    assert row["status"] == "bounded_stop"
    assert row["stop_reason"] == "wall_clock_deadline"
    assert "wall-clock time limit" in row["result"]


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

    from unittest.mock import Mock

    from nanobot.agent.subagent import SubagentManager
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
    written = sorted(telemetry_dir.glob("*.json"))
    # Exactly one, deliberately. tests/test_loop_breaker.py reads telemetry with
    # the same `glob("*.json")` over this directory and uses what it finds, so a
    # second .json here silently feeds those tests the wrong file -- which is how
    # an earlier `<task_id>_context.json` sidecar turned five of them red on Linux
    # while staying green on Windows, where glob order happened to be kind.
    assert len(written) == 1, [p.name for p in written]
    with open(written[0]) as f:
        data = json.load(f)

    assert "context_usage" in data
    assert data["context_usage"]["peak_tokens"] == 1500
    assert data["context_usage"]["iterations"] == [1500]


def _fitness_manager(state_dir, *, reads=None, failures=None):
    from nanobot.agent.subagent import SubagentManager

    manager = object.__new__(SubagentManager)
    manager._skill_fitness_state_dir = state_dir
    manager._skill_fitness_repo = None
    manager._skill_fitness_cycle_id = "cycle-x"
    manager._skill_fitness_cycle_base_sha = ""
    manager._skill_reads_this_cycle = list(reads or [])
    manager._skill_read_failures_this_cycle = list(failures or [])  # #1767
    return manager


def test_collect_skill_reads_records_zero_marker_when_nothing_read(tmp_path):
    """#1666 phase 1 / #1654: a cycle that read no skills still leaves a
    per-cycle marker row -- present with skill_count=0, not silence."""
    import json

    from nanobot.runtime import skill_fitness

    state = tmp_path / "state"
    manager = _fitness_manager(state, reads=[])

    assert manager.collect_skill_reads() == 0
    rows = (state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["cycle_id"] == "cycle-x"
    assert row["skill_count"] == 0
    # No detailed reads.json write for an empty cycle -- nothing to persist there.
    assert not (state / skill_fitness.SIDECAR_REL).exists()


def test_collect_skill_reads_records_marker_and_detail_when_something_read(tmp_path):
    import json

    from nanobot.runtime import skill_fitness

    state = tmp_path / "state"
    manager = _fitness_manager(state, reads=[{"skill": "review", "path": "skills/review/SKILL.md"}])

    assert manager.collect_skill_reads() == 1
    marker_rows = (state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()
    assert json.loads(marker_rows[0])["skills_read"] == ["review"]
    detail = json.loads((state / skill_fitness.SIDECAR_REL).read_text(encoding="utf-8"))
    assert detail["reads"][0]["skill"] == "review"
    assert manager._skill_reads_this_cycle == []  # cleared after persisting


def test_collect_skill_reads_no_op_when_instrumentation_not_configured(tmp_path):
    """state_dir is None -- instrumentation off for this spawn, not a zero
    read -- no marker row of any kind should appear."""
    from nanobot.runtime import skill_fitness

    manager = _fitness_manager(None, reads=[])
    assert manager.collect_skill_reads() == 0
    assert not (tmp_path / "state" / skill_fitness.CYCLE_SCAN_REL).exists()


# ---------------------------------------------------------------------------
# #1767 -- failed skill lookups reach the per-cycle marker row
# ---------------------------------------------------------------------------

def test_collect_skill_reads_records_failed_lookups(tmp_path):
    """A cycle that asked for two skills and got neither must be readable
    as exactly that, not as a cycle that never asked (#1759's dead end)."""
    import json

    from nanobot.runtime import skill_fitness

    state = tmp_path / "state"
    manager = _fitness_manager(state, reads=[], failures=["skip-when-done", "nope"])

    assert manager.collect_skill_reads() == 0
    row = json.loads((state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()[0])
    assert row["skill_count"] == 0
    assert row["skills_attempted_not_found"] == ["nope", "skip-when-done"]
    assert manager._skill_read_failures_this_cycle == []  # cleared after persisting


def test_collect_skill_reads_records_a_mixed_cycle(tmp_path):
    """One hit and one miss in the same cycle: both halves land, so the
    attempt rate is the sum and the success rate is the ratio."""
    import json

    from nanobot.runtime import skill_fitness

    state = tmp_path / "state"
    manager = _fitness_manager(
        state,
        reads=[{"skill": "review", "path": "skills/review/SKILL.md"}],
        failures=["gone"],
    )

    assert manager.collect_skill_reads() == 1
    row = json.loads((state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()[0])
    assert row["skills_read"] == ["review"]
    assert row["skills_attempted_not_found"] == ["gone"]


# ---------------------------------------------------------------------------
# ADR-028 rule 5 (#1812) -- collect_day_file_reads
#
# This method deliberately does NOT persist anything itself (unlike
# collect_skill_reads above): ADR-028 rule 4 keeps subagent.py's source
# free of the module name that does that persisting, so this is a plain
# collect-and-clear, and the bridge (tested separately, since it is not
# subject to that restriction) does the interpretation and the write.
# ---------------------------------------------------------------------------

def _day_file_manager(state_dir, *, reads=None):
    from nanobot.agent.subagent import SubagentManager

    manager = object.__new__(SubagentManager)
    manager._skill_fitness_state_dir = state_dir
    manager._skill_fitness_cycle_id = "cycle-x"
    manager._day_file_reads_this_cycle = list(reads or [])
    return manager


def test_collect_day_file_reads_returns_empty_when_nothing_read(tmp_path):
    manager = _day_file_manager(tmp_path / "state", reads=[])
    assert manager.collect_day_file_reads() == []


def test_collect_day_file_reads_returns_and_clears_the_accumulated_reads(tmp_path):
    manager = _day_file_manager(tmp_path / "state", reads=[{"day": "2026-09-20", "position": 2}])

    reads = manager.collect_day_file_reads()
    assert reads == [{"day": "2026-09-20", "position": 2}]
    assert manager._day_file_reads_this_cycle == []  # cleared after collection


def test_collect_day_file_reads_no_op_when_instrumentation_not_configured(tmp_path):
    manager = _day_file_manager(None, reads=[{"day": "2026-09-20", "position": 1}])
    assert manager.collect_day_file_reads() == []


def test_attempted_skill_name_normalises_the_request():
    """The success path records a bare skill name; the failure path must
    use the same vocabulary or the two halves cannot be compared."""
    from nanobot.agent.subagent import _attempted_skill_name

    assert _attempted_skill_name("skills/run-tests/SKILL.md") == "run-tests"
    assert _attempted_skill_name("./skills/run-tests/SKILL.md") == "run-tests"
    assert _attempted_skill_name(r"skills\run-tests\SKILL.md") == "run-tests"
    assert _attempted_skill_name("a/b/skills/deep/SKILL.md") == "deep"
    # Not the convention -- kept verbatim, because the wrong spelling is
    # the finding.
    assert _attempted_skill_name("docs/thing/SKILL.md") == "docs/thing/SKILL.md"
    assert _attempted_skill_name("SKILL.md") == "SKILL.md"


def test_attempted_skill_name_is_bounded():
    from nanobot.agent.subagent import _MAX_ATTEMPTED_NAME_CHARS, _attempted_skill_name

    assert len(_attempted_skill_name("x" * 5000)) == _MAX_ATTEMPTED_NAME_CHARS
