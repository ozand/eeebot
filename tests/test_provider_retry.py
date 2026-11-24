import asyncio
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.runtime import intent_journal


def _enable_journal_logic_backend(monkeypatch) -> None:
    """Use thread-serialized test primitives; this is not platform durability evidence."""
    locks: dict[str, threading.Lock] = {}
    guard = threading.Lock()

    @contextmanager
    def fake_owner_lock(path, *, create=False):
        if create:
            path.touch(exist_ok=True)
        elif not path.exists():
            raise intent_journal.JournalUnavailableError("lock missing in fake backend")
        key = str(path.resolve())
        with guard:
            lock = locks.setdefault(key, threading.Lock())
        with lock:
            yield

    monkeypatch.setattr(intent_journal, "_owner_lock", fake_owner_lock)
    monkeypatch.setattr(intent_journal, "_fsync_directory", lambda _path: None)


class ScriptedProvider(LLMProvider):
    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0
        self.last_kwargs: dict = {}

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_kwargs = kwargs
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_litellm_per_send_guard_preserves_known_retry_and_holds_lost_response(tmp_path, monkeypatch) -> None:
    import hashlib
    import openai
    import litellm
    from openai import _base_client
    from nanobot.runtime.intent_journal import JournalUnavailableError
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.runtime.intent_journal import bridge_parent_context, provision_route_parent

    _enable_journal_logic_backend(monkeypatch)
    sdk_httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx
    default_client = openai.DefaultAsyncHttpxClient
    import inspect
    from litellm.llms.custom_httpx.http_handler import get_ssl_configuration

    original_get_ssl_configuration = get_ssl_configuration
    def synthetic_ssl_configuration(ssl_verify=None):
        if ssl_verify == "synthetic-call-ca":
            return "synthetic-call-ca"
        if ssl_verify is not None:
            return original_get_ssl_configuration(ssl_verify)
        return "synthetic-env-ca"

    monkeypatch.setattr(
        "litellm.llms.custom_httpx.http_handler.get_ssl_configuration", synthetic_ssl_configuration,
    )
    monkeypatch.setattr(litellm, "ssl_certificate", "synthetic-global-client.pem")
    monkeypatch.setenv("SSL_CERTIFICATE", "synthetic-env-client.pem")
    assert LiteLLMProvider._journal_tls_options(None) == (
        "synthetic-env-ca", "synthetic-env-client.pem",
    )
    assert LiteLLMProvider._journal_timeout({"stream": False}) == 600
    import enum
    from litellm.litellm_core_utils import completion_timeout as timeout_module

    CompletionTimeout = timeout_module.CompletionTimeout
    StreamKind = enum.Enum("StreamKind", {"NOT_STREAMED": "not_streamed"})
    observed_stream_kind: list[object] = []

    def newer_timeout_resolve(
        _model_timeout, _kwargs, _provider, *, global_timeout, supports_httpx_timeout, stream_kind,
    ):
        observed_stream_kind.append(stream_kind)
        return 123.0

    with monkeypatch.context() as patch:
        patch.setattr(timeout_module, "StreamKind", StreamKind, raising=False)
        patch.setattr(CompletionTimeout, "resolve", staticmethod(newer_timeout_resolve))
        assert LiteLLMProvider._journal_timeout({"stream": False}) == 123.0
        assert "stream_kind" in inspect.signature(CompletionTimeout.resolve).parameters
    assert observed_stream_kind == [StreamKind.NOT_STREAMED]
    monkeypatch.delenv("SSL_CERTIFICATE")
    assert LiteLLMProvider._journal_tls_options("synthetic-call-ca") == (
        "synthetic-call-ca", "synthetic-global-client.pem",
    )
    monkeypatch.setenv("SSL_CERTIFICATE", "synthetic-env-client.pem")

    async def run(
        name: str, outcomes: list[str], expected_sends: int, expected_events: list[str],
        *, model_override: str | None = None, expect_success: bool = False,
    ):
        sent: list[bytes] = []
        request_routes: list[tuple[str, str | None]] = []
        scripted = list(outcomes)
        tls_options: list[tuple[object, object]] = []

        def transport(request):
            sent.append(request.content)
            request_routes.append((str(request.url), request.headers.get("x-route-test")))
            outcome = scripted.pop(0)
            if outcome == "lost":
                raise sdk_httpx.ConnectError("lost after transport receipt", request=request)
            status = int(outcome)
            if status == 200:
                body = {
                    "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
                    "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            else:
                body = {"error": {"message": f"confirmed HTTP {status}", "type": "server_error"}}
            return sdk_httpx.Response(status, json=body, request=request)

        class Client(default_client):
            def __init__(self, **kwargs):
                kwargs.pop("transport", None)
                tls_options.append((kwargs.get("verify"), kwargs.get("cert")))
                super().__init__(transport=sdk_httpx.MockTransport(transport), **kwargs)

        monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", Client)
        monkeypatch.setattr(litellm, "num_retries", None)
        monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", False)
        provider = LiteLLMProvider(
            api_key="fake", api_base="https://mock.invalid/v1", default_model="gpt-4o-mini",
            extra_headers={"X-Route-Test": "provider-header"},
        )
        state_root = tmp_path / name / "state"
        state_root.mkdir(parents=True)
        owner = provision_route_parent(state_root, "bridge.cycle")
        with bridge_parent_context(state_root, owner, route="bridge.cycle"):
            with intent_journal.operation_context("subagent", "provider.chat"):
                assert provider.journal_manages_physical_attempts(provider.get_default_model()) is True
                if name == "lost":
                    with pytest.raises(JournalUnavailableError, match="unknown settlement"):
                        await provider.chat_with_retry(
                            messages=[{"role": "user", "content": "same exact body"}],
                        )
                elif name == "http200":
                    response = await provider.chat(
                        messages=[{"role": "user", "content": "same exact body"}], model=model_override,
                    )
                    assert response.content == "ok"
                    assert response.finish_reason == "stop"
                else:
                    response = await provider.chat(
                        messages=[{"role": "user", "content": "same exact body"}], model=model_override,
                    )
                    assert response.finish_reason == ("stop" if expect_success else "error")
        rows = intent_journal.read_owner_records(state_root, owner)
        if name == "lost":
            journal_path = state_root / "intent-journal" / f"{owner}.jsonl"
            parent_path = journal_path.with_suffix(".parent.json")
            preserved = (journal_path.read_bytes(), parent_path.read_bytes())
            with pytest.raises(JournalUnavailableError, match="unresolved|active"):
                with intent_journal.resume_bridge_cycle_parent_context(
                    state_root, owner, cycle_id="resume-after-provider-loss",
                    completed_phase="post_execution", checkpoint_head_sha="a" * 40,
                    checkpoint_status="complete",
                ):
                    await provider.chat_with_retry(
                        messages=[{"role": "user", "content": "same exact body"}],
                    )
            assert (journal_path.read_bytes(), parent_path.read_bytes()) == preserved
            assert len(sent) == 1
        assert len(sent) == expected_sends
        assert [row["event"] for row in rows] == expected_events
        assert all(url == "https://mock.invalid/v1/chat/completions" for url, _ in request_routes)
        assert all(header == "provider-header" for _, header in request_routes)
        assert tls_options == [("synthetic-env-ca", "synthetic-env-client.pem")]
        return sent, rows

    successful, successful_rows = await run(
        "http200", ["200"], 1, ["intent", "terminal", "consumed"],
    )
    override_success, _override_rows = await run(
        "supported-override", ["200"], 1, ["intent", "terminal", "consumed"],
        model_override="gpt-4.1-mini", expect_success=True,
    )
    confirmed, confirmed_rows = await run(
        "http500", ["500", "500", "500"], 3,
        ["intent", "terminal", "consumed"] * 3,
    )
    lost, lost_rows = await run("lost", ["lost"], 1, ["intent"])
    assert len(successful) == 1
    assert len(override_success) == 1
    assert [row["event"] for row in successful_rows] == ["intent", "terminal", "consumed"]
    assert hashlib.sha256(confirmed[0]).hexdigest() == hashlib.sha256(lost[0]).hexdigest()
    assert all(body == confirmed[0] for body in confirmed)
    assert len({row["attempt_id"] for row in confirmed_rows if row["event"] == "intent"}) == 3
    assert lost_rows[0]["event"] == "intent"

    # Compare the actual outbound baseline route/options against the guarded path.
    baseline_sent: list[bytes] = []
    baseline_routes: list[tuple[str, str | None]] = []

    def baseline_transport(request):
        baseline_sent.append(request.content)
        baseline_routes.append((str(request.url), request.headers.get("x-route-test")))
        raise sdk_httpx.ConnectError("nonjournal baseline response loss", request=request)

    class BaselineClient(default_client):
        def __init__(self, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(transport=sdk_httpx.MockTransport(baseline_transport), **kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", BaselineClient)
    monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", False)
    monkeypatch.setattr(litellm, "num_retries", None)
    baseline_http = sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(baseline_transport))
    monkeypatch.setattr(litellm, "aclient_session", baseline_http)
    baseline_provider = LiteLLMProvider(
        api_key="fake", api_base="https://mock.invalid/v1", default_model="gpt-4o-mini",
        extra_headers={"X-Route-Test": "provider-header"},
    )
    try:
        baseline_result = await baseline_provider.chat(messages=[{"role": "user", "content": "same exact body"}])
    finally:
        await baseline_http.aclose()
        monkeypatch.setattr(litellm, "aclient_session", None)
    assert baseline_result.finish_reason == "error"
    assert len(baseline_sent) == 3
    assert all(body == successful[0] for body in baseline_sent)
    assert baseline_routes == [
        ("https://mock.invalid/v1/chat/completions", "provider-header"),
    ] * 3

    # A per-call unsupported model override is rejected before the OpenAI guard sends.
    override_sent: list[bytes] = []

    class OverrideClient(default_client):
        def __init__(self, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(transport=sdk_httpx.MockTransport(
                lambda request: (override_sent.append(request.content), sdk_httpx.Response(200, json={}, request=request))[1],
            ), **kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", OverrideClient)
    override_root = tmp_path / "override" / "state"
    override_root.mkdir(parents=True)
    override_owner = provision_route_parent(override_root, "bridge.cycle")
    with bridge_parent_context(override_root, override_owner, route="bridge.cycle"):
        with intent_journal.operation_context("subagent", "provider.chat"):
            with pytest.raises(JournalUnavailableError, match="no supported per-send client guard"):
                await baseline_provider.chat(
                    messages=[{"role": "user", "content": "must refuse override"}],
                    model="anthropic/claude-3-5-haiku-latest",
                )
    assert override_sent == []
    assert intent_journal.read_owner_records(override_root, override_owner) == []

    # Unsupported provider families hold before transport and admit no attempt.
    unsupported_sent: list[bytes] = []

    def unsupported_transport(request):
        unsupported_sent.append(request.content)
        return sdk_httpx.Response(200, json={}, request=request)

    class UnsupportedClient(default_client):
        def __init__(self, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(transport=sdk_httpx.MockTransport(unsupported_transport), **kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", UnsupportedClient)
    unsupported = LiteLLMProvider(
        api_key="fake", default_model="anthropic/claude-3-5-haiku-latest",
    )
    unsupported_root = tmp_path / "unsupported" / "state"
    unsupported_root.mkdir(parents=True)
    unsupported_owner = provision_route_parent(unsupported_root, "bridge.cycle")
    with bridge_parent_context(unsupported_root, unsupported_owner, route="bridge.cycle"):
        with intent_journal.operation_context("subagent", "provider.chat"):
            with pytest.raises(JournalUnavailableError, match="no supported per-send client guard"):
                await unsupported.chat_with_retry(messages=[{"role": "user", "content": "must not dispatch"}])
    assert unsupported_sent == []
    assert intent_journal.read_owner_records(unsupported_root, unsupported_owner) == []

    # Without an active journal operation, keep the regular LiteLLM route.
    provider = LiteLLMProvider(api_key="fake", api_base="https://mock.invalid/v1", default_model="gpt-4o-mini")
    assert provider.journal_manages_physical_attempts(provider.get_default_model()) is False


@pytest.mark.asyncio
async def test_real_chat_with_retry_journals_each_planner_attempt_and_closes(tmp_path, monkeypatch) -> None:
    from uuid import uuid4

    _enable_journal_logic_backend(monkeypatch)
    owner = str(uuid4())
    state_root = tmp_path / "state"
    state_root.mkdir()
    intent_journal.create_parent(state_root, owner)
    parent_token = intent_journal.set_parent_context(state_root, owner)
    provider = ScriptedProvider([
        LLMResponse(content="429 rate limit", finish_reason="error"),
        LLMResponse(content="ok"),
    ])
    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _fake_sleep)
    try:
        with intent_journal.operation_context("subagent", "provider.chat") as operation:
            assert operation is not None
            response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])
            operation.consume_pending()
            operation.close()
        rows = intent_journal.read_owner_records(state_root, owner)
        assert response.content == "ok"
        assert provider.calls == 2
        assert delays == [1]
        assert [row["event"] for row in rows] == ["intent", "terminal", "consumed"] * 2
        assert len({row["attempt_id"] for row in rows}) == 2
        assert len({row["operation_id"] for row in rows}) == 1
        assert all(row["owner_id"] == owner for row in rows)
        parent = __import__("json").loads(
            (state_root / "intent-journal" / f"{owner}.parent.json").read_text()
        )
        assert parent["active_operation_id"] is None
        assert parent["last_operation_id"] == rows[0]["operation_id"]
    finally:
        intent_journal.reset_parent_context(parent_token)


@pytest.mark.asyncio
async def test_journalled_chat_with_retry_records_full_existing_transient_budget(tmp_path, monkeypatch) -> None:
    from uuid import uuid4

    _enable_journal_logic_backend(monkeypatch)
    owner = str(uuid4())
    state_root = tmp_path / "state"
    state_root.mkdir()
    intent_journal.create_parent(state_root, owner)
    parent_token = intent_journal.set_parent_context(state_root, owner)
    provider = ScriptedProvider([
        LLMResponse(content=f"{status} rate limit", finish_reason="error")
        for status in (429, 500, 502, 503)
    ])
    delays: list[int] = []

    async def fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", fake_sleep)
    try:
        with intent_journal.operation_context("subagent", "provider.chat") as operation:
            assert operation is not None
            response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])
            operation.consume_pending()
            operation.close()
        rows = intent_journal.read_owner_records(state_root, owner)
        attempts = [row for row in rows if row["event"] == "intent"]
        assert provider.calls == 4
        assert delays == [1, 2, 4]
        assert response.content == "503 rate limit"
        assert len(attempts) == 4
        assert len({row["attempt_id"] for row in attempts}) == 4
        assert [row["event"] for row in rows] == ["intent", "terminal", "consumed"] * 4
    finally:
        intent_journal.reset_parent_context(parent_token)


@pytest.mark.asyncio
async def test_journalled_image_fallback_records_distinct_stripped_attempt(tmp_path, monkeypatch) -> None:
    from uuid import uuid4

    _enable_journal_logic_backend(monkeypatch)
    owner = str(uuid4())
    state_root = tmp_path / "state"
    state_root.mkdir()
    intent_journal.create_parent(state_root, owner)
    parent_token = intent_journal.set_parent_context(state_root, owner)
    provider = ScriptedProvider([
        LLMResponse(content="400 invalid request", finish_reason="error"),
        LLMResponse(content="ok without image"),
    ])
    physical_messages: list[list[dict]] = []
    original_chat = provider.chat

    async def capture_chat(*args, **kwargs):
        physical_messages.append(kwargs["messages"])
        return await original_chat(*args, **kwargs)

    provider.chat = capture_chat
    try:
        with intent_journal.operation_context("subagent", "provider.chat") as operation:
            assert operation is not None
            response = await provider.chat_with_retry(messages=_IMAGE_MSG)
            operation.consume_pending()
            operation.close()
        rows = intent_journal.read_owner_records(state_root, owner)
        attempts = [row for row in rows if row["event"] == "intent"]
        assert response.content == "ok without image"
        assert len(physical_messages) == 2
        assert physical_messages[0] == _IMAGE_MSG
        assert all(block.get("type") != "image_url" for block in physical_messages[1][0]["content"])
        assert len({row["attempt_id"] for row in attempts}) == 2
        assert [row["event"] for row in rows] == ["intent", "terminal", "consumed"] * 2
    finally:
        intent_journal.reset_parent_context(parent_token)


@pytest.mark.asyncio
async def test_journalled_chat_cancellation_leaves_unknown_attempt_held(tmp_path, monkeypatch) -> None:
    from uuid import uuid4

    _enable_journal_logic_backend(monkeypatch)
    owner = str(uuid4())
    state_root = tmp_path / "state"
    state_root.mkdir()
    intent_journal.create_parent(state_root, owner)
    parent_token = intent_journal.set_parent_context(state_root, owner)
    provider = ScriptedProvider([asyncio.CancelledError()])
    try:
        with pytest.raises(asyncio.CancelledError):
            with intent_journal.operation_context("subagent", "provider.chat"):
                await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])
        rows = intent_journal.read_owner_records(state_root, owner)
        assert [row["event"] for row in rows] == ["intent"]
        parent = json.loads((state_root / "intent-journal" / f"{owner}.parent.json").read_text())
        assert parent["active_operation_id"] == rows[0]["operation_id"]
        with pytest.raises(intent_journal.JournalUnavailableError, match="active or unresolved"):
            with intent_journal.operation_context("subagent", "provider.chat"):
                pass
    finally:
        intent_journal.reset_parent_context(parent_token)


@pytest.mark.asyncio
async def test_journalled_terminal_persistence_failure_withholds_provider_result(tmp_path, monkeypatch) -> None:
    from uuid import uuid4

    _enable_journal_logic_backend(monkeypatch)
    owner = str(uuid4())
    state_root = tmp_path / "state"
    state_root.mkdir()
    intent_journal.create_parent(state_root, owner)
    parent_token = intent_journal.set_parent_context(state_root, owner)
    provider = ScriptedProvider([LLMResponse(content="result not releasable")])
    real_append = intent_journal._append

    def fail_terminal(path, row, records):
        if row["event"] == "terminal":
            raise intent_journal.JournalUnavailableError("injected terminal persistence failure")
        real_append(path, row, records)

    monkeypatch.setattr(intent_journal, "_append", fail_terminal)
    try:
        with pytest.raises(intent_journal.JournalUnavailableError, match="terminal persistence"):
            with intent_journal.operation_context("subagent", "provider.chat"):
                await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])
        assert provider.calls == 1
        assert [row["event"] for row in intent_journal.read_owner_records(state_root, owner)] == ["intent"]
        parent = json.loads((state_root / "intent-journal" / f"{owner}.parent.json").read_text())
        assert parent["active_operation_id"] is not None
    finally:
        intent_journal.reset_parent_context(parent_token)


@pytest.mark.asyncio
async def test_real_chat_with_retry_intent_failure_prevents_provider_call(tmp_path, monkeypatch) -> None:
    from uuid import uuid4

    _enable_journal_logic_backend(monkeypatch)
    owner = str(uuid4())
    state_root = tmp_path / "state"
    state_root.mkdir()
    intent_journal.create_parent(state_root, owner)
    parent_token = intent_journal.set_parent_context(state_root, owner)
    provider = ScriptedProvider([LLMResponse(content="must not call")])
    monkeypatch.setattr(intent_journal, "_append", lambda *args: (_ for _ in ()).throw(
        intent_journal.JournalUnavailableError("injected intent persistence failure")
    ))
    try:
        with intent_journal.operation_context("subagent", "provider.chat"):
            with pytest.raises(intent_journal.JournalUnavailableError, match="injected"):
                await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])
        assert provider.calls == 0
    finally:
        intent_journal.reset_parent_context(parent_token)


@pytest.mark.asyncio
async def test_real_chat_with_retry_retries_transient_error_then_succeeds(monkeypatch) -> None:
    provider = ScriptedProvider([
        LLMResponse(content="429 rate limit", finish_reason="error"),
        LLMResponse(content="ok"),
    ])
    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _fake_sleep)

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    assert response.finish_reason == "stop"
    assert response.content == "ok"
    assert provider.calls == 2
    assert delays == [1]


@pytest.mark.asyncio
async def test_chat_with_retry_does_not_retry_non_transient_error(monkeypatch) -> None:
    provider = ScriptedProvider([
        LLMResponse(content="401 unauthorized", finish_reason="error"),
    ])
    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _fake_sleep)

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "401 unauthorized"
    assert provider.calls == 1
    assert delays == []


@pytest.mark.asyncio
async def test_chat_with_retry_returns_final_error_after_retries(monkeypatch) -> None:
    provider = ScriptedProvider([
        LLMResponse(content="429 rate limit a", finish_reason="error"),
        LLMResponse(content="429 rate limit b", finish_reason="error"),
        LLMResponse(content="429 rate limit c", finish_reason="error"),
        LLMResponse(content="503 final server error", finish_reason="error"),
    ])
    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _fake_sleep)

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "503 final server error"
    assert provider.calls == 4
    assert delays == [1, 2, 4]


@pytest.mark.asyncio
async def test_chat_with_retry_preserves_cancelled_error() -> None:
    provider = ScriptedProvider([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_chat_with_retry_uses_provider_generation_defaults() -> None:
    """When callers omit generation params, provider.generation defaults are used."""
    provider = ScriptedProvider([LLMResponse(content="ok")])
    provider.generation = GenerationSettings(temperature=0.2, max_tokens=321, reasoning_effort="high")

    await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    assert provider.last_kwargs["temperature"] == 0.2
    assert provider.last_kwargs["max_tokens"] == 321
    assert provider.last_kwargs["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_chat_with_retry_explicit_override_beats_defaults() -> None:
    """Explicit kwargs should override provider.generation defaults."""
    provider = ScriptedProvider([LLMResponse(content="ok")])
    provider.generation = GenerationSettings(temperature=0.2, max_tokens=321, reasoning_effort="high")

    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hello"}],
        temperature=0.9,
        max_tokens=9999,
        reasoning_effort="low",
    )

    assert provider.last_kwargs["temperature"] == 0.9
    assert provider.last_kwargs["max_tokens"] == 9999
    assert provider.last_kwargs["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# Image fallback tests
# ---------------------------------------------------------------------------

_IMAGE_MSG = [
    {"role": "user", "content": [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}, "_meta": {"path": "/media/test.png"}},
    ]},
]

_IMAGE_MSG_NO_META = [
    {"role": "user", "content": [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]},
]


@pytest.mark.asyncio
async def test_non_transient_error_with_images_retries_without_images() -> None:
    """Any non-transient error retries once with images stripped when images are present."""
    provider = ScriptedProvider([
        LLMResponse(content="API调用参数有误,请检查文档", finish_reason="error"),
        LLMResponse(content="ok, no image"),
    ])

    response = await provider.chat_with_retry(messages=_IMAGE_MSG)

    assert response.content == "ok, no image"
    assert provider.calls == 2
    msgs_on_retry = provider.last_kwargs["messages"]
    for msg in msgs_on_retry:
        content = msg.get("content")
        if isinstance(content, list):
            assert all(b.get("type") != "image_url" for b in content)
            assert any("[image: /media/test.png]" in (b.get("text") or "") for b in content)


@pytest.mark.asyncio
async def test_non_transient_error_without_images_no_retry() -> None:
    """Non-transient errors without image content are returned immediately."""
    provider = ScriptedProvider([
        LLMResponse(content="401 unauthorized", finish_reason="error"),
    ])

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hello"}],
    )

    assert provider.calls == 1
    assert response.finish_reason == "error"


@pytest.mark.asyncio
async def test_image_fallback_returns_error_on_second_failure() -> None:
    """If the image-stripped retry also fails, return that error."""
    provider = ScriptedProvider([
        LLMResponse(content="some model error", finish_reason="error"),
        LLMResponse(content="still failing", finish_reason="error"),
    ])

    response = await provider.chat_with_retry(messages=_IMAGE_MSG)

    assert provider.calls == 2
    assert response.content == "still failing"
    assert response.finish_reason == "error"


@pytest.mark.asyncio
async def test_image_fallback_records_each_failed_attempt(tmp_path, monkeypatch) -> None:
    """Both the original and image-stripped calls are failed attempts."""
    from datetime import datetime, timezone

    from nanobot.observability.llm_telemetry import call_context

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "llm_calls"))
    provider = ScriptedProvider([
        LLMResponse(content="Error calling LLM: BadRequestError: sanitized", finish_reason="error",
                    usage={"_http_status": 400}),
        LLMResponse(content="Error calling LLM: BadRequestError: sanitized", finish_reason="error",
                    usage={"_http_status": 400}),
    ])
    with call_context("cycle-image-fail", "executor"):
        response = await provider.chat_with_retry(messages=_IMAGE_MSG)

    assert response.finish_reason == "error"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = _read_jsonl(tmp_path / "llm_calls" / "failures" / f"{day}.jsonl")
    assert len(rows) == 2
    assert [row["seq"] for row in rows] == [1, 2]
    assert all(row["error_type"] == "BadRequestError" for row in rows)
    assert all(row["http_status"] == 400 for row in rows)
    assert all(row["cycle_id"] == "cycle-image-fail" for row in rows)
    assert all(row["component"] == "executor" for row in rows)


@pytest.mark.asyncio
async def test_image_fallback_without_meta_uses_default_placeholder() -> None:
    """When _meta is absent, fallback placeholder is '[image omitted]'."""
    provider = ScriptedProvider([
        LLMResponse(content="error", finish_reason="error"),
        LLMResponse(content="ok"),
    ])

    response = await provider.chat_with_retry(messages=_IMAGE_MSG_NO_META)

    assert response.content == "ok"
    assert provider.calls == 2
    msgs_on_retry = provider.last_kwargs["messages"]
    for msg in msgs_on_retry:
        content = msg.get("content")
        if isinstance(content, list):
            assert any("[image omitted]" in (b.get("text") or "") for b in content)


# ---------------------------------------------------------------------------
# LLM call telemetry hook (issue #675)
# ---------------------------------------------------------------------------


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.mark.asyncio
async def test_chat_with_retry_records_telemetry_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    provider = ScriptedProvider([
        LLMResponse(
            content="ok",
            finish_reason="stop",
            usage={"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10},
        ),
    ])

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hello"}],
        model="un/qwen3.6-27b-mtp",
    )

    assert response.content == "ok"
    rec = _read_jsonl(next(tmp_path.glob("*.jsonl")))[0]
    assert rec["model"] == "un/qwen3.6-27b-mtp"
    assert rec["finish_reason"] == "stop"
    assert rec["prompt_tokens"] == 3
    assert rec["completion_tokens"] == 7
    assert rec["total_tokens"] == 10
    assert rec["retries"] == 0
    assert rec["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_chat_with_retry_failure_telemetry_keeps_success_stream_unchanged(tmp_path, monkeypatch):
    from nanobot.observability.llm_telemetry import call_context

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "llm_calls"))
    response = LLMResponse(
        content="Error calling LLM: RateLimitError: No deployments available",
        finish_reason="error",
        usage={"_http_status": 429, "_retry_after_s": 108301},
    )
    provider = ScriptedProvider([response])
    with call_context("cycle-test-fail", "proposer"):
        await provider.chat_with_retry(messages=[{"role": "user", "content": "secret prompt"}])
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    root = tmp_path / "llm_calls"
    failures = _read_jsonl(root / "failures" / f"{day}.jsonl")
    assert len(failures) == 1
    assert failures[0]["error_type"] == "RateLimitError"
    assert failures[0]["http_status"] == 429
    assert failures[0]["retry_after_s"] == 108301
    assert failures[0]["component"] == "proposer"
    assert failures[0]["cycle_id"] == "cycle-test-fail"
    assert not (root / f"{day}.jsonl").exists()
    assert "secret prompt" not in (root / "failures" / f"{day}.jsonl").read_text(encoding="utf-8")


async def test_chat_with_retry_records_telemetry_with_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    async def _fake_sleep(delay: int) -> None:
        return None

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _fake_sleep)

    provider = ScriptedProvider([
        LLMResponse(content="429 rate limit", finish_reason="error"),
        LLMResponse(content="ok", finish_reason="stop", usage={"total_tokens": 5}),
    ])

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "ok"
    records = _read_jsonl(next(tmp_path.glob("*.jsonl")))
    # Only the final (successful) return path records telemetry.
    assert len(records) == 1
    assert records[0]["retries"] == 1
    assert records[0]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_chat_with_retry_uses_default_model_when_unspecified(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    provider = ScriptedProvider([LLMResponse(content="ok")])
    await provider.chat_with_retry(messages=[{"role": "user", "content": "hello"}])

    rec = _read_jsonl(next(tmp_path.glob("*.jsonl")))[0]
    assert rec["model"] == "test-model"
