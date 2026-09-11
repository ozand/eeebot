"""Regression coverage for the strict prompt-fit resident memory invariant."""
from __future__ import annotations

import os
import re

from nanobot.agent.context import ContextBuilder


CAP = 24_000
CATALOGUE_SIZE = 9_000
IDENTITY_SIZE = 1_446
BOOTSTRAP_SIZE = 9_347
MEMORY_SIZE = 4_000
RESIDENT_LABELS = (
    "[Identity]",
    "[Write target:",
    "[DO NOT touch]",
    "[Rules]",
    "[Key paths",
)


def _filled(size: int, marker: str) -> str:
    return marker * size


def _memory_block(size: int) -> str:
    header = "\n".join(RESIDENT_LABELS) + "\n"
    assert len(header) <= size
    return header + ("m" * (size - len(header)))


def _fit(catalogue_size: int) -> tuple[str, dict, dict[str, str]]:
    builder = ContextBuilder.__new__(ContextBuilder)
    sections = [
        ("identity", _filled(IDENTITY_SIZE, "i")),
        ("bootstrap", _filled(BOOTSTRAP_SIZE, "b")),
        ("skills_catalogue", _filled(catalogue_size, "s")),
        ("memory", _memory_block(MEMORY_SIZE)),
    ]
    previous = os.environ.get(ContextBuilder.SYSTEM_PROMPT_CAP_ENV)
    os.environ[ContextBuilder.SYSTEM_PROMPT_CAP_ENV] = str(CAP)
    try:
        prompt = builder._fit_system_prompt(
            sections, strict=True, degrade_on_overflow=True
        )
        fit = dict(builder.last_fit)
    finally:
        if previous is None:
            os.environ.pop(ContextBuilder.SYSTEM_PROMPT_CAP_ENV, None)
        else:
            os.environ[ContextBuilder.SYSTEM_PROMPT_CAP_ENV] = previous

    parts = prompt.split(builder.SECTION_SEPARATOR)
    emitted = dict(zip((name for name, content in sections if content), parts))
    return prompt, fit, emitted


def test_resident_memory_labels_survive_catalogue_overflow_ladder():
    """Water-filling must keep the capped resident memory block intact."""
    for delta in (0, 200, 500, 1_000, 2_000, 5_000, 10_000, 20_000, 50_000):
        prompt, fit, emitted = _fit(CATALOGUE_SIZE + delta)
        memory = emitted["memory"]

        assert fit["rung"] in {"full", "uniform_trim"}
        assert all(label in memory for label in RESIDENT_LABELS), (
            delta,
            fit,
            memory[:200],
        )
        assert len(memory) == MEMORY_SIZE
        assert "[trimmed " not in memory
        assert len(prompt) <= CAP


def test_resident_labels_are_intact_not_prefix_fragments():
    """The assertion is about complete labels, not accidental short fragments."""
    _, fit, emitted = _fit(CATALOGUE_SIZE + 2_000)
    memory = emitted["memory"]
    assert fit["rung"] == "uniform_trim"
    for label in RESIDENT_LABELS:
        assert re.search(re.escape(label), memory)
