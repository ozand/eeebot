"""#1802 -- the release ontology draws from one pool, not five per-block caps.

Measured before the change, on the architect's working branch where the
charter's ladder and the operating-rule edits had landed:

    file           used    old cap   spare
    IDENTITY.md    1,228    1,500      272
    SOUL.md        1,578    1,800      222
    goals.md       3,125    3,200       75
    USER.md        2,527    4,000    1,473
    OPERATING.md   4,997    5,000        3
    total         13,455   15,500    2,045

2,045 characters spare across the ontology; OPERATING.md held three of
them while USER.md sat on 1,473 it was not using, and the whole prompt was
at 59% of its 35,000 ceiling. A distribution problem, not a shortage.

The cost was paid in deleted content: history from the charter's Vector 1,
the parenthetical in the handoff rule, the reason ``exec`` output is
capped. None redundant.

And the reason it was invisible: a per-block cap does its damage BEFORE
anything is written. An edit that will not fit is simply not made, so it
never appears in ``truncated``. "Nothing truncated in 7 days" reads a fuse
that is HOLDING -- which is how #1783 came to be closed by compression.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder


RELEASE_FILES = ContextBuilder._RELEASE_BLOCK_NAMES


def _write_release(root: Path, sizes: dict[str, int]) -> Path:
    release = root / "release"
    release.mkdir(parents=True, exist_ok=True)
    for name in RELEASE_FILES:
        (release / name).write_text("x" * sizes.get(name, 10), encoding="utf-8")
    return release


def _blocks(tmp_path: Path, sizes: dict[str, int]):
    release = _write_release(tmp_path, sizes)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    builder = ContextBuilder(workspace, release_root=release)
    sections, missing, truncated = builder._load_ontology_blocks()
    return builder, dict(sections), missing, truncated


# ---------------------------------------------------------------------------
# the pool itself
# ---------------------------------------------------------------------------

def test_the_pool_is_the_sum_of_the_caps_it_replaces():
    """Derived, not chosen: this change reallocates, it does not widen.
    1500 + 1800 + 3200 + 4000 + 5000."""
    assert ContextBuilder._RELEASE_POOL_CHARS == 15_500


def test_no_per_block_cap_survives_for_release_files():
    """Every release entry carries the pool, not an individual allowance."""
    release = [b for b in ContextBuilder.BOOTSTRAP_FILES if b[0] == "release"]
    assert [b[1] for b in release] == list(RELEASE_FILES)
    assert {b[2] for b in release} == {ContextBuilder._RELEASE_POOL_CHARS}


def test_the_workspace_cap_is_kept_and_is_load_bearing():
    """Not an oversight that this one stays: it bounds a file the LOOP can
    commit to, which is a different question from budgeting operator text."""
    workspace = [b for b in ContextBuilder.BOOTSTRAP_FILES if b[0] == "workspace"]
    assert workspace
    assert all(b[2] == 4000 for b in workspace)


# ---------------------------------------------------------------------------
# the behaviour the issue asks for
# ---------------------------------------------------------------------------

def test_a_block_may_exceed_its_former_individual_cap(tmp_path: Path):
    """The AC: OPERATING.md past 5,000 while the pool has room. Under the
    old caps this content was unreachable at any prompt size."""
    sizes = {name: 10 for name in RELEASE_FILES}
    sizes["OPERATING.md"] = 7_000  # 2,000 over its former cap
    _, sections, missing, truncated = _blocks(tmp_path, sizes)

    assert not truncated, truncated
    # AGENTS.md is legitimately absent from these bare workspaces; only the
    # release files are this test's subject.
    assert not [m for m in missing if m in RELEASE_FILES], missing
    # The loader prepends a "## <name>" heading, so the block is the
    # content plus that -- what matters is that all 7,000 chars survived.
    assert len(sections["operating"]) >= 7_000


def test_one_file_may_take_what_another_is_not_using(tmp_path: Path):
    """The measured case, in miniature: USER.md small, OPERATING.md large,
    total inside the pool. The old caps refused this; the pool does not."""
    sizes = {name: 10 for name in RELEASE_FILES}
    sizes["USER.md"] = 100          # far under its old 4,000
    sizes["OPERATING.md"] = 9_000   # far over its old 5,000
    _, sections, _, truncated = _blocks(tmp_path, sizes)

    assert not truncated
    assert len(sections["operating"]) >= 9_000


def test_the_pool_still_bounds_the_total(tmp_path: Path):
    """The pool is what is bounded. A file demanding more than the whole
    pool is truncated -- and SAYS so, which the per-block caps never did
    for the edit that was never written."""
    sizes = {name: 10 for name in RELEASE_FILES}
    sizes["IDENTITY.md"] = ContextBuilder._RELEASE_POOL_CHARS + 5_000
    _, sections, _, truncated = _blocks(tmp_path, sizes)

    assert "IDENTITY.md" in truncated
    assert len(sections["identity"]) <= ContextBuilder._RELEASE_POOL_CHARS


def test_an_early_runaway_starves_later_blocks_and_that_is_reported(tmp_path: Path):
    """The one risk a per-block ceiling covered. Blocks draw in assembly
    order, so a bloated IDENTITY.md leaves less for OPERATING.md. The
    answer is the alarm below, not pre-emptive rationing: the condition is
    reported rather than prevented by starving everyone equally."""
    sizes = {name: 10 for name in RELEASE_FILES}
    sizes["IDENTITY.md"] = ContextBuilder._RELEASE_POOL_CHARS - 100
    sizes["OPERATING.md"] = 5_000
    _, sections, _, truncated = _blocks(tmp_path, sizes)

    assert "OPERATING.md" in truncated
    assert len(sections["operating"]) <= 100


# ---------------------------------------------------------------------------
# occupancy is published, and a cut is announced
# ---------------------------------------------------------------------------

def test_pool_occupancy_is_recorded_per_file(tmp_path: Path):
    """AC 5: a file approaching the pool must be visible BEFORE it
    truncates. The five caps it replaces were silent by construction."""
    sizes = {name: 100 for name in RELEASE_FILES}
    builder, _, _, _ = _blocks(tmp_path, sizes)

    assert builder._release_pool_usage
    assert set(builder._release_pool_usage) == set(RELEASE_FILES)
    used = sum(builder._release_pool_usage.values())
    assert used + builder._release_pool_left == ContextBuilder._RELEASE_POOL_CHARS


def test_a_truncated_block_raises_an_alarm(tmp_path: Path, caplog):
    """AC 4: ``truncated`` non-empty is a condition someone is TOLD about.
    The first block to truncate will be the one carrying the cycle rules."""
    import logging

    from loguru import logger as _loguru

    records: list[str] = []
    sink_id = _loguru.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        sizes = {name: 10 for name in RELEASE_FILES}
        sizes["IDENTITY.md"] = ContextBuilder._RELEASE_POOL_CHARS + 5_000
        release = _write_release(tmp_path, sizes)
        workspace = tmp_path / "ws2"
        workspace.mkdir(exist_ok=True)
        builder = ContextBuilder(workspace, release_root=release)
        builder.build_system_prompt(loop_profile=True)
    finally:
        _loguru.remove(sink_id)

    assert any("system prompt blocks cut" in r for r in records), records
    assert any("IDENTITY.md" in r for r in records), records


def test_no_alarm_when_nothing_was_cut(tmp_path: Path):
    """An alarm that fires on the healthy path is one nobody reads."""
    from loguru import logger as _loguru

    records: list[str] = []
    sink_id = _loguru.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        release = _write_release(tmp_path, {name: 50 for name in RELEASE_FILES})
        workspace = tmp_path / "ws3"
        workspace.mkdir(exist_ok=True)
        ContextBuilder(workspace, release_root=release).build_system_prompt(loop_profile=True)
    finally:
        _loguru.remove(sink_id)

    assert not [r for r in records if "system prompt blocks cut" in r], records
