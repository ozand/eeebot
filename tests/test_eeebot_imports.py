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


def test_import_eeebot_config_loader_alias() -> None:
    mod = importlib.import_module('eeebot.config.loader')
    from nanobot.config.loader import load_config

    assert mod.load_config is load_config


def test_import_eeebot_provider_registry_alias() -> None:
    mod = importlib.import_module('eeebot.providers.registry')
    from nanobot.providers.registry import find_by_name

    assert mod.find_by_name is find_by_name


def test_import_eeebot_bus_queue_alias() -> None:
    mod = importlib.import_module('eeebot.bus.queue')
    from nanobot.bus.queue import MessageBus

    assert mod.MessageBus is MessageBus


def test_import_eeebot_cron_service_alias() -> None:
    mod = importlib.import_module('eeebot.cron.service')
    from nanobot.cron.service import CronService

    assert mod.CronService is CronService


def test_import_eeebot_agent_loop_alias() -> None:
    mod = importlib.import_module('eeebot.agent.loop')
    from nanobot.agent.loop import AgentLoop

    assert mod.AgentLoop is AgentLoop


def test_import_eeebot_agent_context_alias() -> None:
    mod = importlib.import_module('eeebot.agent.context')
    from nanobot.agent.context import ContextBuilder

    assert mod.ContextBuilder is ContextBuilder


def test_import_eeebot_agent_memory_alias() -> None:
    mod = importlib.import_module('eeebot.agent.memory')
    from nanobot.agent.memory import MemoryStore

    assert mod.MemoryStore is MemoryStore


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
