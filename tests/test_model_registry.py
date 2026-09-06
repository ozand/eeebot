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
    assert resolve_model("proposer") == "openai/an/gemini-3.8-flash-high"


def test_golden_default_executor():
    assert resolve_model("executor") == "openai/an/gemini-3.8-flash-high"


def test_escalation_model_is_opt_in(monkeypatch):
    assert resolve_model("escalation") == ""
    monkeypatch.setenv("SELFEVO_ESCALATION_MODEL", "an/frontier-model")
    assert resolve_model("escalation") == "an/frontier-model"


def test_golden_default_harness():
    assert resolve_model("harness") == "openai/un/qwen3.8-27b-gguf"


def test_golden_default_summary():
    assert resolve_model("summary") == "openai/an/gemini-3.8-flash-high"


def test_golden_default_coordinator():
    assert resolve_model("coordinator") == "openai/an/gemini-3.8-flash-high"


def test_golden_default_curator():
    assert resolve_model("curator") == "openai/an/gemini-3.8-flash-high"


def test_golden_default_reflector():
    assert resolve_model("reflector") == "openai/an/gemini-3.8-flash-high"


def test_golden_default_strategist():
    assert resolve_model("strategist") == "openai/an/gemini-3.8-flash-high"


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
    assert resolve_model("proposer") == "openai/an/gemini-3.8-flash-high"


# ─── executor precedence ──────────────────────────────────────────────────────

def test_executor_uses_config_fallback_only_when_env_unset():
    assert resolve_model("executor", config_fallback="cfg/model") == "cfg/model"


def test_executor_env_wins_over_config_fallback(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("executor", config_fallback="cfg/model") == "an/bridge-model"


def test_executor_default_when_env_and_config_fallback_unset():
    assert resolve_model("executor") == "openai/an/gemini-3.8-flash-high"
    assert resolve_model("executor", config_fallback=None) == "openai/an/gemini-3.8-flash-high"


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
    assert resolve_model("harness") == "openai/un/qwen3.8-27b-gguf"


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
    assert resolve_model("harness") == "openai/un/qwen3.8-27b-gguf"


def test_harness_selfevo_harness_model_does_not_affect_executor(monkeypatch):
    """SELFEVO_HARNESS_MODEL does not bleed into the executor role."""
    monkeypatch.setenv("SELFEVO_HARNESS_MODEL", "an/harness-model")
    assert resolve_model("executor") == "openai/an/gemini-3.8-flash-high"


def test_harness_selfevo_harness_model_does_not_affect_proposer(monkeypatch):
    """SELFEVO_HARNESS_MODEL does not bleed into the proposer role."""
    monkeypatch.setenv("SELFEVO_HARNESS_MODEL", "an/harness-model")
    assert resolve_model("proposer") == "openai/an/gemini-3.8-flash-high"


# ─── summary ──────────────────────────────────────────────────────────────────

def test_summary_env_override(monkeypatch):
    monkeypatch.setenv("SELFEVO_SUMMARY_MODEL", "an/summary-model")
    assert resolve_model("summary") == "an/summary-model"


def test_summary_default_when_unset():
    assert resolve_model("summary") == "openai/an/gemini-3.8-flash-high"


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
    assert resolve_model("proposer") == "openai/an/gemini-3.8-flash-high"


def test_whitespace_only_explicit_treated_as_unset(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("executor", explicit="   ") == "an/bridge-model"


def test_whitespace_only_config_fallback_treated_as_unset():
    assert resolve_model("executor", config_fallback="   ") == "openai/an/gemini-3.8-flash-high"


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
        ("proposer", "openai/an/gemini-3.8-flash-high"),
        ("executor", "openai/an/gemini-3.8-flash-high"),
        ("harness", "openai/un/qwen3.8-27b-gguf"),
        ("summary", "openai/an/gemini-3.8-flash-high"),
        ("coordinator", "openai/an/gemini-3.8-flash-high"),
        ("curator", "openai/an/gemini-3.8-flash-high"),
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

    #1395: defaults now carry a litellm route (``openai/cl/...``), so the pool
    is read from the gateway name AFTER the route, not from the raw prefix --
    a ``startswith("cl/")`` check would have silently passed a routed value.
    """
    from nanobot.runtime.model_registry import _ROLE_DEFAULTS

    offenders = {
        role: model
        for role, model in _ROLE_DEFAULTS.items()
        if _gateway_name(model).startswith("cl/")
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


# ─── #1395: the default tier must be routable for a NON-stripping caller ──────
#
# Two kinds of caller share the registry. The OpenAI-SDK / raw-HTTP callers
# (proposer, curator, reflector, strategist, both harness sites, and
# scripts/memory_archiver.py) pass ``strip_openai=True`` and want the bare
# gateway name -- their path is always ``/chat/completions``. The executor
# (``bridge.py``) and ``demand.escalation_model()`` do NOT strip: they hand the
# string to the litellm SDK, which picks the HTTP shape from the route head. A
# route-less value there keyword-matches the ``gemini`` spec, goes out as a
# Google-shaped call, and the OpenAI-compatible gateway answers
# ``404 {"detail":"Not Found"}`` (#1387). Until #1395 every ``_ROLE_DEFAULTS``
# value had that shape, so any executor fallthrough reproduced the outage.

# The bare gateway name each strip_openai=True call site receives with no env
# set. For every role but ``harness`` this is byte-for-byte what the site
# received before #1395 added routes to the defaults. ``harness`` changed
# MODEL (never-called ``un/qwen3.6-27b-mtp`` -> served ``un/qwen3.8-27b-gguf``,
# defect 2 of #1395); that is a deliberate value change, not a route leak.
_STRIP_CALLER_EXPECTED = {
    "proposer": "an/gemini-3.8-flash-high",    # llm_proposer._model_name
    "curator": "an/gemini-3.8-flash-high",     # knowledge_curator
    "reflector": "an/gemini-3.8-flash-high",   # reflector
    "strategist": "an/gemini-3.8-flash-high",  # strategist
    "harness": "un/qwen3.8-27b-gguf",          # skill_eval_harness, knowledge_lift
    "summary": "an/gemini-3.8-flash-high",     # scripts/memory_archiver
}

# The LiteLLM gateway is OpenAI-compatible: every preset env value for every
# role travels the ``openai`` route. Changing the gateway's protocol is an
# operator decision that would update this pin deliberately.
_GATEWAY_ROUTE = "openai"


def _gateway_name(model: str) -> str:
    """Strip a leading litellm route (a provider token) off ``model``.

    ``openai/an/x`` -> ``an/x``; ``an/x`` -> ``an/x`` (``an`` is a gateway
    namespace, not a route). Uses the registry's own vocabulary so this helper
    cannot disagree with ``route_like``.
    """
    from nanobot.runtime.model_registry import _route_head, _route_tokens

    return model.split("/", 1)[1] if _route_head(model) in _route_tokens() else model


def _nonempty_defaults() -> dict[str, str]:
    from nanobot.runtime.model_registry import _ROLE_DEFAULTS

    return {role: model for role, model in _ROLE_DEFAULTS.items() if model}


def test_every_nonempty_role_default_carries_a_litellm_route():
    """#1395 ratchet: a default added without a route fails here.

    The head of every non-empty default must be a provider token from the
    registry (``_route_tokens()`` derives the vocabulary from ``PROVIDERS``),
    never a gateway namespace such as ``an/``, ``un/`` or ``cl/``. #1366
    shipped a default nobody could reach and nothing noticed for six days;
    this is the check whose absence let that happen.
    """
    from nanobot.runtime.model_registry import _route_head, _route_tokens

    routes = _route_tokens()
    offenders = {
        role: model for role, model in _nonempty_defaults().items()
        if _route_head(model) not in routes
    }
    assert not offenders, (
        f"role defaults without a litellm route: {offenders}; the executor "
        f"does not strip, so a bare gateway name here is the #1387 404 shape. "
        f"Prefix the route (e.g. {_GATEWAY_ROUTE}/<gateway-name>)"
    )


def test_every_role_default_is_sent_on_the_gateway_route_by_the_litellm_client():
    """Acceptance (#1395): with no env set, ``resolve_model(role)`` returns a
    string the litellm client sends on the intended route.

    Pins the mechanism end to end, not just the prefix: the provider registry
    must classify the resolved string by its route head (``openai``), not by a
    keyword deeper in the string (``gemini``), and the live-like
    ``LiteLLMProvider`` in standard mode must pass it through unchanged rather
    than re-prefixing it onto another route.
    """
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.registry import find_by_model

    for role in _nonempty_defaults():
        resolved = resolve_model(role)
        spec = find_by_model(resolved)
        assert spec is not None and spec.name == _GATEWAY_ROUTE, (
            f"{role}: {resolved!r} classified as {getattr(spec, 'name', None)!r}"
        )
        provider = LiteLLMProvider(
            api_key="k",
            api_base="http://gateway.invalid/v1",
            default_model=resolved,
            provider_name=_GATEWAY_ROUTE,
        )
        assert provider._gateway is None, f"{role}: live-like provider must not be gateway mode"
        assert provider._resolve_model(resolved) == resolved, (
            f"{role}: litellm client would rewrite {resolved!r} to "
            f"{provider._resolve_model(resolved)!r}"
        )


def test_pre_1395_bare_default_shape_never_reaches_the_gateway_route():
    """Mechanism pin for the reader: the OLD default shape (route-less
    gateway name) is never classified onto the OpenAI-compatible route.

    Two sub-shapes, both non-deliverable through the litellm SDK:
    * ``an/gemini-...`` keyword-matches the ``gemini`` spec and goes out as a
      Google-shaped call -> ``404 {"detail":"Not Found"}`` (#1387).
    * ``un/qwen...`` matches no spec at all, so the provider passes it to the
      litellm SDK with no route and litellm refuses it as a model with no
      provider before any HTTP is sent.
    If this ever stops holding, the ratchet above is guarding a non-problem
    and should be revisited rather than kept by inertia."""
    from nanobot.providers.registry import find_by_model

    for role, model in _nonempty_defaults().items():
        bare = _gateway_name(model)
        assert bare != model, f"{role}: default {model!r} has no route to strip"
        spec = find_by_model(bare)
        assert spec is None or spec.name != _GATEWAY_ROUTE, (
            f"{role}: bare {bare!r} is classified onto {_GATEWAY_ROUTE!r} -- the "
            "route-less shape would now be deliverable; revisit the ratchet"
        )


def test_strip_callers_receive_exactly_the_bare_gateway_name():
    """Constraint (#1395): adding routes to the default tier must leave every
    ``strip_openai=True`` call site resolving to exactly what it resolved to
    before. Proven by value, not by inspection."""
    from nanobot.runtime.model_registry import ROLES, _route_head, _route_tokens

    for role, expected in _STRIP_CALLER_EXPECTED.items():
        assert resolve_model(role, strip_openai=True) == expected, role

    # And as a shape rule for EVERY role, including ones no strip caller uses
    # today: a stripping caller never sees a route head.
    routes = _route_tokens()
    for role in (*ROLES, "escalation"):
        stripped = resolve_model(role, strip_openai=True)
        assert _route_head(stripped) not in routes, (role, stripped)


def test_strip_callers_are_the_gateway_name_of_the_unstripped_default():
    """The two views of one default agree: strip == route removed, nothing
    else. Guards against a future default whose strip result is not simply
    the unstripped value minus its route."""
    for role, model in _nonempty_defaults().items():
        assert resolve_model(role, strip_openai=True) == _gateway_name(model), role
        assert resolve_model(role) == model, role


@pytest.mark.parametrize("role", sorted(_STRIP_CALLER_EXPECTED))
def test_strip_caller_env_override_unaffected_by_default_route(monkeypatch, role):
    """An operator env value (routed, as every preset writes it, or bare)
    still resolves for a strip caller to the bare gateway name -- the
    default's route is never consulted once an env var is set."""
    from nanobot.runtime.model_registry import _ROLE_ENV_VARS

    env_var = _ROLE_ENV_VARS[role][0]
    monkeypatch.setenv(env_var, "openai/an/operator-choice")
    assert resolve_model(role, strip_openai=True) == "an/operator-choice"
    monkeypatch.setenv(env_var, "an/operator-choice")
    assert resolve_model(role, strip_openai=True) == "an/operator-choice"


def test_failsoft_path_honours_strip_openai():
    """#1395: the fail-soft ``except`` path returns the routed default; a
    stripping caller on that path must still get the bare name, or a
    resolver bug would hand the OpenAI SDK an ``openai/...`` string."""
    assert resolve_model("proposer", explicit=123, strip_openai=True) == "an/gemini-3.8-flash-high"  # type: ignore[arg-type]
    assert resolve_model("proposer", explicit=123) == "openai/an/gemini-3.8-flash-high"  # type: ignore[arg-type]


def test_executor_config_fallback_tier_shares_the_route_vocabulary():
    """``config.tools.subagent.model`` is the executor's ``config_fallback``
    tier and the executor does not strip, so the schema default must be
    routed exactly like ``_ROLE_DEFAULTS`` (#1395, defect 2: the previous
    value was both route-less and a model with zero recorded calls)."""
    from nanobot.config.schema import SubagentToolConfig
    from nanobot.providers.registry import find_by_model
    from nanobot.runtime.model_registry import _route_head, _route_tokens

    cfg_model = SubagentToolConfig().model
    assert _route_head(cfg_model) in _route_tokens(), cfg_model
    spec = find_by_model(cfg_model)
    assert spec is not None and spec.name == _GATEWAY_ROUTE, cfg_model
    # The executor receives the config tier unstripped and routed.
    assert resolve_model("executor", config_fallback=cfg_model) == cfg_model


def test_no_default_names_the_never_called_qwen36():
    """#1395 defect 2 ratchet: ``un/qwen3.6-27b-mtp`` has zero recorded calls
    on any day (vs 5,750 for ``un/qwen3.8-27b-gguf``) and may no longer exist
    on the gateway; a fallback pointing at it converts a missing env var into
    an outage. Keep it out of every fallback tier."""
    from nanobot.config.schema import SubagentToolConfig

    retired = "un/qwen3.6-27b-mtp"
    offenders = {role: m for role, m in _nonempty_defaults().items() if retired in m}
    assert not offenders, offenders
    assert retired not in SubagentToolConfig().model
