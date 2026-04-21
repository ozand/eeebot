import importlib


def test_import_eeebot_top_level_aliases_nanobot_metadata() -> None:
    import eeebot

    assert eeebot.__version__ == '0.1.4.post5'
    assert eeebot.__logo__ == '🐈'


def test_import_eeebot_cli_commands_alias() -> None:
    mod = importlib.import_module('eeebot.cli.commands')
    from nanobot.cli.commands import app

    assert mod.app is app


def test_import_eeebot_config_paths_alias() -> None:
    mod = importlib.import_module('eeebot.config.paths')
    from nanobot.config.paths import get_workspace_path

    assert mod.get_workspace_path is get_workspace_path
