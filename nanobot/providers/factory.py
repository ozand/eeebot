"""Provider factory for creating LLMProvider instances from configuration."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.config.schema import Config
    from nanobot.providers.base import LLMProvider


def _make_provider(config: Config) -> LLMProvider:
    """Create the appropriate LLM provider from config.

    Standalone factory extracted from ``nanobot.cli.commands`` to decouple
    the background daemon/bridge path from the interactive CLI stack.
    """
    from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        provider = OpenAICodexProvider(default_model=model)
    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    elif provider_name == "custom":
        from nanobot.providers.custom_provider import CustomProvider

        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    # Azure OpenAI: direct Azure OpenAI endpoint with deployment name
    elif provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            sys.stderr.write(
                "Error: Azure OpenAI requires api_key and api_base.\n"
                "Set them in ~/.nanobot/config.json under providers.azure_openai section\n"
                "Use the model field to specify the deployment name.\n"
            )
            raise SystemExit(1)
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    else:
        from nanobot.providers.litellm_provider import LiteLLMProvider
        from nanobot.providers.registry import find_by_name

        spec = find_by_name(provider_name)
        if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and (spec.is_oauth or spec.is_local)):
            sys.stderr.write(
                "Error: No API key configured.\n"
                "Set one in ~/.nanobot/config.json under providers section\n"
            )
            raise SystemExit(1)
        provider = LiteLLMProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            provider_name=provider_name,
        )

    defaults = config.agents.defaults
    generation_model = defaults.model
    generation_reasoning_effort = defaults.reasoning_effort
    generation_max_tokens = defaults.max_tokens
    if getattr(config, "supermind", None) and config.supermind.enabled:
        generation_model = config.supermind.model or generation_model
        generation_reasoning_effort = config.supermind.reasoning_effort or generation_reasoning_effort
        generation_max_tokens = config.supermind.max_tokens or generation_max_tokens
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=generation_max_tokens,
        reasoning_effort=generation_reasoning_effort,
    )
    if generation_model:
        provider.default_model = generation_model
    return provider
