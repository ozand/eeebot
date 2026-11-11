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
    def fake_owner_lock(path):
        path.touch(exist_ok=True)
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
