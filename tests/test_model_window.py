"""Tests for nanobot.providers.model_window (issue #1755).

The resolver backs ``llm_calls``' ``context_window`` field: it does a live
``GET {api_base}/model/info`` (never the static ``litellm.model_cost`` table,
which doesn't know about custom local gateway routes), caches both a hit and
a failure per ``(api_base, model)`` for the life of the process, and logs its
"window unknown" warning at most once per that key -- never on a cache hit,
never on every call.
"""
from __future__ import annotations

import httpx
import pytest

import nanobot.providers.base as providers_base
from nanobot.providers import model_window
from nanobot.providers.base import LLMProvider, LLMResponse


@pytest.fixture(autouse=True)
def _clear_model_window_cache():
    model_window._reset_cache_for_tests()
    yield
    model_window._reset_cache_for_tests()


def _mock_get(monkeypatch, *, response=None, status_code=200, raise_exc=None):
    """Patch ``model_window.httpx.get``; record every URL and header map it saw."""
    class _Calls(list):
        """A list of URLs that also carries the header map seen per call."""
        headers_seen: list[dict]

    calls = _Calls()
    headers_seen: list[dict] = []

    class FakeResponse:
        def __init__(self, body, code):
            self._body = body
            self.status_code = code

        def json(self):
            return self._body

    def fake_get(url, timeout=None, headers=None):
        calls.append(url)
        headers_seen.append(headers or {})
        if raise_exc is not None:
            raise raise_exc
        return FakeResponse(response, status_code)

    monkeypatch.setattr(model_window.httpx, "get", fake_get)
    calls.headers_seen = headers_seen
    return calls


# ─── resolution against a known model ───────────────────────────────────────


def test_resolve_known_model_returns_max_input_tokens(monkeypatch):
    payload = {
        "data": [
            {"model_name": "un/qwen3.8-27b-gguf", "model_info": {"max_input_tokens": 98304}},
        ]
    }
    calls = _mock_get(monkeypatch, response=payload)

    window = model_window.resolve_context_window("un/qwen3.8-27b-gguf", "http://gw:4001")

    assert window == 98304
    assert calls == ["http://gw:4001/model/info"]


def test_resolve_strips_trailing_slash_from_api_base(monkeypatch):
    payload = {"data": [{"model_name": "m", "model_info": {"max_input_tokens": 1000}}]}
    calls = _mock_get(monkeypatch, response=payload)

    model_window.resolve_context_window("m", "http://gw:4001/")

    assert calls == ["http://gw:4001/model/info"]


def test_resolve_strips_openai_route_prefix_before_matching(monkeypatch):
    """base.py's litellm-SDK call site passes ``model="openai/un/..."`` --
    litellm strips that route head before the model goes over the wire, so
    the gateway's own ``/model/info`` lists the bare name. The resolver must
    strip it the same way (mirrors
    ``model_registry.resolve_model(strip_openai=True)``) or the executor's
    own route would never match."""
    payload = {"data": [{"model_name": "un/qwen3.8-27b-gguf", "model_info": {"max_input_tokens": 98304}}]}
    _mock_get(monkeypatch, response=payload)

    window = model_window.resolve_context_window("openai/un/qwen3.8-27b-gguf", "http://gw:4001")

    assert window == 98304


# ─── caching: at most one network call per (api_base, model) ───────────────


def test_resolve_caches_hit_and_calls_network_once(monkeypatch):
    payload = {"data": [{"model_name": "m", "model_info": {"max_input_tokens": 1000}}]}
    calls = _mock_get(monkeypatch, response=payload)

    first = model_window.resolve_context_window("m", "http://gw")
    second = model_window.resolve_context_window("m", "http://gw")
    third = model_window.resolve_context_window("m", "http://gw")

    assert first == second == third == 1000
    assert len(calls) == 1


def test_resolve_distinct_pairs_each_get_their_own_call(monkeypatch):
    payload = {"data": [{"model_name": "m", "model_info": {"max_input_tokens": 1000}}]}
    calls = _mock_get(monkeypatch, response=payload)

    model_window.resolve_context_window("m", "http://gw-a")
    model_window.resolve_context_window("m", "http://gw-b")
    model_window.resolve_context_window("other-model", "http://gw-a")

    assert len(calls) == 3


# ─── unknown model / lookup failure: cached None, logged once ──────────────


def test_resolve_unknown_model_returns_none_and_logs(monkeypatch, caplog):
    payload = {"data": [{"model_name": "other-model", "model_info": {"max_input_tokens": 1000}}]}
    _mock_get(monkeypatch, response=payload)

    with caplog.at_level("WARNING"):
        window = model_window.resolve_context_window("missing-model", "http://gw")

    assert window is None
    assert "context window unknown" in caplog.text


def test_resolve_non_200_returns_none_and_logs_exactly_once(monkeypatch, caplog):
    calls = _mock_get(monkeypatch, response={}, status_code=500)

    with caplog.at_level("WARNING"):
        first = model_window.resolve_context_window("m", "http://gw")
        second = model_window.resolve_context_window("m", "http://gw")
        third = model_window.resolve_context_window("m", "http://gw")

    assert first is None and second is None and third is None
    assert len(calls) == 1  # cached after the first failure, no retry per call
    assert caplog.text.count("context window unknown") == 1


def test_resolve_network_error_returns_none_and_logs_once(monkeypatch, caplog):
    calls = _mock_get(monkeypatch, raise_exc=httpx.ConnectError("boom"))

    with caplog.at_level("WARNING"):
        model_window.resolve_context_window("m", "http://gw")
        model_window.resolve_context_window("m", "http://gw")

    assert len(calls) == 1
    assert caplog.text.count("context window unknown") == 1


def test_resolve_malformed_response_shape_returns_none(monkeypatch):
    _mock_get(monkeypatch, response={"unexpected": "shape"})
    assert model_window.resolve_context_window("m", "http://gw") is None


def test_resolve_max_input_tokens_missing_returns_none(monkeypatch):
    payload = {"data": [{"model_name": "m", "model_info": {}}]}
    _mock_get(monkeypatch, response=payload)
    assert model_window.resolve_context_window("m", "http://gw") is None


def test_resolve_max_input_tokens_bool_is_rejected(monkeypatch):
    """``True``/``False`` pass Python's ``isinstance(x, int)`` -- guard explicitly
    so a malformed upstream value can never masquerade as a real window."""
    payload = {"data": [{"model_name": "m", "model_info": {"max_input_tokens": True}}]}
    _mock_get(monkeypatch, response=payload)
    assert model_window.resolve_context_window("m", "http://gw") is None


def test_resolve_never_raises_on_missing_inputs():
    assert model_window.resolve_context_window(None, "http://gw") is None
    assert model_window.resolve_context_window("m", None) is None
    assert model_window.resolve_context_window(None, None) is None


# ─── wiring: chat_with_retry's telemetry record carries the resolved window ─


class ScriptedProvider(LLMProvider):
    def __init__(self, responses, api_base="http://gw:4001"):
        super().__init__(api_base=api_base)
        self._responses = list(responses)

    async def chat(self, *args, **kwargs) -> LLMResponse:
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "requested-model"


def _patch_record_llm_call(monkeypatch) -> list[dict]:
    recorded: list[dict] = []
    # Same rationale as test_served_model_telemetry.py: patch the already-
    # imported module object, not a dotted string, since nanobot.providers
    # has a lazy __getattr__.
    monkeypatch.setattr(providers_base, "record_llm_call", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(providers_base, "record_llm_prompt", lambda **kw: None)
    return recorded


@pytest.mark.asyncio
async def test_chat_with_retry_wires_resolved_context_window(monkeypatch):
    payload = {"data": [{"model_name": "un/qwen3.8-27b-gguf", "model_info": {"max_input_tokens": 98304}}]}
    _mock_get(monkeypatch, response=payload)
    recorded = _patch_record_llm_call(monkeypatch)

    provider = ScriptedProvider([LLMResponse(content="ok")], api_base="http://gw:4001")
    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="openai/un/qwen3.8-27b-gguf"
    )

    assert recorded[0]["context_window"] == 98304


@pytest.mark.asyncio
async def test_chat_with_retry_passes_none_when_window_unknown(monkeypatch):
    _mock_get(monkeypatch, response={"data": []})
    recorded = _patch_record_llm_call(monkeypatch)

    provider = ScriptedProvider([LLMResponse(content="ok")], api_base="http://gw:4001")
    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="openai/unknown-model"
    )

    assert recorded[0]["context_window"] is None


@pytest.mark.asyncio
async def test_chat_with_retry_passes_none_when_no_api_base(monkeypatch):
    """A provider with no api_base (e.g. a hosted provider with no local
    gateway) must never attempt the lookup, and must never guess."""
    calls_seen: list[str] = []
    monkeypatch.setattr(
        model_window.httpx,
        "get",
        lambda *a, **k: calls_seen.append(a) or (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    recorded = _patch_record_llm_call(monkeypatch)

    provider = ScriptedProvider([LLMResponse(content="ok")], api_base=None)
    await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="some-model")

    assert recorded[0]["context_window"] is None
    assert calls_seen == []


# ─── the gateway requires a bearer token (#1755 review) ─────────────────────


def _payload(name="un/qwen3.8-27b-gguf", window=98304):
    return {"data": [{"model_name": name, "model_info": {"max_input_tokens": window}}]}


def test_explicit_api_key_is_sent_as_a_bearer_header(monkeypatch):
    """The live gateway answers /model/info with 401 when unauthenticated
    (measured 2026-09-18), so a resolver that sends no Authorization header
    records context_window: null on every row while looking exactly like a
    model whose window is genuinely unknown."""
    calls = _mock_get(monkeypatch, response=_payload())
    assert model_window.resolve_context_window(
        "openai/un/qwen3.8-27b-gguf", "http://gw:4001/v1", api_key="sk-live",
    ) == 98304
    assert calls.headers_seen[0].get("Authorization") == "Bearer sk-live"


def test_api_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "sk-env")
    calls = _mock_get(monkeypatch, response=_payload())
    assert model_window.resolve_context_window(
        "un/qwen3.8-27b-gguf", "http://gw:4001/v1",
    ) == 98304
    assert calls.headers_seen[0].get("Authorization") == "Bearer sk-env"


def test_no_key_anywhere_sends_no_header_and_still_never_raises(monkeypatch):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = _mock_get(monkeypatch, status_code=401, response={})
    assert model_window.resolve_context_window(
        "un/qwen3.8-27b-gguf", "http://gw:4001/v1",
    ) is None
    assert calls.headers_seen[0] == {}
