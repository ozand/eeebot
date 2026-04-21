from typer.testing import CliRunner

from nanobot.cli.commands import app as nanobot_app
from nanobot.cli.eeebot import app as eeebot_app, main


runner = CliRunner()


def test_eeebot_cli_alias_imports() -> None:
    assert callable(main)


def test_eeebot_cli_help_uses_eeebot_branding() -> None:
    result = runner.invoke(eeebot_app, ['--help'])
    assert result.exit_code == 0
    assert 'eeebot' in result.stdout
    assert 'eeepc self-improving runtime' in result.stdout


def test_nanobot_cli_help_now_uses_eeebot_public_branding() -> None:
    result = runner.invoke(nanobot_app, ['--help'])
    assert result.exit_code == 0
    assert 'eeebot' in result.stdout
