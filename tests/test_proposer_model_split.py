"""Tests for #897: proposer model split via ``SELFEVO_PROPOSER_MODEL``.

Covers ``llm_proposer._model_name()`` precedence: the proposer's reasoning
step may run on a different (stronger) model than the executor code-writer,
which keeps reading ``SUBAGENT_BRIDGE_MODEL`` directly and is unaffected.
"""
from __future__ import annotations

from nanobot.runtime import llm_proposer

PROPOSER_ENV = "SELFEVO_PROPOSER_MODEL"
BRIDGE_ENV = "SUBAGENT_BRIDGE_MODEL"


def _clear(monkeypatch) -> None:
    monkeypatch.delenv(PROPOSER_ENV, raising=False)
    monkeypatch.delenv(BRIDGE_ENV, raising=False)


def test_proposer_model_wins_over_bridge_model(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(PROPOSER_ENV, "cl/gemini-3.7-flash-high")
    monkeypatch.setenv(BRIDGE_ENV, "cl/gemini-3.7-flash-mid")
    assert llm_proposer._model_name() == "cl/gemini-3.7-flash-high"


def test_falls_back_to_bridge_model_when_proposer_unset(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(BRIDGE_ENV, "cl/gemini-3.7-flash-mid")
    assert llm_proposer._model_name() == "cl/gemini-3.7-flash-mid"


def test_falls_back_to_bridge_model_when_proposer_empty(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(PROPOSER_ENV, "   ")
    monkeypatch.setenv(BRIDGE_ENV, "cl/gemini-3.7-flash-mid")
    assert llm_proposer._model_name() == "cl/gemini-3.7-flash-mid"


def test_default_when_both_unset(monkeypatch):
    _clear(monkeypatch)
    assert llm_proposer._model_name() == "cl/gemini-3.5-flash-low"


def test_openai_prefix_stripped_from_proposer_model(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(PROPOSER_ENV, "openai/an/gemini-3.7-flash-high")
    monkeypatch.setenv(BRIDGE_ENV, "openai/an/gemini-3.7-flash-mid")
    assert llm_proposer._model_name() == "an/gemini-3.7-flash-high"


def test_openai_prefix_stripped_from_bridge_model_fallback(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(BRIDGE_ENV, "openai/an/gemini-3.7-flash-mid")
    assert llm_proposer._model_name() == "an/gemini-3.7-flash-mid"
