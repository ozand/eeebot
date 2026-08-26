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
from nanobot.runtime.model_registry import resolve_max_tool_iterations, resolve_model

_ALL_MODEL_ENV_VARS = (
    "SELFEVO_PROPOSER_MODEL",
    "SUBAGENT_BRIDGE_MODEL",
    "SELFEVO_SUMMARY_MODEL",
    "LITELLM_MODEL",
    "SELFEVO_CURATOR_MODEL",
    "SELFEVO_REFLECTOR_MODEL",
    "SELFEVO_STRATEGIST_MODEL",
)


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    """No host bleed: clear every model-selection env var before each test."""
    for var in _ALL_MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ─── GOLDEN DEFAULTS ──────────────────────────────────────────────────────────

def test_golden_default_proposer():
    assert resolve_model("proposer") == "cl/gemini-3.5-flash-low"


def test_golden_default_executor():
    assert resolve_model("executor") == "cl/gemini-3.5-flash-low"


def test_golden_default_harness():
    assert resolve_model("harness") == "un/qwen3.6-27b-mtp"


def test_golden_default_summary():
    assert resolve_model("summary") == "cl/gemini-3.5-flash-low"


def test_golden_default_coordinator():
    assert resolve_model("coordinator") == "cl/gemini-3.5-flash-low"


def test_golden_default_curator():
    assert resolve_model("curator") == "cl/gemini-3.5-flash-low"


def test_golden_default_reflector():
    assert resolve_model("reflector") == "cl/gemini-3.5-flash-low"


def test_golden_default_strategist():
    assert resolve_model("strategist") == "cl/gemini-3.5-flash-low"


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
    assert resolve_model("proposer") == "cl/gemini-3.5-flash-low"


# ─── executor precedence ──────────────────────────────────────────────────────

def test_executor_uses_config_fallback_only_when_env_unset():
    assert resolve_model("executor", config_fallback="cfg/model") == "cfg/model"


def test_executor_env_wins_over_config_fallback(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("executor", config_fallback="cfg/model") == "an/bridge-model"


def test_executor_default_when_env_and_config_fallback_unset():
    assert resolve_model("executor") == "cl/gemini-3.5-flash-low"
    assert resolve_model("executor", config_fallback=None) == "cl/gemini-3.5-flash-low"


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


# ─── summary ──────────────────────────────────────────────────────────────────

def test_summary_env_override(monkeypatch):
    monkeypatch.setenv("SELFEVO_SUMMARY_MODEL", "an/summary-model")
    assert resolve_model("summary") == "an/summary-model"


def test_summary_default_when_unset():
    assert resolve_model("summary") == "cl/gemini-3.5-flash-low"


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
    assert resolve_model("proposer") == "cl/gemini-3.5-flash-low"


def test_whitespace_only_explicit_treated_as_unset(monkeypatch):
    monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "an/bridge-model")
    assert resolve_model("executor", explicit="   ") == "an/bridge-model"


def test_whitespace_only_config_fallback_treated_as_unset():
    assert resolve_model("executor", config_fallback="   ") == "cl/gemini-3.5-flash-low"


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
        ("proposer", "cl/gemini-3.5-flash-low"),
        ("executor", "cl/gemini-3.5-flash-low"),
        ("harness", "un/qwen3.6-27b-mtp"),
        ("summary", "cl/gemini-3.5-flash-low"),
        ("coordinator", "cl/gemini-3.5-flash-low"),
        ("curator", "cl/gemini-3.5-flash-low"),
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
