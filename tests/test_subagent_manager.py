

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
