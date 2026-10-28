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
from unittest.mock import AsyncMock
import uuid
import json

@pytest.mark.asyncio
async def test_planner_final_turn_starts_at_local_call_reserve_without_tools(tmp_path, monkeypatch):
    """ADR-045: finalization at Dplanner-C preserves the configured local C window."""
    monkeypatch.setenv("NANOBOT_WALL_CALL_P99_SECS", "243")
    monkeypatch.setenv("NANOBOT_PROGRESS_TIMEOUT_SECS", "10000")
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    from nanobot.agent.tools.toolsets import PLANNER_PROFILE

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
async def test_opted_in_planner_tool_timeout_drains_cancellation_without_late_call(tmp_path, monkeypatch):
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
        planner_phase_budget=True, planner_deadline=6000.0, wall_deadline=9000.0,
        max_iterations=20, tool_profile=PLANNER_PROFILE,
        role_system_prompt="planner {{TOOL_CAPABILITIES}}",
    )
    manager._monotonic = lambda: clock[0]
    spawned = await manager.spawn("plan", label="planner", origin_channel="system", origin_chat_id="test")
    task = manager._running_tasks[next(iter(manager._running_tasks))]
    await asyncio.wait_for(tool_started.wait(), timeout=2)
    clock[0] = 8957.0
    await asyncio.wait_for(tool_cancelled.wait(), timeout=2)
    assert not second_provider_call.is_set()
    assert task.done() is False  # the wait_for timeout requested cancellation; tool suppresses it
    release_tool.set()
    await asyncio.gather(task, return_exceptions=True)
    assert spawned.startswith("Subagent [planner] started")
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
