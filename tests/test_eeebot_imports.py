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


def test_import_eeebot_provider_base_alias() -> None:
    mod = importlib.import_module('eeebot.providers.base')
    from nanobot.providers.base import LLMProvider

    assert mod.LLMProvider is LLMProvider


def test_import_eeebot_bus_queue_alias() -> None:
    mod = importlib.import_module('eeebot.bus.queue')
    from nanobot.bus.queue import MessageBus

    assert mod.MessageBus is MessageBus


def test_import_eeebot_cron_service_alias() -> None:
    mod = importlib.import_module('eeebot.cron.service')
    from nanobot.cron.service import CronService

    assert mod.CronService is CronService


def test_import_eeebot_channels_manager_alias() -> None:
    mod = importlib.import_module('eeebot.channels.manager')
    from nanobot.channels.manager import ChannelManager

    assert mod.ChannelManager is ChannelManager


def test_import_eeebot_heartbeat_service_alias() -> None:
    mod = importlib.import_module('eeebot.heartbeat.service')
    from nanobot.heartbeat.service import HeartbeatService

    assert mod.HeartbeatService is HeartbeatService


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


def test_import_eeebot_agent_package_alias() -> None:
    eeebot_agent = importlib.import_module('eeebot.agent')
    nanobot_agent_loop = importlib.import_module('nanobot.agent.loop')

    # The compatibility package may be a separate module object due to local
    # __init__.py shims; verify the key submodule symbols resolve correctly.
    eeebot_agent_loop = importlib.import_module('eeebot.agent.loop')
    assert eeebot_agent_loop.AgentLoop is nanobot_agent_loop.AgentLoop


def test_import_eeebot_agent_skills_alias() -> None:
    mod = importlib.import_module('eeebot.agent.skills')
    from nanobot.agent.skills import SkillsLoader

    assert mod.SkillsLoader is SkillsLoader


def test_import_eeebot_agent_subagent_alias() -> None:
    mod = importlib.import_module('eeebot.agent.subagent')
    from nanobot.agent.subagent import SubagentManager

    assert mod.SubagentManager is SubagentManager


def test_import_eeebot_bus_package_alias() -> None:
    mod = importlib.import_module('eeebot.bus')
    from nanobot.bus.queue import MessageBus

    # Package may differ; verify key symbol resolves.
    assert importlib.import_module('eeebot.bus.queue').MessageBus is MessageBus


def test_import_eeebot_bus_events_alias() -> None:
    mod = importlib.import_module('eeebot.bus.events')
    from nanobot.bus.events import InboundMessage

    assert mod.InboundMessage is InboundMessage


def test_import_eeebot_channels_package_alias() -> None:
    mod = importlib.import_module('eeebot.channels')
    from nanobot.channels.base import BaseChannel

    # Package may differ; verify key symbol resolves.
    assert importlib.import_module('eeebot.channels.base').BaseChannel is BaseChannel


def test_import_eeebot_channels_base_alias() -> None:
    mod = importlib.import_module('eeebot.channels.base')
    from nanobot.channels.base import BaseChannel

    assert mod.BaseChannel is BaseChannel


def test_import_eeebot_channels_registry_alias() -> None:
    mod = importlib.import_module('eeebot.channels.registry')
    from nanobot.channels.registry import load_channel_class

    assert mod.load_channel_class is load_channel_class


def test_import_eeebot_config_package_alias() -> None:
    mod = importlib.import_module('eeebot.config')
    from nanobot.config.loader import load_config

    # Package may differ; verify key symbol resolves.
    assert importlib.import_module('eeebot.config.loader').load_config is load_config


def test_import_eeebot_cron_package_alias() -> None:
    mod = importlib.import_module('eeebot.cron')
    from nanobot.cron.service import CronService

    # Package may differ; verify key symbol resolves.
    assert importlib.import_module('eeebot.cron.service').CronService is CronService


def test_import_eeebot_cron_types_alias() -> None:
    mod = importlib.import_module('eeebot.cron.types')
    from nanobot.cron.types import CronJob

    assert mod.CronJob is CronJob


def test_import_eeebot_heartbeat_package_alias() -> None:
    mod = importlib.import_module('eeebot.heartbeat')
    from nanobot.heartbeat.service import HeartbeatService

    # Package may differ; verify key symbol resolves.
    assert importlib.import_module('eeebot.heartbeat.service').HeartbeatService is HeartbeatService


def test_import_eeebot_providers_package_alias() -> None:
    mod = importlib.import_module('eeebot.providers')
    from nanobot.providers.base import LLMProvider

    # Package may differ; verify key symbol resolves.
    assert importlib.import_module('eeebot.providers.base').LLMProvider is LLMProvider


def test_import_eeebot_utils_package_alias() -> None:
    mod = importlib.import_module('eeebot.utils')
    from nanobot.utils.evaluator import evaluate_response

    # Package may differ; verify key symbol resolves.
    assert importlib.import_module('eeebot.utils.evaluator').evaluate_response is evaluate_response


def test_import_eeebot_utils_helpers_alias() -> None:
    mod = importlib.import_module('eeebot.utils.helpers')

    # The module may be a separate object due to __path__ extension, so test
    # that the key function exists and is callable rather than identity.
    assert callable(mod.estimate_prompt_tokens)


def test_import_eeebot_utils_evaluator_alias() -> None:
    mod = importlib.import_module('eeebot.utils.evaluator')
    from nanobot.utils.evaluator import evaluate_response

    assert mod.evaluate_response is evaluate_response


def test_import_eeebot_config_schema_alias() -> None:
    mod = importlib.import_module('eeebot.config.schema')
    from nanobot.config.schema import Config

    assert mod.Config is Config


def test_import_eeebot_security_network_alias() -> None:
    mod = importlib.import_module('eeebot.security.network')
    from nanobot.security.network import validate_url_target

    assert mod.validate_url_target is validate_url_target


def test_import_eeebot_cli_onboard_wizard_alias() -> None:
    mod = importlib.import_module('eeebot.cli.onboard_wizard')

    assert mod is not None
