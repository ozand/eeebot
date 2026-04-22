from nanobot.config.schema import Config
from nanobot.cli.commands import _make_provider


def test_make_provider_applies_supermind_override(monkeypatch):
    cfg = Config()
    cfg.agents.defaults.model = 'base-model'
    cfg.agents.defaults.max_tokens = 100
    cfg.agents.defaults.reasoning_effort = 'low'
    cfg.supermind.enabled = True
    cfg.supermind.model = 'boost-model'
    cfg.supermind.max_tokens = 999
    cfg.supermind.reasoning_effort = 'high'

    # Ensure provider path resolves without real credentials by using custom provider.
    cfg.agents.defaults.provider = 'custom'
    cfg.providers.custom.api_base = 'http://127.0.0.1:4001/v1'
    cfg.providers.custom.api_key = 'test'

    provider = _make_provider(cfg)
    assert provider.default_model == 'boost-model'
    assert provider.generation.max_tokens == 999
    assert provider.generation.reasoning_effort == 'high'
