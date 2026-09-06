"""Tests for #899: the centralized runtime model resolver.

Covers golden-defaults parity (every role resolves to its pre-#899
hardcoded default with no env set), the per-role precedence chains,
``strip_openai``, whitespace-only-env-is-unset, and a regression-parity
check that the rewritten ``llm_proposer._model_name()`` still returns
exactly what it did before the refactor for representative env combos.
"""
from __future__ import annotations

import pytest

from nanobot.runtime import llm_proposer, model_registry
from nanobot.runtime.model_registry import (
    resolve_harness_case_timeout,
    resolve_harness_max_tokens,
    resolve_harness_run_budget,
    resolve_harness_total_budget,
    resolve_max_tool_iterations,
    resolve_model,
)

_ALL_MODEL_ENV_VARS = (
    "SELFEVO_PROPOSER_MODEL",
    "SUBAGENT_BRIDGE_MODEL",
    "SELFEVO_SUMMARY_MODEL",
    "LITELLM_MODEL",
    "SELFEVO_CURATOR_MODEL",
    "SELFEVO_REFLECTOR_MODEL",
    "SELFEVO_STRATEGIST_MODEL",
    "SELFEVO_HARNESS_MODEL",
    "SELFEVO_ESCALATION_MODEL",
)


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    """No host bleed: clear every model-selection env var before each test."""
    for var in _ALL_MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ─── GOLDEN DEFAULTS ──────────────────────────────────────────────────────────

def test_golden_default_proposer():
    assert resolve_model("proposer") == "an/gemini-3.8-flash-high"


def test_golden_default_executor():
    assert resolve_model("executor") == "an/gemini-3.8-flash-high"


def test_escalation_model_is_opt_in(monkeypatch):
    assert resolve_model("escalation") == ""
    monkeypatch.setenv("SELFEVO_ESCALATION_MODEL", "an/frontier-model")
    assert resolve_model("escalation") == "an/frontier-model"


def test_golden_default_harness():
    assert resolve_model("harness") == "un/qwen3.6-27b-mtp"


def test_golden_default_summary():
    assert resolve_model("summary") == "an/gemini-3.8-flash-high"


def test_golden_default_coordinator():
    assert resolve_model("coordinator") == "an/gemini-3.8-flash-high"


def test_golden_default_curator():
    assert resolve_model("curator") == "an/gemini-3.8-flash-high"


def test_golden_default_reflector():
    assert resolve_model("reflector") == "an/gemini-3.8-flash-high"


def test_golden_default_strategist():
    assert resolve_model("strategist") == "an/gemini-3.8-flash-high"


def test_strategist_env_override(monkeypatch):
    monkeypatch.setenv("SELFEVO_STRATEGIST_MODEL", "an/strategist-model")
    assert resolve_model("strategist") == "an/strategist-model"


def test_strategist_fallback_to_summary_env(monkeypatch):
    monkeypatch.setenv("SELFEVO_SUMMARY_MODEL", "an/summary-model")
    assert resolve_model("strategist") == "an/summary-model"


def test_curator_env_override(monkeypatch):
    monkeypatch.setenv("SELFEVO_CURATOR_MODEL", "an/curator-model")
    assert resolve_model("curator") == "an/curator-model"


def test_roles_constant_covers_every_documented_role():
    assert set(model_registry.ROLES) == {
        "proposer",
        "executor",
        "harness",
        "summary",
        "coordinator",
        "curator",
        "reflector",
        "strategist",
    }


# ─── proposer precedence ──────────────────────────────────────────────────────

def test_proposer_selfevo_wins_over_bridge_env(monkeypatch):
    monkeypatch.setenv("SELFEVO_PROPOSER_MODEL", "an/proposer-model")
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("proposer") == "an/proposer-model"


def test_proposer_falls_back_to_bridge_env(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("proposer") == "an/bridge-model"


def test_proposer_falls_back_to_default_when_both_unset():
    assert resolve_model("proposer") == "an/gemini-3.8-flash-high"


# ─── executor precedence ──────────────────────────────────────────────────────

def test_executor_uses_config_fallback_only_when_env_unset():
    assert resolve_model("executor", config_fallback="cfg/model") == "cfg/model"


def test_executor_env_wins_over_config_fallback(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("executor", config_fallback="cfg/model") == "an/bridge-model"


def test_executor_default_when_env_and_config_fallback_unset():
    assert resolve_model("executor") == "an/gemini-3.8-flash-high"
    assert resolve_model("executor", config_fallback=None) == "an/gemini-3.8-flash-high"


# ─── harness precedence ────────────────────────────────────────────────────────

def test_harness_explicit_wins_over_env_and_config_fallback(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert (
        resolve_model("harness", explicit="an/explicit-model", config_fallback="cfg/model")
        == "an/explicit-model"
    )


def test_harness_env_wins_over_config_fallback_when_no_explicit(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("harness", config_fallback="cfg/model") == "an/bridge-model"


def test_harness_config_fallback_used_when_env_and_explicit_unset():
    assert resolve_model("harness", config_fallback="cfg/model") == "cfg/model"


def test_harness_default_when_everything_unset():
    assert resolve_model("harness") == "un/qwen3.6-27b-mtp"


# #1104: SELFEVO_HARNESS_MODEL dedicated precedence for the harness role

def test_harness_selfevo_harness_model_wins_over_bridge(monkeypatch):
    """SELFEVO_HARNESS_MODEL beats SUBAGENT_BRIDGE_MODEL for harness role."""
    monkeypatch.setenv("SELFEVO_HARNESS_MODEL", "an/harness-fast")
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-slow")
    assert resolve_model("harness") == "an/harness-fast"


def test_harness_falls_back_to_bridge_when_selfevo_harness_model_unset(monkeypatch):
    """When SELFEVO_HARNESS_MODEL is unset, falls back to SUBAGENT_BRIDGE_MODEL."""
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("harness") == "an/bridge-model"


def test_harness_selfevo_harness_model_whitespace_is_unset(monkeypatch):
    """Whitespace-only SELFEVO_HARNESS_MODEL is treated as unset."""
    monkeypatch.setenv("SELFEVO_HARNESS_MODEL", "   ")
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("harness") == "an/bridge-model"


def test_harness_selfevo_harness_model_empty_is_unset(monkeypatch):
    """Empty SELFEVO_HARNESS_MODEL is treated as unset."""
    monkeypatch.setenv("SELFEVO_HARNESS_MODEL", "")
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("harness") == "an/bridge-model"


def test_harness_falls_back_to_default_when_all_envs_unset():
    """With both env vars unset and no explicit/config, falls back to built-in default."""
    assert resolve_model("harness") == "un/qwen3.6-27b-mtp"


def test_harness_selfevo_harness_model_does_not_affect_executor(monkeypatch):
    """SELFEVO_HARNESS_MODEL does not bleed into the executor role."""
    monkeypatch.setenv("SELFEVO_HARNESS_MODEL", "an/harness-model")
    assert resolve_model("executor") == "an/gemini-3.8-flash-high"


def test_harness_selfevo_harness_model_does_not_affect_proposer(monkeypatch):
    """SELFEVO_HARNESS_MODEL does not bleed into the proposer role."""
    monkeypatch.setenv("SELFEVO_HARNESS_MODEL", "an/harness-model")
    assert resolve_model("proposer") == "an/gemini-3.8-flash-high"


# ─── summary ──────────────────────────────────────────────────────────────────

def test_summary_env_override(monkeypatch):
    monkeypatch.setenv("SELFEVO_SUMMARY_MODEL", "an/summary-model")
    assert resolve_model("summary") == "an/summary-model"


def test_summary_default_when_unset():
    assert resolve_model("summary") == "an/gemini-3.8-flash-high"


# ─── strip_openai ─────────────────────────────────────────────────────────────

def test_strip_openai_strips_leading_prefix_only(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "openai/an/gemini-3.7-flash-high")
    assert resolve_model("executor", strip_openai=True) == "an/gemini-3.7-flash-high"


def test_strip_openai_noop_when_absent(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/gemini-3.7-flash-high")
    assert resolve_model("executor", strip_openai=True) == "an/gemini-3.7-flash-high"


def test_strip_openai_only_strips_leading_occurrence():
    # "openai/" appearing only mid-string (not at the very front) is untouched
    assert (
        resolve_model("proposer", explicit="an/not-openai/inner", strip_openai=True)
        == "an/not-openai/inner"
    )


def test_strip_openai_false_by_default_leaves_prefix(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "openai/an/gemini-3.7-flash-high")
    assert resolve_model("executor") == "openai/an/gemini-3.7-flash-high"


# ─── whitespace-only env treated as unset ─────────────────────────────────────

def test_whitespace_only_env_treated_as_unset(monkeypatch):
    monkeypatch.setenv("SELFEVO_PROPOSER_MODEL", "   ")
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "  \t  ")
    assert resolve_model("proposer") == "an/gemini-3.8-flash-high"


def test_whitespace_only_explicit_treated_as_unset(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("executor", explicit="   ") == "an/bridge-model"


def test_whitespace_only_config_fallback_treated_as_unset():
    assert resolve_model("executor", config_fallback="   ") == "an/gemini-3.8-flash-high"


def test_value_is_stripped_of_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "  an/bridge-model  ")
    assert resolve_model("executor") == "an/bridge-model"


# ─── unknown role / fail-soft ──────────────────────────────────────────────────

def test_unknown_role_with_no_explicit_or_fallback_returns_empty_string():
    assert resolve_model("nonexistent-role") == ""


def test_unknown_role_still_honors_explicit_and_config_fallback():
    assert resolve_model("nonexistent-role", explicit="an/x") == "an/x"
    assert resolve_model("nonexistent-role", config_fallback="an/y") == "an/y"


# ─── REGRESSION PARITY: llm_proposer._model_name() vs the resolver ────────────

@pytest.mark.parametrize(
    "proposer_env,bridge_env",
    [
        (None, None),
        ("an/proposer-model", None),
        (None, "an/bridge-model"),
        (None, "openai/an/gemini-3.7-flash-high"),
        ("openai/an/proposer-model", "an/bridge-model"),
    ],
)
def test_model_name_parity_with_resolver(monkeypatch, proposer_env, bridge_env):
    if proposer_env is not None:
        monkeypatch.setenv("SELFEVO_PROPOSER_MODEL", proposer_env)
    if bridge_env is not None:
        monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", bridge_env)

    assert llm_proposer._model_name() == resolve_model("proposer", strip_openai=True)


# ─── FAIL-SOFT: resolver never returns "" for a known role ────────────────────

def test_resolve_model_failsoft_returns_role_default(monkeypatch):
    # A non-str explicit makes the internal .strip() raise; the except path
    # must yield the role's built-in default, never an empty model string.
    for role, expected in (
        ("proposer", "an/gemini-3.8-flash-high"),
        ("executor", "an/gemini-3.8-flash-high"),
        ("harness", "un/qwen3.6-27b-mtp"),
        ("summary", "an/gemini-3.8-flash-high"),
        ("coordinator", "an/gemini-3.8-flash-high"),
        ("curator", "an/gemini-3.8-flash-high"),
    ):
        assert resolve_model(role, explicit=123) == expected


# ─── #906: resolve_max_tool_iterations — operator preset iteration cap ────────


@pytest.fixture(autouse=True)
def _clean_max_iterations_env(monkeypatch):
    monkeypatch.delenv("SELFEVO_MAX_TOOL_ITERATIONS", raising=False)


def test_max_iterations_absent_env_falls_back_to_config():
    assert resolve_max_tool_iterations(40) == 40


def test_max_iterations_empty_env_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "")
    assert resolve_max_tool_iterations(40) == 40


def test_max_iterations_whitespace_only_env_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "   ")
    assert resolve_max_tool_iterations(40) == 40


def test_max_iterations_garbage_env_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "not-a-number")
    assert resolve_max_tool_iterations(40) == 40


def test_max_iterations_float_string_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "80.0")
    assert resolve_max_tool_iterations(40) == 40


def test_max_iterations_zero_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "0")
    assert resolve_max_tool_iterations(40) == 40


def test_max_iterations_negative_falls_back_to_config(monkeypatch):
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "-5")
    assert resolve_max_tool_iterations(40) == 40


def test_max_iterations_valid_positive_int_is_honored(monkeypatch):
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "80")
    assert resolve_max_tool_iterations(40) == 80


def test_max_iterations_valid_int_with_surrounding_whitespace_is_honored(monkeypatch):
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "  80  ")
    assert resolve_max_tool_iterations(40) == 80


def test_max_iterations_huge_value_is_honored(monkeypatch):
    # No upper cap is imposed by the resolver itself — an operator asking
    # for a very deep cycle gets exactly that; any sanity ceiling belongs to
    # the caller/operator, not this fail-open resolver.
    monkeypatch.setenv("SELFEVO_MAX_TOOL_ITERATIONS", "1000000")
    assert resolve_max_tool_iterations(40) == 1_000_000


def test_max_iterations_never_raises_on_non_int_config_fallback_type():
    # Fail-open contract: even a malformed call site (wrong fallback type)
    # must not raise — absent env just returns the fallback verbatim.
    sentinel = object()
    assert resolve_max_tool_iterations(sentinel) is sentinel


# ─── #1104: resolve_harness_case_timeout ──────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_harness_timeout_env(monkeypatch):
    monkeypatch.delenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("SELFEVO_HARNESS_RUN_BUDGET_S", raising=False)
    monkeypatch.delenv("SELFEVO_HARNESS_TOTAL_BUDGET_S", raising=False)
    monkeypatch.delenv("SELFEVO_HARNESS_MAX_TOKENS", raising=False)


def test_harness_case_timeout_default_is_30():
    """Default case timeout is 30 seconds when env is unset."""
    assert resolve_harness_case_timeout() == 30.0


def test_harness_case_timeout_unset_returns_default(monkeypatch):
    """Absent env returns the default."""
    monkeypatch.delenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", raising=False)
    assert resolve_harness_case_timeout() == 30.0


def test_harness_case_timeout_empty_returns_default(monkeypatch):
    """Empty string returns the default (not an error)."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "")
    assert resolve_harness_case_timeout() == 30.0


def test_harness_case_timeout_whitespace_returns_default(monkeypatch):
    """Whitespace-only env returns the default."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "   ")
    assert resolve_harness_case_timeout() == 30.0


def test_harness_case_timeout_valid_float_honored(monkeypatch):
    """A valid positive float <= 120 is honored exactly."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "60.0")
    assert resolve_harness_case_timeout() == 60.0


def test_harness_case_timeout_valid_int_honored(monkeypatch):
    """An integer value is parsed as float."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "90")
    assert resolve_harness_case_timeout() == 90.0


def test_harness_case_timeout_clamped_at_600(monkeypatch):
    """Values above 600 are clamped to 600."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "900")
    assert resolve_harness_case_timeout() == 600.0


def test_harness_case_timeout_exactly_600_honored(monkeypatch):
    """Value of exactly 600 is not clamped."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "600")
    assert resolve_harness_case_timeout() == 600.0


def test_harness_case_timeout_live_value_300_honored(monkeypatch):
    """Live verification value of 300 s is within the clamp."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "300")
    assert resolve_harness_case_timeout() == 300.0


def test_harness_case_timeout_garbage_returns_default(monkeypatch):
    """Non-numeric env returns the default (fail-open)."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "not-a-number")
    assert resolve_harness_case_timeout() == 30.0


def test_harness_case_timeout_negative_returns_default(monkeypatch):
    """Zero or negative value returns the default."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "-5")
    assert resolve_harness_case_timeout() == 30.0
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "0")
    assert resolve_harness_case_timeout() == 30.0


def test_harness_case_timeout_whitespace_stripped_value_honored(monkeypatch):
    """Values with surrounding whitespace are parsed correctly."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "  45  ")
    assert resolve_harness_case_timeout() == 45.0


# ─── #1104: resolve_harness_run_budget ───────────────────────────────────────


def test_harness_run_budget_default_is_240():
    """Default run budget is 240 s when env is unset."""
    assert resolve_harness_run_budget() == 240.0


def test_harness_run_budget_empty_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "")
    assert resolve_harness_run_budget() == 240.0


def test_harness_run_budget_whitespace_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "   ")
    assert resolve_harness_run_budget() == 240.0


def test_harness_run_budget_valid_honored(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "1800")
    assert resolve_harness_run_budget() == 1800.0


def test_harness_run_budget_negative_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "-1")
    assert resolve_harness_run_budget() == 240.0


def test_harness_run_budget_zero_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "0")
    assert resolve_harness_run_budget() == 240.0


def test_harness_run_budget_garbage_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "bad")
    assert resolve_harness_run_budget() == 240.0


def test_harness_run_budget_whitespace_stripped_honored(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "  600  ")
    assert resolve_harness_run_budget() == 600.0


def test_harness_run_budget_clamp_at_7200(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "99999")
    assert resolve_harness_run_budget() == 7200.0


# ─── #1104: resolve_harness_total_budget ─────────────────────────────────────


def test_harness_total_budget_default_is_600():
    """Default total budget is 600 s when env is unset."""
    assert resolve_harness_total_budget() == 600.0


def test_harness_total_budget_empty_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_TOTAL_BUDGET_S", "")
    assert resolve_harness_total_budget() == 600.0


def test_harness_total_budget_whitespace_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_TOTAL_BUDGET_S", "   ")
    assert resolve_harness_total_budget() == 600.0


def test_harness_total_budget_valid_honored(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_TOTAL_BUDGET_S", "3600")
    assert resolve_harness_total_budget() == 3600.0


def test_harness_total_budget_negative_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_TOTAL_BUDGET_S", "-1")
    assert resolve_harness_total_budget() == 600.0


def test_harness_total_budget_garbage_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_TOTAL_BUDGET_S", "abc")
    assert resolve_harness_total_budget() == 600.0


def test_harness_total_budget_clamp_at_14400(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_TOTAL_BUDGET_S", "99999")
    assert resolve_harness_total_budget() == 14400.0


def test_harness_total_budget_live_value_3600_honored(monkeypatch):
    """Live verification value of 3600 s is within the clamp."""
    monkeypatch.setenv("SELFEVO_HARNESS_TOTAL_BUDGET_S", "3600")
    assert resolve_harness_total_budget() == 3600.0


# ─── #1104: resolve_harness_max_tokens ───────────────────────────────────────


def test_harness_max_tokens_default_is_8192():
    """Default max_tokens is 8192 when env is unset."""
    assert resolve_harness_max_tokens() == 8192


def test_harness_max_tokens_empty_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "")
    assert resolve_harness_max_tokens() == 8192


def test_harness_max_tokens_whitespace_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "   ")
    assert resolve_harness_max_tokens() == 8192


def test_harness_max_tokens_valid_honored(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "4096")
    assert resolve_harness_max_tokens() == 4096


def test_harness_max_tokens_8192_honored(monkeypatch):
    """Live verification value 8192 is not clamped."""
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "8192")
    assert resolve_harness_max_tokens() == 8192


def test_harness_max_tokens_clamp_at_32768(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "99999")
    assert resolve_harness_max_tokens() == 32768


def test_harness_max_tokens_zero_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "0")
    assert resolve_harness_max_tokens() == 8192


def test_harness_max_tokens_negative_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "-100")
    assert resolve_harness_max_tokens() == 8192


def test_harness_max_tokens_float_string_returns_default(monkeypatch):
    """Floats are not valid integers; fall back to default."""
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "4096.5")
    assert resolve_harness_max_tokens() == 8192


def test_harness_max_tokens_garbage_returns_default(monkeypatch):
    monkeypatch.setenv("SELFEVO_HARNESS_MAX_TOKENS", "big")
    assert resolve_harness_max_tokens() == 8192


def test_no_role_default_points_at_a_pool_that_is_known_dead():
    """#1363: the last-resort default must name a model on a working pool.

    The previous value sat on the ``cl/`` pool, which returned
    ``429 No deployments available`` for ~38h on 2026-09-05. Two roles really
    do fall through to these defaults because the operator preset defines no
    env var for them (``summary`` -- live caller
    ``scripts/memory_archiver.py``; ``coordinator`` -- no caller since #900),
    so a default on a dead pool turns a missing env var into an outage.

    This is a ratchet, not a style rule: it fails if anyone reintroduces a
    ``cl/`` default, whichever role it lands on.
    """
    from nanobot.runtime.model_registry import _ROLE_DEFAULTS

    offenders = {
        role: model
        for role, model in _ROLE_DEFAULTS.items()
        if model.startswith("cl/")
    }
    assert not offenders, (
        f"role defaults on the retired cl/ pool: {offenders}; "
        "point them at a pool that answers"
    )


def test_every_role_has_either_a_default_or_an_explicit_empty():
    """A role with neither an env var set nor a default resolves to ''.

    Guards the shape rather than the values: an empty default is a deliberate
    choice (``escalation``), a missing key is not.
    """
    from nanobot.runtime.model_registry import _ROLE_DEFAULTS, _ROLE_ENV_VARS

    missing = set(_ROLE_ENV_VARS) - set(_ROLE_DEFAULTS)
    assert not missing, f"roles with env vars but no registered default: {missing}"
