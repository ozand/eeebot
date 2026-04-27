

def test_tools_config_exposes_subagent_compatibility_section():
    from nanobot.config.schema import ToolsConfig

    cfg = ToolsConfig()
    assert cfg.subagent.max_running == 1
