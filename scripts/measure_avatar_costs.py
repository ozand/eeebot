"""Measure tier-1 avatar primitives on the current host.

Usage: python scripts/measure_avatar_costs.py
The output is a bounded JSON document suitable for copying into a reviewed
benchmark record.  It never opens or writes a framebuffer.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nanobot.runtime.avatar.tiles import Palette, TileAtlas, TileMapComposer, draw_sprites, push_dirty_tiles


def peak_rss_kb() -> int | None:
    from nanobot.runtime.avatar.tiles import _rss_kb
    return _rss_kb()


def measure(operation, repeats: int = 100) -> dict[str, object]:
    operation()
    before = peak_rss_kb()
    start = time.perf_counter_ns()
    for _ in range(repeats):
        operation()
    elapsed_us = (time.perf_counter_ns() - start) / 1000 / repeats
    peak = peak_rss_kb()
    rss_values = [value for value in (before, peak) if value is not None]
    return {
        "elapsed_us": round(elapsed_us, 3),
        "peak_rss_kb": max(rss_values) if rss_values else None,
        "measurement_status": "measured" if rss_values else "rss_unavailable",
        "repeats": repeats,
    }


def main() -> int:
    tile = [0] * 64
    rows = [[0] * 128 for _ in range(75)]
    palette = [(i, i, i) for i in range(16)]
    current, _ = TileMapComposer().compose(rows)
    changed = current.__class__(tuple(1 if i == 0 else 0 for i in range(9600)))
    palette_obj = Palette.set(palette)
    measurements = {
        "atlas_load_once": measure(lambda: TileAtlas.load([tile]), 20),
        "tilemap_compose": measure(lambda: TileMapComposer().compose(rows), 100),
        "palette_set": measure(lambda: Palette.set(palette), 100),
        "palette_cycle": measure(palette_obj.cycle, 100),
        "dirty_tile_push": measure(lambda: push_dirty_tiles(TileMapComposer.dirty_tiles(current, changed)), 100),
        "sprite_layer": measure(lambda: draw_sprites(["cat", "status", "cost"], 2), 100),
    }
    output = {
        "schema_version": "avatar-tier1-cost-v1",
        "host": {"platform": sys.platform, "python": sys.version.split()[0]},
        "surface": {"width": 1024, "height": 600, "bits_per_pixel": 32, "full_frame_bytes": 2457600, "tilemap_bytes": 19200, "advantage_ratio": 128.0},
        "measurements": measurements,
        "measurement_note": "Linux host values use resource.RUSAGE_SELF.ru_maxrss (KiB); no framebuffer access performed.",
        "framebuffer_write": False,
    }
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
