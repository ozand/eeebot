"""#1471 regression coverage for uniform-trim observability."""
from __future__ import annotations

import asyncio

from nanobot.agent.context import ContextBuilder
from nanobot.runtime import bridge
from tests.test_bridge_system_prompt_overflow import _FittingManager, _wire
from tests.test_cycle_ledger import _read_ledger, _seed_bridge_request


def test_uniform_trim_records_actual_per_section_losses(monkeypatch):
    builder = ContextBuilder.__new__(ContextBuilder)
    sections = [
        ("identity", "i" * 1_446),
        ("bootstrap", "b" * 9_347),
        ("skills_catalogue", "s" * 9_200),
        ("memory", "m" * 4_000),
    ]
    monkeypatch.setenv(ContextBuilder.SYSTEM_PROMPT_CAP_ENV, "24000")

    prompt = builder._fit_system_prompt(sections, strict=True, degrade_on_overflow=True)

    fit = builder.last_fit
    assert fit["rung"] == "uniform_trim"
    assert fit["trimmed"] == [
        {"section": "bootstrap", "chars": 14, "how": "uniform-trim"},
    ]
    assert len(prompt) == 24_000


class _FittingManagerWithTrim(_FittingManager):
    def _build_subagent_prompt(self) -> str:
        prompt = super()._build_subagent_prompt()
        self.last_prompt_fit.update(
            rung="uniform_trim",
            trimmed=[{"section": "bootstrap", "chars": 14, "how": "uniform-trim"}],
        )
        return prompt


def test_bridge_journals_uniform_trim_breakdown(tmp_path, monkeypatch):
    state_dir = _wire(tmp_path, monkeypatch, _FittingManagerWithTrim)
    _seed_bridge_request(state_dir, "req-trim", "cycle-trim", task_title="Fit prompt")

    assert asyncio.run(bridge._main_impl()) == 0

    fit_rows = [row for row in _read_ledger(state_dir) if row["phase"] == "system_prompt"]
    assert fit_rows[-1]["rung"] == "uniform_trim"
    assert fit_rows[-1]["trimmed"] == [
        {"section": "bootstrap", "chars": 14, "how": "uniform-trim"},
    ]
