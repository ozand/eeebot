import importlib


def test_import_eeebot_top_level_aliases_nanobot_metadata() -> None:
    import eeebot

    assert eeebot.__version__ == '0.1.4.post5'
    assert eeebot.__logo__ == '🐈'


def test_import_eeebot_cli_commands_alias() -> None:
    mod = importlib.import_module('eeebot.cli.commands')
    from nanobot.cli.eeebot import app

    assert mod.app is app


def test_import_eeebot_config_paths_alias() -> None:
    mod = importlib.import_module('eeebot.config.paths')
    from nanobot.config.paths import get_workspace_path

    assert mod.get_workspace_path is get_workspace_path


def test_import_eeebot_agent_loop_alias() -> None:
    mod = importlib.import_module('eeebot.agent.loop')
    from nanobot.agent.loop import AgentLoop

    assert mod.AgentLoop is AgentLoop


def test_import_eeebot_runtime_state_alias() -> None:
    mod = importlib.import_module('eeebot.runtime.state')
    from nanobot.runtime.state import load_runtime_state

    assert mod.load_runtime_state is load_runtime_state


def test_import_eeebot_runtime_coordinator_alias() -> None:
    mod = importlib.import_module('eeebot.runtime.coordinator')
    from nanobot.runtime.coordinator import run_self_evolving_cycle

    assert mod.run_self_evolving_cycle is run_self_evolving_cycle


def test_import_eeebot_runtime_promotion_alias() -> None:
    mod = importlib.import_module('eeebot.runtime.promotion')
    from nanobot.runtime.promotion import review_promotion_candidate

    assert mod.review_promotion_candidate is review_promotion_candidate


def test_import_eeebot_session_manager_alias() -> None:
    mod = importlib.import_module('eeebot.session.manager')
    from nanobot.session.manager import SessionManager

    assert mod.SessionManager is SessionManager
