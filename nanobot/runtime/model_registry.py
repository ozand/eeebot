"""Single source of truth for runtime LLM model selection (#899).

Before this module, model selection was scattered across five call sites
(``llm_proposer._model_name()``, ``bridge.BRIDGE_MODEL``, the bridge's
``_main_impl`` config wiring, ``tool_harness``'s materializer, and
``scripts/memory_archiver.py``), each hand-rolling its own env-var →
config-fallback → hardcoded-default precedence chain. #899 centralizes that
precedence behind one function, :func:`resolve_model`, so every role's
selection logic lives in exactly one place.

Per-role operator env vars (``SUBAGENT_BRIDGE_MODEL``,
``SELFEVO_PROPOSER_MODEL``, ``SELFEVO_SUMMARY_MODEL``, ``LITELLM_MODEL``)
remain the supported way to override a role's model at runtime — this
module does not remove or replace them, it only centralizes the order in
which they are consulted.

This module holds no secrets (model *names* only, never API keys/tokens)
and performs no I/O beyond reading ``os.environ``.

``host/eeepc/etc/models.yaml`` is a SEPARATE artifact — the LiteLLM proxy's
allow-list of models it will route to. It is NOT read here and has no
bearing on this module's precedence chain; a model name resolved here must
separately be present in that allow-list for the proxy to serve it.

This is a behavior-preserving refactor (issue #899): with no new env set,
every role resolves to the exact same model it did before this module
existed. The ``coordinator`` role is registered here for telemetry/
consistency only — ``app/main.py`` still reads ``LITELLM_MODEL`` directly
(legacy call site, out of scope; tracked separately as issue #900).
"""
from __future__ import annotations

import os

# Role names, in a stable order — scorecard iterates this to report each
# role's resolved model for control-plane visibility.
ROLES: tuple[str, ...] = ("proposer", "executor", "harness", "summary", "coordinator")

# Per-role env-var precedence (checked in list order, first non-empty wins)
# and built-in default (used when explicit/env/config_fallback are all
# empty). These MUST reproduce current (pre-#899) behavior exactly.
_ROLE_ENV_VARS: dict[str, tuple[str, ...]] = {
    "proposer": ("SELFEVO_PROPOSER_MODEL", "SUBAGENT_BRIDGE_MODEL"),
    "executor": ("SUBAGENT_BRIDGE_MODEL",),
    "harness": ("SUBAGENT_BRIDGE_MODEL",),
    "summary": ("SELFEVO_SUMMARY_MODEL",),
    "coordinator": ("LITELLM_MODEL",),
}

_ROLE_DEFAULTS: dict[str, str] = {
    "proposer": "cl/gemini-3.5-flash-low",
    "executor": "cl/gemini-3.5-flash-low",
    "harness": "un/qwen3.6-27b-mtp",
    "summary": "cl/gemini-3.5-flash-low",
    "coordinator": "cl/gemini-3.5-flash-low",
}


def resolve_model(
    role: str,
    *,
    explicit: str | None = None,
    config_fallback: str | None = None,
    strip_openai: bool = False,
) -> str:
    """Resolve the model name to use for ``role``.

    Precedence (first non-empty, whitespace-stripped value wins):

    1. ``explicit`` — a caller-passed value (e.g. a function's own ``model``
       kwarg), which always wins when the caller has already decided.
    2. each of ``role``'s env vars, in the order registered in
       :data:`_ROLE_ENV_VARS`.
    3. ``config_fallback`` — a caller-passed config-derived value (e.g.
       ``config.tools.subagent.model``).
    4. the role's built-in default (:data:`_ROLE_DEFAULTS`).

    Empty or whitespace-only strings are treated as unset at every step.
    Fail-soft: never raises. An unknown ``role`` has no env vars and no
    default, so it resolves to ``""`` unless ``explicit``/``config_fallback``
    supply a value.

    If ``strip_openai`` is True, a leading ``"openai/"`` is stripped from
    the final resolved value (only from the front; a no-op if absent).
    """
    try:
        candidates = [explicit]
        for env_var in _ROLE_ENV_VARS.get(role, ()):
            candidates.append(os.environ.get(env_var))
        candidates.append(config_fallback)
        candidates.append(_ROLE_DEFAULTS.get(role, ""))

        result = ""
        for candidate in candidates:
            if candidate is None:
                continue
            stripped = candidate.strip()
            if stripped:
                result = stripped
                break

        if strip_openai and result.startswith("openai/"):
            result = result[len("openai/"):]
        return result
    except Exception:
        return ""
