from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime.avatar.state import POSE_SIGNAL, UNKNOWN_POSE, ObservedState, pose_for, select_palette
from nanobot.runtime.avatar.tiles import (
    Palette,
    TileAtlas,
    TileMap,
    TileMapComposer,
    draw_sprites,
    push_dirty_tiles,
)

FIXTURE = Path(__file__).parent / "fixtures/avatar/recorded_host_state.json"


def _state(item):
    return ObservedState(item.get("cycle_status"), item.get("thermal_status"), frozenset(item.get("signals", [])))


def test_atlas_is_8x8_and_loaded_once():
    atlas = TileAtlas.load([[0] * 64, [1] * 64])
    assert len(atlas.tiles) == 2 and len(atlas.tile(0)) == 64
    assert atlas.cost.operation == "atlas_load_once"


def test_tilemap_composer_and_dirty_push():
    rows = [[0] * 128 for _ in range(75)]
    scene, cost = TileMapComposer().compose(rows)
    changed = TileMap(tuple(1 if i == 0 else 0 for i in range(9600)))
    dirty = push_dirty_tiles(TileMapComposer.dirty_tiles(scene, changed))
    assert scene.at(0, 0) == 0 and dirty.indices == (0,) and dirty.bytes == 2
    assert cost.measured


def test_palette_has_16_rgb_entries_and_cycles():
    palette = Palette.set([(i, i, i) for i in range(16)])
    assert palette.cycle().entries[0] == (1, 1, 1)


def test_sprite_budget_overflow_is_visible_and_deterministic():
    result = draw_sprites(["cat", "status", "cost"], budget=2)
    assert result.accepted == ("cat", "status")
    assert result.dropped == ("cost",) and result.visible_dropout
    assert draw_sprites(["cat", "status", "cost"], 2).dropped == result.dropped


def test_pose_table_names_a_signal_and_has_unknown():
    assert UNKNOWN_POSE in POSE_SIGNAL
    assert set(POSE_SIGNAL) == {"idle", "working", "throttled", "dead", UNKNOWN_POSE}
    assert all(signal for signal in POSE_SIGNAL.values())


def test_recorded_host_sequence_covers_throttle_and_dead_cycle():
    events = json.loads(FIXTURE.read_text())["events"]
    assert any(e["thermal_status"] == "throttled" for e in events)
    assert any(e["cycle_status"] == "dead" for e in events)
    poses = [pose_for(_state(event)) for event in events]
    assert "throttled" in poses and "dead" in poses and UNKNOWN_POSE in poses


def test_cover_rule_avatar_never_expresses_unobserved_distinction():
    events = json.loads(FIXTURE.read_text())["events"]
    for event in events:
        state = _state(event)
        pose = pose_for(state)
        if not state.signals or "cycle_status" not in state.signals:
            assert pose == UNKNOWN_POSE
        if pose == "throttled":
            assert "thermal_status" in state.signals


def test_palette_selection_uses_time_and_observed_health():
    entries = [(0, 0, 0)] * 16
    palettes = {
        f"{period}_{health}": Palette.set(entries)
        for period in ("day", "night")
        for health in ("healthy", "degraded", "dead", "unknown")
    }
    assert select_palette(hour=12, cycle_health="healthy", palettes=palettes) is palettes["day_healthy"]
    assert select_palette(hour=23, cycle_health="probe_unavailable", palettes=palettes) is palettes["night_unknown"]


def test_tier_one_costs_are_explicitly_not_invented():
    from nanobot.runtime.avatar.tiles import measured_costs

    costs = measured_costs()
    assert set(costs) == {
        "atlas_load_once", "tilemap_compose", "palette_set", "palette_cycle",
        "dirty_tile_push", "sprite_layer",
    }
    assert all(item["status"].startswith("measured_eeepc_") for item in costs.values())
    assert all(item["elapsed_us"] is not None and item["peak_rss_kb"] is not None for item in costs.values())
