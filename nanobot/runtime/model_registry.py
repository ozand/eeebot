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

This is a behavior-preserving refactor (issue #899): with no new env set,
every role resolves to the exact same model it did before this module
existed. The ``coordinator`` role is registered here for telemetry/
consistency only. ``app/main.py`` — the dormant coordinator entrypoint that
used to read ``LITELLM_MODEL`` directly — and the LiteLLM proxy's separate
``host/eeepc/etc/models.yaml`` allow-list were both removed in #900; neither
had any bearing on this module's precedence chain.
"""
from __future__ import annotations

import os

# Role names, in a stable order — scorecard iterates this to report each
# role's resolved model for control-plane visibility.
ROLES: tuple[str, ...] = ("proposer", "executor", "harness", "summary", "coordinator", "curator")

# Per-role env-var precedence (checked in list order, first non-empty wins)
# and built-in default (used when explicit/env/config_fallback are all
# empty). These MUST reproduce current (pre-#899) behavior exactly.
_ROLE_ENV_VARS: dict[str, tuple[str, ...]] = {
    "proposer": ("SELFEVO_PROPOSER_MODEL", "SUBAGENT_BRIDGE_MODEL"),
    "executor": ("SUBAGENT_BRIDGE_MODEL",),
    "harness": ("SUBAGENT_BRIDGE_MODEL",),
    "summary": ("SELFEVO_SUMMARY_MODEL",),
    "coordinator": ("LITELLM_MODEL",),
    "curator": ("SELFEVO_CURATOR_MODEL", "SELFEVO_SUMMARY_MODEL"),
}

_ROLE_DEFAULTS: dict[str, str] = {
    "proposer": "cl/gemini-3.5-flash-low",
    "executor": "cl/gemini-3.5-flash-low",
    "harness": "un/qwen3.6-27b-mtp",
    "summary": "cl/gemini-3.5-flash-low",
    "coordinator": "cl/gemini-3.5-flash-low",
    "curator": "cl/gemini-3.5-flash-low",
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
        # Fail-soft to the role's built-in default rather than "" so a
        # resolver bug never sends an empty model string to a provider.
        return _ROLE_DEFAULTS.get(role, "")


# Env var an operator preset (#906) may set to raise/lower the per-spawn
# tool-iteration cap without touching config.json. Read at both bridge spawn
# sites (main + repair) via this one function.
_MAX_TOOL_ITERATIONS_ENV = "SELFEVO_MAX_TOOL_ITERATIONS"


def resolve_max_tool_iterations(config_fallback: int) -> int:
    """Resolve the per-spawn tool-iteration cap (#906).

    Precedence: ``SELFEVO_MAX_TOOL_ITERATIONS`` env var (if it parses as a
    positive integer) wins; otherwise ``config_fallback`` (normally
    ``config.agents.defaults.max_tool_iterations``) is returned unchanged.

    Fail-open by construction: absent, empty, non-integer, zero, negative,
    or otherwise malformed env values are all treated as "unset" and never
    raise — a bad env var must never block a cycle from spawning.
    """
    try:
        raw = os.environ.get(_MAX_TOOL_ITERATIONS_ENV)
        if raw is None:
            return config_fallback
        stripped = raw.strip()
        if not stripped:
            return config_fallback
        value = int(stripped)
        if value <= 0:
            return config_fallback
        return value
    except Exception:
        return config_fallback
