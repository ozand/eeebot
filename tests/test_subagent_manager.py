

def test_subagent_manager_accepts_deployed_bridge_compat_kwargs(tmp_path):
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    class Provider:
        def get_default_model(self):
            return 'test-model'

    class SubagentCfg:
        max_running = 3

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

    manager = SubagentManager(
        provider=Provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        max_iterations=100,
    )
    assert manager.max_iterations == 100
