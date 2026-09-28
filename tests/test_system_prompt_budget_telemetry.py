"""ozand/eeebot-ops-dashboard#368: every ``phase: system_prompt`` ledger row
carries the budgets the runtime actually resolved for that fit -- release
pool (limit and used), OPERATING.md reserve, AGENTS.md cap, total system
prompt budget, and the compaction window/reserve/threshold/keep as
``context_compaction`` sees them -- numbers only.

The prompt is built by a REAL ``ContextBuilder`` (loop profile, real
release/workspace files on disk) inside ``bridge._main_impl``; only the
subagent manager around it is the usual test fake. Env overrides are
applied before ``context_compaction`` is (re)imported, the way the host's
unit environment reaches it, so the row must carry the overridden values,
not the defaults.
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.context import ContextBuilder, SystemPromptOverflowError
from nanobot.runtime import bridge, context_compaction
from tests.test_bridge_executor_llm_error import _stub_planning_session
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)

ENV_OVERRIDES = {
    "SELFEVO_COMPACT_WINDOW_TOKENS": "131072",
    "SELFEVO_COMPACT_RESERVE_TOKENS": "4096",
    "SELFEVO_COMPACT_THRESHOLD": "0.7",
    "SELFEVO_COMPACT_KEEP_TOKENS": "12345",
    ContextBuilder.SYSTEM_PROMPT_CAP_ENV: "41000",
}
EXPECTED_COMPACTION = {"window_tokens": 131072, "reserve_tokens": 4096, "threshold": 0.7, "keep_tokens": 12345}
BUDGET_KEYS = {
    "system_prompt_budget_chars",
    "release_pool_chars",
    "operating_reserve_chars",
    "agents_md_cap_chars",
    "compaction",
}


@pytest.fixture
def overridden_env():
    """Env set, then ``context_compaction`` re-imported so its import-time
    constants resolve from it -- restored (env and module) afterwards."""
    with pytest.MonkeyPatch.context() as mp:
        for name, value in ENV_OVERRIDES.items():
            mp.setenv(name, value)
        importlib.reload(context_compaction)
        yield
    importlib.reload(context_compaction)


@pytest.fixture(autouse=True)
def _bridge_fixture_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))
    release_root = tmp_path / "_release_root"
    release_root.mkdir(exist_ok=True)
    (release_root / "goals.md").write_text("test charter", encoding="utf-8")
    monkeypatch.setattr(bridge, "RELEASE_ROOT", release_root)


def _write_ontology(tmp_path: Path) -> tuple[Path, Path]:
    release_root = tmp_path / "builder_release"
    workspace = tmp_path / "builder_workspace"
    release_root.mkdir()
    workspace.mkdir()
    for name in ContextBuilder._RELEASE_BLOCK_NAMES:
        (release_root / name).write_text(f"# {name}\n\nrelease text for {name}.\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("# AGENTS.md\n\n## Rules\n\nstanding instance guidance.\n", encoding="utf-8")
    return release_root, workspace


def _real_builder(tmp_path: Path) -> ContextBuilder:
    release_root, workspace = _write_ontology(tmp_path)
    return ContextBuilder(workspace, release_root=release_root, state_dir=bridge.STATE_DIR)


class _RealBuilderManager(_FakeSubagentManager):
    """The executor's system prompt comes from a real loop-profile build."""

    builder_root: Path | None = None
    raise_overflow = False
    last_builder: ContextBuilder | None = None

    def _build_subagent_prompt(self) -> str:
        builder = _real_builder(type(self).builder_root)
        type(self).last_builder = builder
        prompt = builder.build_system_prompt(
            loop_profile=True, degrade_on_overflow=True, iteration=1, max_iterations=20, cycle_id="",
        )
        self.last_prompt_fit = builder.last_fit
        if type(self).raise_overflow:
            # the refused-prompt row path: same real fit, then the strict refusal
            raise SystemPromptOverflowError(
                over_by=1, cap=builder.last_fit["cap"], sections=builder.last_fit["sections"],
                dropped=[], droppable_reserve_chars=0,
            )
        return prompt


def _run_cycle(tmp_path, monkeypatch, *, raise_overflow: bool) -> list[dict[str, Any]]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    _init_selfevo_repo(tmp_path)
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", _RealBuilderManager)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    monkeypatch.setenv("SELFEVO_DUMP_PROMPTS", "0")
    builder_root = tmp_path / "builder"
    builder_root.mkdir()
    monkeypatch.setattr(_RealBuilderManager, "builder_root", builder_root)
    monkeypatch.setattr(_RealBuilderManager, "raise_overflow", raise_overflow)
    _seed_bridge_request(state_dir, "req-368", "cycle-368", task_title="Extend a skill")
    _stub_planning_session(monkeypatch, "Extend a skill")
    asyncio.run(bridge._main_impl())
    rows = [r for r in _read_ledger(state_dir) if r.get("phase") == "system_prompt"]
    assert len(rows) == 1, rows
    return rows


def _assert_numbers_only(value: Any, path: str = "budget") -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            _assert_numbers_only(inner, f"{path}.{key}")
    else:
        assert value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)), (path, value)


def _assert_budget(row: dict[str, Any]) -> None:
    builder = _RealBuilderManager.last_builder
    assert builder is not None
    assert BUDGET_KEYS <= set(row), sorted(BUDGET_KEYS - set(row))
    # env-overridden values, not the defaults (98,304 / 8,192 / 0.8 / 20,000 / 35,000)
    assert row["compaction"] == EXPECTED_COMPACTION
    assert row["system_prompt_budget_chars"] == 41000 == row["cap"]
    # the pool as this very build drew from it
    assert row["release_pool_chars"] == {
        "limit": ContextBuilder._RELEASE_POOL_CHARS,
        "used": builder.last_fit["release_pool"]["used"],
    }
    assert row["release_pool_chars"]["used"] > 0
    assert row["operating_reserve_chars"] == ContextBuilder._RELEASE_BLOCK_FLOORS["OPERATING.md"]
    assert row["agents_md_cap_chars"] == next(
        cap for kind, name, cap, _ in ContextBuilder.BOOTSTRAP_FILES if kind == "workspace"
    )
    for key in BUDGET_KEYS:
        _assert_numbers_only(row[key], key)


def test_healthy_system_prompt_row_carries_resolved_budgets(tmp_path, monkeypatch, overridden_env):
    (row,) = _run_cycle(tmp_path, monkeypatch, raise_overflow=False)
    assert "overflow" not in row
    _assert_budget(row)


def test_overflow_system_prompt_row_carries_resolved_budgets(tmp_path, monkeypatch, overridden_env):
    (row,) = _run_cycle(tmp_path, monkeypatch, raise_overflow=True)
    assert row["overflow"] is True
    _assert_budget(row)


def test_builder_budget_reads_compaction_module_at_fit_time(tmp_path, overridden_env):
    """The builder reads ``context_compaction``'s attributes when it fits,
    not a copy taken at its own import -- so the value ``compact_messages``
    uses is the value published."""
    builder = _real_builder(tmp_path)
    builder.build_system_prompt(loop_profile=True, degrade_on_overflow=True, iteration=1, max_iterations=20, cycle_id="")
    assert builder.last_fit["budget"]["compaction"] == {
        "window_tokens": context_compaction.WINDOW_TOKENS,
        "reserve_tokens": context_compaction.RESERVE_TOKENS,
        "threshold": context_compaction.THRESHOLD,
        "keep_tokens": context_compaction.KEEP_TOKENS,
    } == EXPECTED_COMPACTION
