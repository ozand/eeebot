from typer.testing import CliRunner

from nanobot.cli.eeebot import app, main


runner = CliRunner()


def test_eeebot_cli_alias_imports() -> None:
    assert callable(main)


def test_eeebot_cli_help_uses_eeebot_branding() -> None:
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    assert 'eeebot' in result.stdout
    assert 'eeepc self-improving runtime' in result.stdout
