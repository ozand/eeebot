"""#1784 -- the executor's ``system_prompt_chars`` was null on every row.

Measured on host eeepc: **797 of 797 executor rows in ``state/llm_calls``
carried ``system_prompt_chars: null``**, while the proposer, reflector,
curator and strategist all carried real numbers. Those four pass the field
explicitly at their own call sites; the executor reaches telemetry only
through ``BaseProvider._record``, which never passed it.

The sting is that the executor is the one role whose prompt budget was just
raised from 24,000 to 35,000 (#1753), so the role the change was made for is
the role not measured. The real figure does exist in the ``phase:
"system_prompt"`` ledger row (21,477 at the time of measurement), so the
number was reachable in one artifact and null in another for the same cycle.

Nothing reads the field -- no dashboard panel, no scorecard metric, no guard
-- so this is additive and cannot change behaviour.
"""
from __future__ import annotations

from typing import Any

import pytest

from nanobot.observability.llm_telemetry import system_chars


# ---------------------------------------------------------------------------
# the measurement helper
# ---------------------------------------------------------------------------

def test_measures_the_system_message():
    assert system_chars([
        {"role": "system", "content": "abcde"},
        {"role": "user", "content": "much longer user turn"},
    ]) == 5


def test_measures_the_first_system_message_only():
    assert system_chars([
        {"role": "system", "content": "abc"},
        {"role": "user", "content": "x"},
        {"role": "system", "content": "a much longer second system turn"},
    ]) == 3


def test_returns_none_rather_than_zero_when_there_is_nothing_to_measure():
    """None means "not measured"; 0 would mean "an empty system prompt was
    sent". Conflating them is the no-data-as-zero confusion this project
    keeps paying for."""
    assert system_chars([]) is None
    assert system_chars(None) is None
    assert system_chars([{"role": "user", "content": "x"}]) is None


def test_an_empty_system_message_measures_zero_not_none():
    """The other side of the same distinction: a system message that IS
    empty is a real measurement of zero."""
    assert system_chars([{"role": "system", "content": ""}]) == 0


def test_tolerates_malformed_entries():
    assert system_chars([None, "not a dict", {"role": "system", "content": "ok"}]) == 2


def test_role_prompt_re_export_is_the_same_object():
    """#1729's four callers import it from `role_prompt`. The definition
    moved so `providers.base` could use it without a provider importing a
    runtime module -- the dependency inversion that broke
    `test_trainer_no_direct_mutation` in #1743. The name must keep working."""
    from nanobot.runtime import role_prompt

    assert role_prompt.system_chars is system_chars


# ---------------------------------------------------------------------------
# the provider actually passes it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_with_retry_records_the_executors_system_prompt_chars(monkeypatch):
    """The regression this issue is about: the executor's only telemetry site
    omitted the argument, so every executor row was null."""
    from nanobot.providers import base as provider_base

    recorded: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(provider_base, "record_llm_call", _capture)
    monkeypatch.setattr(provider_base, "record_llm_prompt", lambda *a, **k: None)
    monkeypatch.setattr(provider_base, "resolve_context_window", lambda *a, **k: None)

    class _Provider(provider_base.LLMProvider):
        api_base = None
        api_key = None

        def get_default_model(self) -> str:
            return "fake/model"

        async def chat(self, messages, tools=None, model=None, **kwargs):
            return provider_base.LLMResponse(content="ok", finish_reason="stop")

    messages = [
        {"role": "system", "content": "s" * 1234},
        {"role": "user", "content": "do the thing"},
    ]
    await _Provider().chat_with_retry(messages=messages, model="fake/model")

    assert recorded.get("system_prompt_chars") == 1234
