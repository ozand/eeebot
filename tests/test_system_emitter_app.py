"""Regression tests for the minimal system health/emitter entrypoint."""

from __future__ import annotations

from pathlib import Path


def test_system_emitter_entrypoint_imports_nanobot_runtime_directly():
    source = Path('app/main.py').read_text(encoding='utf-8')

    assert 'from nanobot.runtime.coordinator import run_self_evolving_cycle' in source
    assert 'from eeebot.runtime.coordinator import run_self_evolving_cycle' not in source
