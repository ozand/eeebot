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
ROLES: tuple[str, ...] = ("proposer", "executor", "harness", "summary", "coordinator", "curator", "reflector", "strategist")

# Per-role env-var precedence (checked in list order, first non-empty wins)
# and built-in default (used when explicit/env/config_fallback are all
# empty). These MUST reproduce current (pre-#899) behavior exactly.
_ROLE_ENV_VARS: dict[str, tuple[str, ...]] = {
    "proposer": ("SELFEVO_PROPOSER_MODEL", "SUBAGENT_BRIDGE_MODEL"),
    "executor": ("SUBAGENT_BRIDGE_MODEL",),
    "harness": ("SELFEVO_HARNESS_MODEL", "SUBAGENT_BRIDGE_MODEL"),
    "summary": ("SELFEVO_SUMMARY_MODEL",),
    "coordinator": ("LITELLM_MODEL",),
    "curator": ("SELFEVO_CURATOR_MODEL", "SELFEVO_SUMMARY_MODEL"),
    "reflector": ("SELFEVO_REFLECTOR_MODEL", "SELFEVO_SUMMARY_MODEL"),
    "strategist": ("SELFEVO_STRATEGIST_MODEL", "SELFEVO_SUMMARY_MODEL"),
    "escalation": ("SELFEVO_ESCALATION_MODEL",),
}

# Last-resort fallback: consulted only when a role's env vars AND the caller's
# config_fallback are all empty. The operator preset
# (/etc/eeepc-agent/preset.env) covers proposer/executor/harness/curator/
# reflector/strategist/escalation, so in production these are never reached.
#
# #1363: they must still name a model on a POOL THAT WORKS. The previous value,
# ``cl/gemini-3.5-flash-low``, sat on the `cl/` pool, which returned
# `429 No deployments available` for ~38h on 2026-09-05. Two roles genuinely
# fall through here because the preset defines no env var for them:
#   * ``summary``     -- only env is SELFEVO_SUMMARY_MODEL; live caller is
#                        scripts/memory_archiver.py:42 (no timer today, so the
#                        gun is loaded rather than firing)
#   * ``coordinator`` -- only env is LITELLM_MODEL; no caller remains after
#                        app/main.py was removed in #900
# A fallback that points at a dead pool turns a missing env var into an outage
# instead of a degradation.
#
# #1395: every value is a ROUTED model string -- ``<litellm route>/<gateway
# name>`` -- the same vocabulary the operator presets use for every role env
# var (``SUBAGENT_BRIDGE_MODEL=openai/un/...``). The registry has two kinds
# of caller and the route is what tells them apart:
#   * litellm-SDK callers (the executor, ``bridge.py``) send the string as-is;
#     litellm picks the HTTP shape from the route head. A bare gateway name
#     such as ``an/gemini-3.8-flash-high`` keyword-matches the ``gemini`` spec
#     and is sent as a Google call, which the OpenAI-compatible gateway
#     answers with ``404 {"detail":"Not Found"}`` -- the #1387 outage, which
#     the previous route-less defaults reproduced on any fallthrough.
#   * OpenAI-SDK / raw-HTTP callers (proposer, curator, reflector, strategist,
#     harness, memory_archiver) pass ``strip_openai=True`` and receive the
#     bare gateway name; their path is always ``/chat/completions``.
# So adding a route here changes nothing for stripping callers and fixes the
# non-stripping ones. ``tests/test_model_registry.py`` ratchets both halves:
# every non-empty default must carry a route head that is a provider token
# (``_route_tokens()`` owns that vocabulary), and every stripping caller must
# still receive exactly the bare name.
#
# ``harness`` names the local model the gateway actually serves --
# ``un/qwen3.8-27b-gguf`` (5,750 recorded calls); its predecessor
# ``un/qwen3.6-27b-mtp`` has zero recorded calls on any day and may no longer
# exist on the gateway (#1395).
_ROLE_DEFAULTS: dict[str, str] = {
    "proposer": "openai/an/gemini-3.8-flash-high",
    "executor": "openai/an/gemini-3.8-flash-high",
    "harness": "openai/un/qwen3.8-27b-gguf",
    "summary": "openai/an/gemini-3.8-flash-high",
    "coordinator": "openai/an/gemini-3.8-flash-high",
    "curator": "openai/an/gemini-3.8-flash-high",
    "reflector": "openai/an/gemini-3.8-flash-high",
    "strategist": "openai/an/gemini-3.8-flash-high",
    "escalation": "",
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
    the final resolved value (only from the front; a no-op if absent). Pass
    it when the caller talks to the gateway over the OpenAI SDK / raw HTTP
    (path is always ``/chat/completions``, so the gateway wants the bare
    model name); leave it False when the caller hands the string to the
    ``litellm`` SDK, which needs the route to pick the HTTP shape (#1395).
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

        return _strip_openai(result) if strip_openai else result
    except Exception:
        # Fail-soft to the role's built-in default rather than "" so a
        # resolver bug never sends an empty model string to a provider.
        # #1395: the default is routed, so honour ``strip_openai`` here too --
        # an OpenAI-SDK caller must never receive ``openai/...`` on this path.
        default = _ROLE_DEFAULTS.get(role, "")
        return _strip_openai(default) if strip_openai else default


def _strip_openai(model: str) -> str:
    return model[len("openai/"):] if model.startswith("openai/") else model


def _route_tokens() -> frozenset[str]:
    """Provider tokens that name a litellm route (``openai``, ``gemini``,
    ``openrouter``, …) — the registry is the single owner of that vocabulary.
    Gateway namespaces such as ``an/``, ``un/``, ``cl/`` are NOT routes."""
    try:
        from nanobot.providers.registry import PROVIDERS

        tokens = {s.name for s in PROVIDERS} | {s.litellm_prefix for s in PROVIDERS if s.litellm_prefix}
        return frozenset(t.lower().replace("-", "_") for t in tokens)
    except Exception:
        return frozenset({"openai"})


def _route_head(model: str) -> str:
    return model.split("/", 1)[0].lower().replace("-", "_") if "/" in model else ""


def route_like(base_model: str, candidate: str) -> str:
    """Give ``candidate`` the litellm route ``base_model`` travels on (#1387).

    Observed on the live host: the executor calls the LiteLLM gateway through
    the ``litellm`` SDK, which picks the HTTP shape from the model string's
    provider prefix. ``openai/<gateway-model>`` is an OpenAI-compatible
    ``/chat/completions`` call to ``api_base``; a bare gateway name such as
    ``an/gemini-3.7-flash-high`` keyword-matches the ``gemini`` spec, is sent
    as a Google call to the same ``api_base``, and the gateway answers FastAPI
    ``404 {"detail":"Not Found"}`` (``GeminiException``). The proposer never
    hits this: it uses the OpenAI SDK directly, whose path is always
    ``/chat/completions``.

    Rule: prepend ``<route>/`` iff ``base_model``'s head IS a provider token
    and ``candidate``'s head is NOT one. So ``an/``, ``un/``, ``cl/`` (gateway
    namespaces) get the route; ``gemini/…`` or ``openrouter/…`` (an explicit,
    different route) are left alone; a route-less base copies nothing.
    Idempotent — safe to apply at the producer and again at the consumer.
    """
    base = (base_model or "").strip()
    cand = (candidate or "").strip()
    if not cand:
        return ""
    routes = _route_tokens()
    base_head, cand_head = _route_head(base), _route_head(cand)
    if base_head not in routes or cand_head in routes:
        return cand
    return f"{base.split('/', 1)[0]}/{cand}"


# ── #1104: harness budget resolvers ──────────────────────────────────────────
# Env vars an operator preset may set to tune the harness execution budgets
# independently of the executor.  Read by both skill_eval_harness and
# knowledge_lift through these functions so the two modules stay in sync.
_HARNESS_CASE_TIMEOUT_ENV = "SELFEVO_HARNESS_CASE_TIMEOUT_S"
_HARNESS_CASE_TIMEOUT_DEFAULT = 30.0
_HARNESS_CASE_TIMEOUT_MAX = 600.0

_HARNESS_RUN_BUDGET_ENV = "SELFEVO_HARNESS_RUN_BUDGET_S"
_HARNESS_RUN_BUDGET_DEFAULT = 240.0
_HARNESS_RUN_BUDGET_MAX = 7200.0  # 2 h; operator responsible for sane values

_HARNESS_TOTAL_BUDGET_ENV = "SELFEVO_HARNESS_TOTAL_BUDGET_S"
_HARNESS_TOTAL_BUDGET_DEFAULT = 600.0
_HARNESS_TOTAL_BUDGET_MAX = 14400.0  # 4 h

_HARNESS_MAX_TOKENS_ENV = "SELFEVO_HARNESS_MAX_TOKENS"
_HARNESS_MAX_TOKENS_DEFAULT = 8192
_HARNESS_MAX_TOKENS_MAX = 32768


def _resolve_positive_float(env_var: str, default: float, max_val: float) -> float:
    """Shared helper: parse env var as a positive float clamped to max_val.

    Invalid, empty, non-positive, or absent values fall back to ``default``.
    Never raises.
    """
    try:
        raw = os.environ.get(env_var)
        if raw is None:
            return default
        stripped = raw.strip()
        if not stripped:
            return default
        value = float(stripped)
        if value <= 0:
            return default
        return min(value, max_val)
    except Exception:
        return default


def resolve_harness_case_timeout() -> float:
    """Resolve the harness per-case timeout in seconds (#1104).

    Precedence: ``SELFEVO_HARNESS_CASE_TIMEOUT_S`` env var (if it parses as a
    positive float <= 600) wins; otherwise the hard-coded default of 30 s.

    Invalid, empty, out-of-range, or absent env values fall back to the
    default — a bad env var must never block an eval run.
    """
    return _resolve_positive_float(
        _HARNESS_CASE_TIMEOUT_ENV, _HARNESS_CASE_TIMEOUT_DEFAULT, _HARNESS_CASE_TIMEOUT_MAX
    )


def resolve_harness_run_budget() -> float:
    """Resolve the harness per-skill-run wall-clock budget in seconds (#1104).

    Precedence: ``SELFEVO_HARNESS_RUN_BUDGET_S`` env var wins if it parses as a
    positive float; otherwise the hard-coded default of 240 s.
    """
    return _resolve_positive_float(
        _HARNESS_RUN_BUDGET_ENV, _HARNESS_RUN_BUDGET_DEFAULT, _HARNESS_RUN_BUDGET_MAX
    )


def resolve_harness_total_budget() -> float:
    """Resolve the harness total invocation wall-clock budget in seconds (#1104).

    Precedence: ``SELFEVO_HARNESS_TOTAL_BUDGET_S`` env var wins if it parses as a
    positive float; otherwise the hard-coded default of 600 s.
    """
    return _resolve_positive_float(
        _HARNESS_TOTAL_BUDGET_ENV, _HARNESS_TOTAL_BUDGET_DEFAULT, _HARNESS_TOTAL_BUDGET_MAX
    )


def resolve_harness_max_tokens() -> int:
    """Resolve the harness completion max_tokens parameter (#1104).

    Precedence: ``SELFEVO_HARNESS_MAX_TOKENS`` env var wins if it parses as a
    positive integer <= 32768; otherwise the hard-coded default of 8192.

    Sized for medium thinking + answer on reasoning models; the operator can
    lower it for cost or raise it (to the clamp) for longer outputs.
    """
    try:
        raw = os.environ.get(_HARNESS_MAX_TOKENS_ENV)
        if raw is None:
            return _HARNESS_MAX_TOKENS_DEFAULT
        stripped = raw.strip()
        if not stripped:
            return _HARNESS_MAX_TOKENS_DEFAULT
        value = int(stripped)
        if value <= 0:
            return _HARNESS_MAX_TOKENS_DEFAULT
        return min(value, _HARNESS_MAX_TOKENS_MAX)
    except Exception:
        return _HARNESS_MAX_TOKENS_DEFAULT


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
