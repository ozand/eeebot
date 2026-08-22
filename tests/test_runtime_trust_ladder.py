"""Tests for #876: the derived runtime trust ladder.

The ladder widens the bounded runtime-slice mutation surface (#812/#823) by
letting the loop EARN access to further ``nanobot/runtime/*.py`` compute
modules purely as a function of which modules already have an ACTIVE
root-verified promotion (#875) — never a new mutable state machine. This
file covers the two pure functions in ``nanobot.runtime.runtime_deny``
(``earned_ladder_slice`` / ``earned_ladder_level``) and the deny-set
invariant on the ladder itself. The filesystem-reading half
(``promoted_overlay.active_promoted_modules`` / ``effective_runtime_slice``)
is covered in ``tests/test_promoted_overlay.py``.
"""
from __future__ import annotations

from nanobot.runtime import runtime_deny

_RUNG0 = runtime_deny.RUNTIME_TRUST_LADDER[0]
_RUNG1 = runtime_deny.RUNTIME_TRUST_LADDER[1]
_RUNG2 = runtime_deny.RUNTIME_TRUST_LADDER[2]


def test_ladder_is_the_expected_three_modules_in_ascending_blast_radius_order():
    # #924: the former rung 3 (cycle_planning.py) was dropped — that module
    # was deleted in #916/#923 and the entry was inert (allow-listing a
    # nonexistent file).
    assert runtime_deny.RUNTIME_TRUST_LADDER == (
        "nanobot/runtime/existence_index.py",
        "nanobot/runtime/demand.py",
        "nanobot/runtime/llm_proposer.py",
    )


def test_no_ladder_module_is_ever_in_the_deny_set():
    for module_path in runtime_deny.RUNTIME_TRUST_LADDER:
        assert not runtime_deny._is_runtime_deny(module_path), module_path


# ─── earned_ladder_slice ──────────────────────────────────────────────────────
# rung 0 (existence_index.py) is NEVER part of earned_ladder_slice's output —
# it is the operator-seeded base rung, reachable only via the env allow-list
# (runtime_slice_paths). The ladder only ever ADDS rungs 1-2 on top of that.


def test_earned_slice_zero_active_is_empty():
    # No promotions at all -> the ladder contributes nothing. This is the
    # byte-identical-at-zero-promotions invariant: effective_runtime_slice
    # must equal the env-only slice exactly, even when the env is unset.
    assert runtime_deny.earned_ladder_slice(set()) == set()


def test_earned_slice_rung0_active_unlocks_rung1():
    assert runtime_deny.earned_ladder_slice({_RUNG0}) == {_RUNG1}


def test_earned_slice_rung0_and_rung1_active_unlocks_rung2():
    assert runtime_deny.earned_ladder_slice({_RUNG0, _RUNG1}) == {_RUNG1, _RUNG2}


def test_earned_slice_non_consecutive_active_does_not_skip():
    # rung1 active but rung0 NOT — the walk starts at rung0, finds it
    # missing, and stops immediately. rung1 being active does not unlock
    # rung2, and contributes nothing itself (rung0 is never granted by the
    # ladder — only the env allow-list can grant it).
    assert runtime_deny.earned_ladder_slice({_RUNG1}) == set()


def test_earned_slice_all_active_is_the_ladder_minus_rung0():
    all_active = set(runtime_deny.RUNTIME_TRUST_LADDER)
    assert runtime_deny.earned_ladder_slice(all_active) == {_RUNG1, _RUNG2}


def test_earned_slice_top_rung_active_alone_unlocks_nothing():
    # rung2 (the top) active alone, with nothing below it active, unlocks
    # nothing at all — consecutive-from-bottom only.
    assert runtime_deny.earned_ladder_slice({_RUNG2}) == set()


def test_earned_slice_fails_open_to_empty_on_bad_input():
    assert runtime_deny.earned_ladder_slice(None) == set()  # type: ignore[arg-type]


# ─── earned_ladder_level ──────────────────────────────────────────────────────


def test_earned_level_zero_active_is_zero():
    assert runtime_deny.earned_ladder_level(set()) == 0


def test_earned_level_rung0_active_is_one():
    assert runtime_deny.earned_ladder_level({_RUNG0}) == 1


def test_earned_level_rung0_and_rung1_active_is_two():
    assert runtime_deny.earned_ladder_level({_RUNG0, _RUNG1}) == 2


def test_earned_level_non_consecutive_active_is_zero():
    assert runtime_deny.earned_ladder_level({_RUNG1}) == 0


def test_earned_level_all_active_is_full_length():
    all_active = set(runtime_deny.RUNTIME_TRUST_LADDER)
    assert runtime_deny.earned_ladder_level(all_active) == len(runtime_deny.RUNTIME_TRUST_LADDER)


def test_earned_level_fails_open_to_zero_on_bad_input():
    assert runtime_deny.earned_ladder_level(None) == 0  # type: ignore[arg-type]
