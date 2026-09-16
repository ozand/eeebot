"""#1660: telemetry must name the model that actually served a call, not
merely the one requested -- a gateway fallback substitutes a different
deployment, and unconditionally recording the request cannot distinguish
that from an ordinary call.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import nanobot.providers.base as providers_base
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.litellm_provider import LiteLLMProvider


class ScriptedProvider(LLMProvider):
    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)

    async def chat(self, *args, **kwargs) -> LLMResponse:
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "requested-model"


@pytest.mark.asyncio
async def test_record_prefers_served_model_over_requested(monkeypatch) -> None:
    recorded: list[dict] = []
    # #1660 test fix: nanobot.providers has a lazy module __getattr__ (for
    # provider backends); resolving "nanobot.providers.base.record_llm_call"
    # as a dotted string depends on that package attribute already being
    # attached, which is order-dependent under the full suite. Patch the
    # already-imported module object directly instead.
    monkeypatch.setattr(providers_base, "record_llm_call", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(providers_base, "record_llm_prompt", lambda **kw: None)

    provider = ScriptedProvider([
        LLMResponse(content="ok", served_model="an/gemini-3.8-flash-high"),
    ])
    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="un/qwen3.8-27b-gguf"
    )

    assert recorded[0]["model"] == "an/gemini-3.8-flash-high"


@pytest.mark.asyncio
async def test_record_falls_back_to_requested_model_when_none_served(monkeypatch) -> None:
    """A provider that reports no served model (or an error path) must not
    lose the requested model from telemetry -- fall back, never blank it."""
    recorded: list[dict] = []
    # #1660 test fix: nanobot.providers has a lazy module __getattr__ (for
    # provider backends); resolving "nanobot.providers.base.record_llm_call"
    # as a dotted string depends on that package attribute already being
    # attached, which is order-dependent under the full suite. Patch the
    # already-imported module object directly instead.
    monkeypatch.setattr(providers_base, "record_llm_call", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(providers_base, "record_llm_prompt", lambda **kw: None)

    provider = ScriptedProvider([LLMResponse(content="ok", served_model=None)])
    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="un/qwen3.8-27b-gguf"
    )

    assert recorded[0]["model"] == "un/qwen3.8-27b-gguf"


def _fake_response(model: str = "un/qwen3.8-27b-gguf") -> SimpleNamespace:
    message = SimpleNamespace(
        content="ok", tool_calls=None, reasoning_content=None, thinking_blocks=None,
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


def test_parse_response_captures_served_model() -> None:
    provider = LiteLLMProvider.__new__(LiteLLMProvider)
    response = provider._parse_response(_fake_response(model="an/gemini-3.8-flash-high"))
    assert response.served_model == "an/gemini-3.8-flash-high"


def test_parse_response_none_when_provider_reports_no_model() -> None:
    fake = _fake_response()
    del fake.model
    provider = LiteLLMProvider.__new__(LiteLLMProvider)
    response = provider._parse_response(fake)
    assert response.served_model is None
