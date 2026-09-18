"""Resolve a model's context window from the gateway's live ``/model/info``.

Issue #1755: ``llm_calls`` rows record ``prompt_tokens``/``completion_tokens``
but never the model's actual context window, so "how close did we come to
the limit" cannot be computed from the rows alone -- it requires an
out-of-band fact about the model. The static ``litellm.model_cost`` database
used by ``nanobot/cli/model_info.py`` does not know about custom local
gateway routes (e.g. ``un/qwen3.8-27b-gguf``), so this module makes a live
HTTP call to the gateway's own ``/model/info`` endpoint instead.

This lives in the providers layer (not in ``nanobot.observability.llm_telemetry``,
which deliberately stays dumb/data-only and decoupled from anything that
looks like runtime/network behaviour) and not in ``nanobot.runtime`` either,
since ``api_base`` is a provider-layer concept and every current call site
already has it in local scope without touching config loading.

Design constraints (see issue #1755):

- the network call happens at most ONCE per distinct ``(api_base, model)``
  pair for the life of the process -- never a per-call network round trip
  in the hot ``chat_with_retry`` path;
- a lookup failure (network error, non-200, malformed response, model not
  found in the response) is cached as ``None`` too, so a bad or unknown
  route doesn't retry on every subsequent call;
- that failure logs a warning exactly ONCE per ``(api_base, model)`` key,
  not on every call that later hits the cached ``None``;
- the resolver never raises -- callers get ``None`` on any failure, and
  ``None`` is written straight through to ``context_window`` rather than
  ever being guessed or defaulted.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECS = 4.0

# Cache both hits and lookup failures keyed by (api_base, model) so the
# network call happens at most once per distinct pair for the life of the
# process. `_logged_failures` tracks which failed keys have already logged
# their one warning -- kept separate from `_cache` so the log fires exactly
# once, on the call that computes and stores the failure, never again on a
# cache hit of that same `None`.
_cache: dict[tuple[str, str], int | None] = {}
_logged_failures: set[tuple[str, str]] = set()


def resolve_context_window(
    model: str | None,
    api_base: str | None,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECS,
) -> int | None:
    """Return ``model``'s max input tokens per the gateway at ``api_base``, or ``None``.

    Queries ``GET {api_base}/model/info`` and reads ``model_info.max_input_tokens``
    for the response's ``data`` entry whose ``model_name`` equals ``model``.
    Never raises -- any failure (missing inputs, network error, non-200,
    malformed body, model absent from the response) returns ``None``.

    Cached per ``(api_base, model)`` for the life of the process: a resolved
    window and an unknown/failed lookup are both cached, so this performs at
    most one network call per distinct pair, and logs its "window unknown"
    warning at most once per pair.
    """
    if not model or not api_base:
        return None

    # The litellm SDK route head (e.g. "openai/") tells THIS process which
    # HTTP shape to use and is stripped before the model name goes over the
    # wire -- ``nanobot.runtime.model_registry.resolve_model(strip_openai=True)``
    # documents the same split for its OpenAI-SDK callers. The gateway's own
    # ``/model/info`` reports the wire name (e.g. "un/qwen3.8-27b-gguf"), so
    # the lookup -- and its cache key -- must use the wire name, not the
    # route-prefixed one, or every litellm-SDK call site (e.g. the executor's
    # ``openai/un/qwen3.8-27b-gguf``) would never match.
    wire_model = _wire_model_name(model)

    key = (api_base, wire_model)
    if key in _cache:
        return _cache[key]

    window = _fetch_context_window(wire_model, api_base, timeout=timeout)
    _cache[key] = window
    if window is None and key not in _logged_failures:
        _logged_failures.add(key)
        logger.warning(
            "context window unknown for model=%r (wire name %r) via api_base=%r; "
            "llm_calls rows for this route will carry context_window=null",
            model,
            wire_model,
            api_base,
        )
    return window


def _wire_model_name(model: str) -> str:
    """Strip a leading litellm route head (currently just ``"openai/"``) from ``model``."""
    return model[len("openai/"):] if model.startswith("openai/") else model


def _fetch_context_window(model: str, api_base: str, *, timeout: float) -> int | None:
    """Do the actual ``/model/info`` GET and pick out ``max_input_tokens``. Never raises."""
    try:
        url = api_base.rstrip("/") + "/model/info"
        response = httpx.get(url, timeout=timeout)
        if response.status_code != 200:
            return None
        payload = response.json()
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("model_name") != model:
                continue
            model_info = entry.get("model_info")
            if not isinstance(model_info, dict):
                continue
            max_input = model_info.get("max_input_tokens")
            return _as_int(max_input)
        return None
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    """Coerce a JSON-decoded ``max_input_tokens`` to ``int``, or ``None`` if not sane."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _reset_cache_for_tests() -> None:
    """Test-only: clear the module-level cache/log-once state between cases."""
    _cache.clear()
    _logged_failures.clear()
