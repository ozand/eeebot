from nanobot.cli.eeebot import main


def test_eeebot_cli_alias_imports() -> None:
    assert callable(main)
