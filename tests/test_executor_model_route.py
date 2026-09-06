"""Tests for #1387 part 2: the executor's escalation-model override keeps the
configured gateway route.

Diagnosis (mechanism pins in ``TestMechanismPins`` below): the executor talks
to the LiteLLM gateway through ``openai/<gateway-model>`` — the ``openai``
provider spec has an empty ``litellm_prefix``, so ``LiteLLMProvider._resolve_model``
leaves it untouched and it goes out as an OpenAI-shaped ``/chat/completions``
call. An escalation marker names a bare gateway model
(``SELFEVO_ESCALATION_MODEL``, e.g. ``an/gemini-3.7-flash-high``) with no
route. Substituted wholesale for the executor's model string, ``find_by_model``
keyword-matches "gemini" in it, routes it through the ``gemini`` provider spec
(``litellm_prefix="gemini"``), and litellm sends a Google-shaped request to
the same OpenAI-compatible gateway — which answers ``404 {"detail":"Not
Found"}``. ``nanobot.runtime.model_registry.route_like`` fixes this by
re-applying the base model's route prefix to the escalated candidate before
it is ever handed to litellm; ``nanobot.runtime.bridge._executor_model_for_request``
is the call site that applies it for a demand's escalation marker.

Contract extension: ``route_like`` decides by the provider registry, not by
the mere presence of a "/" — it prepends the base's route iff the base's head
token IS a provider name/``litellm_prefix`` from ``nanobot.providers.registry
.PROVIDERS`` (``openai``, ``gemini``, ``openrouter``, …) AND the candidate's
head token is NOT one. Gateway namespaces like ``an/``, ``un/``, ``cl/`` are
NOT provider tokens, so a base on one of those copies nothing. The producer,
``nanobot.runtime.demand.escalation_model()``, now applies ``route_like``
itself so the escalation marker, the ``proposed`` row, and what the executor
actually sends are one string.

``_executor_model_for_request`` does not exist on ``main`` — every test in
``TestExecutorModelForRequest`` is a regression pin that fails with
``AttributeError: module 'nanobot.runtime.bridge' has no attribute
'_executor_model_for_request'`` there.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.runtime import bridge, demand
from nanobot.runtime.model_registry import route_like


# ─── route_like ────────────────────────────────────────────────────────────


def test_route_like_prepends_base_route_to_routeless_candidate():
    assert (
        route_like("openai/un/qwen3.8-27b-gguf", "an/gemini-3.7-flash-high")
        == "openai/an/gemini-3.7-flash-high"
    )


def test_route_like_candidate_already_on_route_unchanged():
    assert route_like("openai/un/qwen3.8-27b-gguf", "openai/an/gemini-3.7-flash-high") == (
        "openai/an/gemini-3.7-flash-high"
    )


def test_route_like_routeless_base_returns_candidate_unchanged():
    assert route_like("un-qwen-no-slash", "an/gemini-3.7-flash-high") == "an/gemini-3.7-flash-high"


def test_route_like_empty_candidate_returns_empty_string():
    assert route_like("openai/un/qwen3.8-27b-gguf", "") == ""


def test_route_like_whitespace_is_stripped_on_both_sides():
    assert route_like("openai/x", "  an/g ") == "openai/an/g"


def test_route_like_base_without_slash_returns_candidate_unchanged():
    assert route_like("nowslashbase", "an/gemini-3.7-flash-high") == "an/gemini-3.7-flash-high"


def test_route_like_different_explicit_route_on_candidate_untouched():
    """Candidate already carries a DIFFERENT registered route (gemini) — the
    registry, not "/", decides this stays put."""
    assert route_like("openai/x", "gemini/gemini-2.5-pro") == "gemini/gemini-2.5-pro"


def test_route_like_candidate_on_openrouter_route_untouched():
    assert route_like("openai/x", "openrouter/anthropic/claude") == "openrouter/anthropic/claude"


def test_route_like_gateway_namespace_base_copies_nothing():
    """``an/`` is a gateway namespace, not a provider token — a base that
    merely has a slash is not enough to make it a route."""
    assert route_like("an/gemini-3.8-flash-high", "un/qwen3.8-27b-gguf") == "un/qwen3.8-27b-gguf"


def test_route_like_base_head_not_a_registry_token_returns_candidate():
    assert route_like("qwen3.8", "an/g") == "an/g"


def test_route_like_is_idempotent():
    once = route_like("openai/x", "an/g")
    assert route_like("openai/x", once) == once == "openai/an/g"


def test_route_like_empty_candidate_returns_empty_string_regardless_of_base():
    assert route_like("openai/x", "") == ""


# ─── mechanism pins: this is the diagnosis, kept as tests so the next reader
# does not have to re-derive it from scratch ────────────────────────────────


class TestMechanismPins:
    def test_find_by_model_matches_gemini_by_keyword(self):
        from nanobot.providers.registry import find_by_model

        assert find_by_model("an/gemini-3.7-flash-high").name == "gemini"

    def test_find_by_model_matches_openai_by_explicit_prefix_over_gemini_keyword(self):
        from nanobot.providers.registry import find_by_model

        assert find_by_model("openai/an/gemini-3.7-flash-high").name == "openai"

    def test_find_by_model_matches_openai_by_explicit_prefix(self):
        from nanobot.providers.registry import find_by_model

        assert find_by_model("openai/un/qwen3.8-27b-gguf").name == "openai"

    def _provider(self):
        from nanobot.providers.litellm_provider import LiteLLMProvider

        return LiteLLMProvider(
            api_key="k",
            api_base="http://gateway.invalid/v1",
            default_model="openai/un/qwen3.8-27b-gguf",
            provider_name="openai",
        )

    def test_bare_escalation_model_resolves_to_gemini_route_404_shape(self):
        provider = self._provider()
        # This is the string that made litellm send a Google-shaped request
        # to the OpenAI-compatible gateway, which answered 404.
        assert provider._resolve_model("an/gemini-3.7-flash-high") == "gemini/an/gemini-3.7-flash-high"

    def test_routed_escalation_model_is_left_on_the_openai_route(self):
        provider = self._provider()
        assert (
            provider._resolve_model("openai/an/gemini-3.7-flash-high")
            == "openai/an/gemini-3.7-flash-high"
        )

    def test_provider_is_not_detected_as_a_gateway(self):
        """A live-like construction with provider_name="openai" must NOT be
        classified as a gateway — the gateway branch of ``_resolve_model``
        can ``strip_model_prefix`` and re-prefix, which would undo the route
        ``route_like`` just applied. Standard-provider mode (``self._gateway
        is None``) is what makes the keyword-match/no-op behavior pinned
        above hold."""
        provider = self._provider()
        assert provider._gateway is None


# ─── producer: nanobot.runtime.demand.escalation_model() ──────────────────
# #1387 contract extension: the operator-configured escalation model is
# routed at the single point the operator string enters the system, so the
# escalation marker, the ``proposed`` row and what the executor sends are
# one string. This complements (does not replace) the consumer-side
# ``_executor_model_for_request`` repair, which still applies for markers
# recorded before this producer-side fix shipped.


class TestEscalationModelProducer:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("SUBAGENT_BRIDGE_MODEL", raising=False)
        monkeypatch.delenv("SELFEVO_ESCALATION_MODEL", raising=False)

    def test_escalation_model_routed_onto_configured_executor_route(self, monkeypatch):
        monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "openai/un/qwen3.8-27b-gguf")
        monkeypatch.setenv("SELFEVO_ESCALATION_MODEL", "an/gemini-3.7-flash-high")

        assert demand.escalation_model() == "openai/an/gemini-3.7-flash-high"

    def test_escalation_model_unset_returns_empty_string(self, monkeypatch):
        monkeypatch.setenv("SUBAGENT_BRIDGE_MODEL", "openai/un/qwen3.8-27b-gguf")

        assert demand.escalation_model() == ""

    def test_escalation_model_with_routeless_executor_default_returns_candidate_unchanged(self, monkeypatch):
        # SUBAGENT_BRIDGE_MODEL unset -> resolve_model("executor") falls back
        # to its built-in default, "an/gemini-3.8-flash-high" -- route-less
        # (the "an/" head is a gateway namespace, not a provider token).
        monkeypatch.setenv("SELFEVO_ESCALATION_MODEL", "an/g")

        assert demand.escalation_model() == "an/g"


# ─── _executor_model_for_request ───────────────────────────────────────────


_DEMAND_ID = "defect-93d5458abb21"
_CYCLE_ID = "cycle-5445c37c9d07"
_MARKER_MODEL = "an/gemini-3.7-flash-high"
_BASE_MODEL = "openai/un/qwen3.8-27b-gguf"


def _req(task: str, cycle_id: str = _CYCLE_ID) -> dict:
    return {"task": task, "cycle_id": cycle_id}


def _serves_task(demand_id: str = _DEMAND_ID) -> str:
    return f"Do X\nServes: demand {demand_id}\n"


class TestExecutorModelForRequest:
    def test_missing_function_would_raise_attributeerror_on_main(self):
        # Documents the regression this file pins: on main, this attribute
        # does not exist at all.
        assert hasattr(bridge, "_executor_model_for_request")

    def test_escalation_override_applies_route_and_logs(self, tmp_path, capsys):
        demand.record_escalation(tmp_path, _DEMAND_ID, _CYCLE_ID, _MARKER_MODEL)

        result = bridge._executor_model_for_request(
            _req(_serves_task()), "unused-request-id", _BASE_MODEL, tmp_path,
        )

        assert result == "openai/an/gemini-3.7-flash-high"
        out = capsys.readouterr().out
        assert f"escalation override for {_DEMAND_ID}" in out
        assert f"marker={_MARKER_MODEL}" in out
        assert f"base={_BASE_MODEL}" in out

    def test_other_cycle_id_returns_base(self, tmp_path):
        demand.record_escalation(tmp_path, _DEMAND_ID, _CYCLE_ID, _MARKER_MODEL)

        result = bridge._executor_model_for_request(
            _req(_serves_task(), cycle_id="cycle-some-other-one"), "unused-request-id", _BASE_MODEL, tmp_path,
        )

        assert result == _BASE_MODEL

    def test_no_serves_demand_marker_in_task_returns_base(self, tmp_path):
        demand.record_escalation(tmp_path, _DEMAND_ID, _CYCLE_ID, _MARKER_MODEL)

        result = bridge._executor_model_for_request(
            _req("Do X with no serves line\n"), "unused-request-id", _BASE_MODEL, tmp_path,
        )

        assert result == _BASE_MODEL

    def test_marker_for_another_demand_returns_base(self, tmp_path):
        demand.record_escalation(tmp_path, "defect-other-demand", _CYCLE_ID, _MARKER_MODEL)

        result = bridge._executor_model_for_request(
            _req(_serves_task(_DEMAND_ID)), "unused-request-id", _BASE_MODEL, tmp_path,
        )

        assert result == _BASE_MODEL

    def test_nonexistent_state_dir_fails_open_to_base_without_raising(self, tmp_path):
        missing = tmp_path / "does-not-exist"

        result = bridge._executor_model_for_request(
            _req(_serves_task()), "unused-request-id", _BASE_MODEL, missing,
        )

        assert result == _BASE_MODEL

    def test_marker_model_already_routed_is_unchanged(self, tmp_path):
        demand.record_escalation(tmp_path, _DEMAND_ID, _CYCLE_ID, "openai/an/x")

        result = bridge._executor_model_for_request(
            _req(_serves_task()), "unused-request-id", _BASE_MODEL, tmp_path,
        )

        assert result == "openai/an/x"

    def test_request_id_used_when_cycle_id_key_absent(self, tmp_path, capsys):
        demand.record_escalation(tmp_path, _DEMAND_ID, _CYCLE_ID, _MARKER_MODEL)
        req = {"task": _serves_task()}  # no "cycle_id" key

        result = bridge._executor_model_for_request(
            req, _CYCLE_ID, _BASE_MODEL, tmp_path,
        )

        assert result == "openai/an/gemini-3.7-flash-high"

    def test_unreadable_escalation_marker_fails_open_and_logs(self, tmp_path, capsys, monkeypatch):
        def _raise(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(demand, "_escalation_marker", _raise)

        result = bridge._executor_model_for_request(
            _req(_serves_task()), "unused-request-id", _BASE_MODEL, tmp_path,
        )

        assert result == _BASE_MODEL
        out = capsys.readouterr().out
        assert "escalation marker unreadable" in out


# ─── end-to-end wiring pin ─────────────────────────────────────────────────


def test_bridge_model_override_goes_through_the_helper():
    """Pins that the executor's live model override at the
    ``config.agents.defaults.model =`` call site routes through
    ``_executor_model_for_request`` rather than substituting the escalation
    marker's model directly (the substitution this file's mechanism pins
    show is unsafe).
    """
    import nanobot.runtime.bridge as bridge_mod

    source = Path(bridge_mod.__file__).read_text(encoding="utf-8")
    assert "bridge_model = _executor_model_for_request(" in source
