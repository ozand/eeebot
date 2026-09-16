"""#1660: telemetry must record BOTH the model this code requested (``model``)
and the one the gateway reports having served (``served_model``). A gateway
fallback substitutes a different deployment; recording only one of the two
cannot distinguish that from an ordinary call. #1678 collapsed them into
``model`` and dropped the request (follow-up issue: served_model overwrote
``model``); these tests pin the two-key contract.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import nanobot.providers.base as providers_base
from nanobot.observability import llm_telemetry
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


def _patch_recorders(monkeypatch) -> list[dict]:
    recorded: list[dict] = []
    # #1660 test fix: nanobot.providers has a lazy module __getattr__ (for
    # provider backends); resolving "nanobot.providers.base.record_llm_call"
    # as a dotted string depends on that package attribute already being
    # attached, which is order-dependent under the full suite. Patch the
    # already-imported module object directly instead.
    monkeypatch.setattr(providers_base, "record_llm_call", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(providers_base, "record_llm_prompt", lambda **kw: None)
    return recorded


@pytest.mark.asyncio
async def test_record_keeps_requested_model_and_adds_served_model(monkeypatch) -> None:
    """``model`` stays the request (exactly as before #1678); the gateway's
    answer lands in a separate ``served_model`` key."""
    recorded = _patch_recorders(monkeypatch)

    provider = ScriptedProvider([
        LLMResponse(content="ok", served_model="an/gemini-3.8-flash-high"),
    ])
    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="openai/un/qwen3.8-27b-gguf"
    )

    assert recorded[0]["model"] == "openai/un/qwen3.8-27b-gguf"
    assert recorded[0]["served_model"] == "an/gemini-3.8-flash-high"


@pytest.mark.asyncio
async def test_record_served_model_is_none_when_gateway_reports_none(monkeypatch) -> None:
    """A provider that reports no served model (or an error path) keeps the
    requested model in ``model`` and records ``served_model`` as None -- it
    must never be fabricated by copying the request into it."""
    recorded = _patch_recorders(monkeypatch)

    provider = ScriptedProvider([LLMResponse(content="ok", served_model=None)])
    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="openai/un/qwen3.8-27b-gguf"
    )

    assert recorded[0]["model"] == "openai/un/qwen3.8-27b-gguf"
    assert recorded[0]["served_model"] is None


@pytest.mark.asyncio
async def test_record_default_model_is_requested_not_served(monkeypatch) -> None:
    """With no explicit model the request resolves to the provider default;
    that default -- not the served name -- is what ``model`` records."""
    recorded = _patch_recorders(monkeypatch)

    provider = ScriptedProvider([LLMResponse(content="ok", served_model="served-x")])
    await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert recorded[0]["model"] == "requested-model"
    assert recorded[0]["served_model"] == "served-x"


def test_record_llm_call_writes_both_keys(tmp_path, monkeypatch) -> None:
    """The JSONL writer persists ``served_model`` as its own key, None when
    the caller passes none, so readers can compute served != requested."""
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    llm_telemetry.record_llm_call(
        model="openai/un/qwen3.8-27b-gguf", served_model="un/qwen3.8-27b-gguf",
        duration_ms=1.0, usage={}, finish_reason="stop", retries=0,
    )
    llm_telemetry.record_llm_call(
        model="openai/un/qwen3.8-27b-gguf",
        duration_ms=1.0, usage={}, finish_reason="stop", retries=0,
    )

    rows = [json.loads(line) for path in tmp_path.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["model"] == "openai/un/qwen3.8-27b-gguf"
    assert rows[0]["served_model"] == "un/qwen3.8-27b-gguf"
    assert rows[1]["model"] == "openai/un/qwen3.8-27b-gguf"
    assert "served_model" in rows[1] and rows[1]["served_model"] is None


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
