import os

from nanobot.config.loader import load_config


def test_load_config_accepts_eeebot_env_aliases(monkeypatch, tmp_path):
    monkeypatch.delenv('NANOBOT_AGENTS__DEFAULTS__WORKSPACE', raising=False)
    monkeypatch.setenv('EEEBOT_AGENTS__DEFAULTS__WORKSPACE', str(tmp_path / 'eeebot-workspace'))

    cfg = load_config(config_path=tmp_path / 'missing.json')

    assert cfg.agents.defaults.workspace == str(tmp_path / 'eeebot-workspace')


def test_nanobot_env_overrides_eeebot_alias(monkeypatch, tmp_path):
    monkeypatch.setenv('EEEBOT_AGENTS__DEFAULTS__WORKSPACE', str(tmp_path / 'eeebot-workspace'))
    monkeypatch.setenv('NANOBOT_AGENTS__DEFAULTS__WORKSPACE', str(tmp_path / 'nanobot-workspace'))

    cfg = load_config(config_path=tmp_path / 'missing.json')

    assert cfg.agents.defaults.workspace == str(tmp_path / 'nanobot-workspace')
