"""LiteLLM provider implementation for multi-provider support."""

import hashlib
import os
import secrets
import string
import ssl
from contextlib import asynccontextmanager
from typing import Any

import httpx
import json_repair
from loguru import logger

from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)
from nanobot.providers.registry import find_by_model, find_gateway
from nanobot.runtime import intent_journal
from nanobot.runtime.intent_journal import JournalUnavailableError

# `litellm` (and, transitively, `tiktoken`) is imported lazily on first use
# (see `_ensure_litellm` below) rather than at module load time, so that
# importing this module — and collecting tests that reference it — stays
# cheap and doesn't require the full litellm/tiktoken dependency chain to be
# installed (tiktoken needs a Rust toolchain to build on some hosts, e.g. the
# i386 eeepc host; see #657).
#
# `acompletion` is kept as a module-level attribute (initially None) so tests
# can `mock.patch("nanobot.providers.litellm_provider.acompletion", ...)`
# before any real import has happened; `_ensure_litellm` fills it in and
# subsequent calls patch the already-bound name.
acompletion = None


def _ensure_litellm() -> None:
    """Import litellm on first use and apply process-global settings once.

    Race-safety note: this module runs in a single asyncio event loop plus
    occasional worker threads spawned by tool_harness._run_async. The
    check-and-import below is not atomic, but CPython's GIL makes the worst
    case a harmless double-import (idempotent) rather than a data race, so a
    plain module-level flag is sufficient here — no lock needed.
    """
    global acompletion
    if acompletion is not None:
        return
    import litellm as _litellm
    from litellm import acompletion as _acompletion

    acompletion = _acompletion
    # Disable LiteLLM logging noise. Process-global by nature (litellm has no
    # per-instance equivalent), so applying it once at first import is correct.
    _litellm.suppress_debug_info = True
    # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params).
    _litellm.drop_params = True


# Standard chat-completion message keys.
_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"})
_ANTHROPIC_EXTRA_KEYS = frozenset({"thinking_blocks"})
_ALNUM = string.ascii_letters + string.digits

def _short_tool_id() -> str:
    """Generate a 9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)

        # NOTE: we deliberately do *not* set `litellm.api_base` here (the
        # stranded/reworked version of this lazy-import patch used to do
        # that once per process on first chat() call anywhere, which meant
        # every instance's requests silently picked up whichever instance
        # happened to initialize litellm first). It is unnecessary: `chat()`
        # already passes `api_base` per-request via `kwargs["api_base"]`
        # (see below), which takes precedence over any module-level litellm
        # default and is correctly scoped to this instance's config.
        self._langsmith_enabled = bool(os.getenv("LANGSMITH_API_KEY"))

    def journal_manages_physical_attempts(self, model: str | None = None) -> bool:
        # If an operation is active, chat() either guards the resolved route or
        # refuses it before dispatch. Do not add an application-level intent.
        return intent_journal.current_operation() is not None

    def _supports_journal_route(self, model: str, kwargs: dict[str, Any] | None = None) -> bool:
        """Use LiteLLM's own resolver and guard only its direct OpenAI SDK route."""
        if self._gateway is not None and self._gateway.name != "aihubmix":
            return False
        kwargs = kwargs or {}
        if kwargs.get("custom_llm_provider") not in (None, "openai"):
            return False
        if any(kwargs.get(key) for key in ("model_list", "fallbacks", "router", "client", "shared_session")):
            return False
        try:
            import litellm
            if (
                litellm.aclient_session is not None
                or litellm.model_fallbacks is not None
                or model in litellm.model_alias_map
                or litellm.route_all_chat_openai_to_responses
                or (kwargs.get("metadata") or {}).get("model_group") is not None
                or (kwargs.get("litellm_metadata") or {}).get("model_group") is not None
            ):
                return False
        except AttributeError:
            return False
        try:
            import litellm
            resolved_model, provider, _dynamic_key, _api_base = litellm.get_llm_provider(
                model=self._resolve_model(model),
                api_base=kwargs.get("api_base") or self.api_base,
                api_key=kwargs.get("api_key") or self.api_key,
                custom_llm_provider=kwargs.get("custom_llm_provider"),
            )
            from litellm.main import responses_api_bridge_check
            response_info, _routed_model = responses_api_bridge_check(
                model=resolved_model, custom_llm_provider=provider,
                web_search_options=kwargs.get("web_search_options"),
                tools=kwargs.get("tools"), reasoning_effort=kwargs.get("reasoning_effort"),
                api_base=kwargs.get("api_base") or self.api_base,
            )
        except Exception:
            return False
        return provider == "openai" and response_info.get("mode") != "responses"

    @asynccontextmanager
    async def _journaled_openai_client(self, kwargs: dict[str, Any]):
        operation = intent_journal.current_operation()
        if operation is None:
            raise JournalUnavailableError("journalled OpenAI client requires an active owner operation")
        if not self._supports_journal_route(self.default_model, kwargs):
            raise JournalUnavailableError("journalled LiteLLM route has no supported per-send client guard")
        import litellm
        import openai
        from nanobot.runtime.intent_journal import IntentHttpHooks

        hooks = IntentHttpHooks(operation)

        async def on_request(request: Any) -> None:
            hooks.on_request(request)

        async def on_response(response: Any) -> None:
            hooks.on_response(response)

        from litellm.llms.custom_httpx.http_handler import (
            AsyncHTTPHandler,
            get_ssl_configuration,
        )
        ssl_verify = kwargs.get("ssl_verify")
        ssl_config = get_ssl_configuration(ssl_verify)
        transport = None
        if litellm.aclient_session is None:
            transport = AsyncHTTPHandler._create_async_transport(
                ssl_context=ssl_config if isinstance(ssl_config, ssl.SSLContext) else None,
                ssl_verify=ssl_config if isinstance(ssl_config, bool) else None,
            )
        try:
            http_client = openai.DefaultAsyncHttpxClient(
                event_hooks={"request": [on_request], "response": [on_response]},
                verify=ssl_config, follow_redirects=True, transport=transport,
                trust_env=not litellm.disable_aiohttp_trust_env,
            )
        except BaseException:
            if transport is not None:
                close = getattr(transport, "close", None)
                if close is not None:
                    close()
            raise
        from litellm.secret_managers.main import get_secret_str
        api_key = (
            kwargs.get("api_key") or self.api_key or litellm.api_key or litellm.openai_key
            or (get_secret_str(self._gateway.env_key) if self._gateway and self._gateway.env_key else None)
        )
        if not api_key:
            api_key = get_secret_str("OPENAI_API_KEY")
        from litellm.main import _resolve_openai_api_base
        api_base = _resolve_openai_api_base(kwargs.get("api_base"))
        if not api_key:
            await http_client.aclose()
            raise JournalUnavailableError("journalled OpenAI route has no resolved API key")
        from litellm.litellm_core_utils.completion_timeout import CompletionTimeout
        from litellm.litellm_core_utils.request_timeout_resolver import get_configured_request_timeout
        from litellm.utils import supports_httpx_timeout
        global_timeout = get_configured_request_timeout()
        timeout = CompletionTimeout.resolve(
            kwargs.get("timeout"), kwargs, "openai",
            global_timeout=global_timeout,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        if (
            kwargs.get("timeout") is None
            and kwargs.get("request_timeout") is None
            and global_timeout is None
        ):
            timeout = litellm.request_timeout
        max_retries = kwargs.get("max_retries")
        if max_retries is None:
            max_retries = litellm.num_retries
            if max_retries is None:
                max_retries = openai.DEFAULT_MAX_RETRIES
        organization = (
            kwargs.get("organization") or litellm.organization
            or get_secret_str("OPENAI_ORGANIZATION") or None
        )
        default_headers = dict(litellm.headers or {})
        default_headers.update(kwargs.get("headers") or {})
        default_headers.update(kwargs.get("extra_headers") or {})
        if litellm.proxy_auth is not None:
            try:
                from litellm.main import _proxy_auth_headers
                default_headers.update(_proxy_auth_headers(litellm.proxy_auth))
            except Exception:
                logger.warning("Failed to resolve LiteLLM proxy auth headers")
        try:
            client = openai.AsyncOpenAI(
                api_key=api_key, base_url=api_base, timeout=timeout,
                max_retries=max_retries, organization=organization,
                default_headers=default_headers or None, http_client=http_client,
            )
        except BaseException:
            await http_client.aclose()
            raise
        try:
            yield client, hooks
        finally:
            await client.close()

    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return
        if not spec.env_key:
            # OAuth/provider-only specs (for example: openai_codex)
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix:
                model = f"{prefix}/{model}"
            return model

        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            model = self._canonicalize_explicit_prefix(model, spec.name, spec.litellm_prefix)
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"

        return model

    @staticmethod
    def _canonicalize_explicit_prefix(model: str, spec_name: str, canonical_prefix: str) -> str:
        """Normalize explicit provider prefixes like `github-copilot/...`."""
        if "/" not in model:
            return model
        prefix, remainder = model.split("/", 1)
        if prefix.lower().replace("-", "_") != spec_name:
            return model
        return f"{canonical_prefix}/{remainder}"

    def _supports_cache_control(self, model: str) -> bool:
        """Return True when the provider supports cache_control on content blocks."""
        if self._gateway is not None:
            return self._gateway.supports_prompt_caching
        spec = find_by_model(model)
        return spec is not None and spec.supports_prompt_caching

    def _apply_cache_control(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Return copies of messages and tools with cache_control injected."""
        new_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg["content"]
                if isinstance(content, str):
                    new_content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                else:
                    new_content = list(content)
                    new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
                new_messages.append({**msg, "content": new_content})
            else:
                new_messages.append(msg)

        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": {"type": "ephemeral"}}

        return new_messages, new_tools

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return

    @staticmethod
    def _extra_msg_keys(original_model: str, resolved_model: str) -> frozenset[str]:
        """Return provider-specific extra keys to preserve in request messages."""
        spec = find_by_model(original_model) or find_by_model(resolved_model)
        if (spec and spec.name == "anthropic") or "claude" in original_model.lower() or resolved_model.startswith("anthropic/"):
            return _ANTHROPIC_EXTRA_KEYS
        return frozenset()

    @staticmethod
    def _normalize_tool_call_id(tool_call_id: Any) -> Any:
        """Normalize tool_call_id to a provider-safe 9-char alphanumeric form."""
        if not isinstance(tool_call_id, str):
            return tool_call_id
        if len(tool_call_id) == 9 and tool_call_id.isalnum():
            return tool_call_id
        return hashlib.sha1(tool_call_id.encode()).hexdigest()[:9]

    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]], extra_keys: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
        """Strip non-standard keys and ensure assistant messages have a content key."""
        allowed = _ALLOWED_MSG_KEYS | extra_keys
        sanitized = LLMProvider._sanitize_request_messages(messages, allowed)
        id_map: dict[str, str] = {}

        def map_id(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return id_map.setdefault(value, LiteLLMProvider._normalize_tool_call_id(value))

        for clean in sanitized:
            # Keep assistant tool_calls[].id and tool tool_call_id in sync after
            # shortening, otherwise strict providers reject the broken linkage.
            if isinstance(clean.get("tool_calls"), list):
                normalized_tool_calls = []
                for tc in clean["tool_calls"]:
                    if not isinstance(tc, dict):
                        normalized_tool_calls.append(tc)
                        continue
                    tc_clean = dict(tc)
                    tc_clean["id"] = map_id(tc_clean.get("id"))
                    normalized_tool_calls.append(tc_clean)
                clean["tool_calls"] = normalized_tool_calls

            if "tool_call_id" in clean and clean["tool_call_id"]:
                clean["tool_call_id"] = map_id(clean["tool_call_id"])
        return sanitized

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        original_model = model or self.default_model
        model = self._resolve_model(original_model)
        extra_msg_keys = self._extra_msg_keys(original_model, model)

        if self._supports_cache_control(original_model):
            messages, tools = self._apply_cache_control(messages, tools)

        # Clamp max_tokens to at least 1 — negative or zero values cause
        # LiteLLM to reject the request with "max_tokens must be at least 1".
        max_tokens = max(1, max_tokens)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_messages(self._sanitize_empty_content(messages), extra_keys=extra_msg_keys),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if self._gateway:
            kwargs.update(self._gateway.litellm_kwargs)

        if not kwargs.get("stream"):
            kwargs.pop("stream_options", None)

        # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
        self._apply_model_overrides(model, kwargs)

        if self._langsmith_enabled:
            kwargs.setdefault("callbacks", []).append("langsmith")

        # Pass api_key directly — more reliable than env vars alone
        if self.api_key:
            kwargs["api_key"] = self.api_key

        # Pass api_base for custom endpoints
        if self.api_base:
            kwargs["api_base"] = self.api_base

        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers
        
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
            kwargs["drop_params"] = True
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        _ensure_litellm()
        hooks = None
        try:
            if intent_journal.current_operation() is not None:
                if not self._supports_journal_route(original_model, kwargs):
                    raise JournalUnavailableError(
                        "journalled LiteLLM route has no supported per-send client guard"
                    )
                async with self._journaled_openai_client(kwargs) as (client, hooks):
                    try:
                        response = await acompletion(**kwargs, client=client)
                    except BaseException:
                        if hooks.failure is not None:
                            raise hooks.failure
                        if not hooks.has_unresolved_attempt:
                            hooks.consume_final()
                        raise
                    if hooks.failure is not None:
                        raise hooks.failure
                    hooks.consume_final()
            else:
                response = await acompletion(**kwargs)
            return self._parse_response(response)
        except JournalUnavailableError:
            raise
        except Exception as e:
            if hooks is not None and hooks.failure is not None:
                raise hooks.failure from e
            if hooks is not None and hooks.has_unresolved_attempt:
                raise JournalUnavailableError(
                    "journalled provider send has unknown settlement"
                ) from e
            # Preserve safe transport metadata for the shared failure writer;
            # never include response body or prompt in the failure stream.
            status = getattr(e, "status_code", None)
            headers = getattr(e, "headers", None)
            if headers is None:
                headers = getattr(getattr(e, "response", None), "headers", None)
            retry_after = None
            if headers is not None:
                try:
                    retry_after = float(headers.get("Retry-After") or headers.get("retry-after"))
                except (TypeError, ValueError):
                    retry_after = None
            return LLMResponse(
                content=f"Error calling LLM: {type(e).__name__}: {str(e)}",
                finish_reason="error",
                usage={"_http_status": status, "_retry_after_s": retry_after},
            )

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        content = message.content
        finish_reason = choice.finish_reason

        # Some providers (e.g. GitHub Copilot) split content and tool_calls
        # across multiple choices. Merge them so tool_calls are not lost.
        raw_tool_calls = []
        for ch in response.choices:
            msg = ch.message
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                raw_tool_calls.extend(msg.tool_calls)
                if ch.finish_reason in ("tool_calls", "stop"):
                    finish_reason = ch.finish_reason
            if not content and msg.content:
                content = msg.content

        if len(response.choices) > 1:
            logger.debug("LiteLLM response has {} choices, merged {} tool_calls",
                         len(response.choices), len(raw_tool_calls))

        tool_calls = []
        for tc in raw_tool_calls:
            # Parse arguments from JSON string if needed
            args = tc.function.arguments
            if isinstance(args, str):
                args = json_repair.loads(args)

            provider_specific_fields = getattr(tc, "provider_specific_fields", None) or None
            function_provider_specific_fields = (
                getattr(tc.function, "provider_specific_fields", None) or None
            )

            tool_calls.append(ToolCallRequest(
                id=_short_tool_id(),
                name=tc.function.name,
                arguments=args,
                provider_specific_fields=provider_specific_fields,
                function_provider_specific_fields=function_provider_specific_fields,
            ))

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        reasoning_content = getattr(message, "reasoning_content", None) or None
        thinking_blocks = getattr(message, "thinking_blocks", None) or None
        # #1660: the deployment that actually answered -- distinct from the
        # requested model group whenever a router fallback substitutes one.
        served_model = getattr(response, "model", None) or None

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
            served_model=served_model,
        )

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
