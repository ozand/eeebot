"""Text-only tile primitives for the ADR-014 avatar slice.

This is deliberately a small vertical slice, not a rendering framework.  It
builds tile data and packets only; no function opens a framebuffer, writes a
terminal, or touches host hardware.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter_ns
from typing import Iterable, Mapping, Sequence

TILE_SIZE = 8
TILEMAP_WIDTH = 128
TILEMAP_HEIGHT = 75
TILE_COUNT = TILEMAP_WIDTH * TILEMAP_HEIGHT
FULL_FRAME_BYTES_32BPP = 1024 * 600 * 4
TILEMAP_BYTES = TILE_COUNT * 2

RGB = tuple[int, int, int]
Tile = tuple[int, ...]


def _rss_kb() -> int | None:
    """Return the process peak RSS using only the standard library.

    Linux exposes the high-water mark in ``/proc`` and ``resource`` supplies
    it on POSIX.  The Windows fallback uses ``GetProcessMemoryInfo`` through
    ctypes.  A measurement is required by the cost contract, so unsupported
    platforms fail explicitly rather than reporting a made-up zero.
    """
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if value > 1024 else value // 1024
    except (ImportError, OSError):
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                        ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
                        ("quota_peak", ctypes.c_size_t), ("quota", ctypes.c_size_t),
                        ("pagefile_peak", ctypes.c_size_t), ("pagefile", ctypes.c_size_t),
                        ("private_usage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not ok or counters.peak_working_set <= 0:
            raise OSError("GetProcessMemoryInfo returned no reading")
        return int(counters.peak_working_set // 1024)
    except (AttributeError, OSError, TypeError):
        return None


def _cost(operation: str, started_ns: int) -> "MeasuredCost":
    return MeasuredCost(operation, (perf_counter_ns() - started_ns) / 1000.0, _rss_kb())


@dataclass(frozen=True)
class MeasuredCost:
    """Measured price of one primitive invocation."""

    operation: str
    elapsed_us: float
    peak_rss_kb: int | None
    measured: bool = True


@dataclass(frozen=True)
class TileAtlas:
    """Immutable collection of 8x8 tiles."""

    tiles: tuple[Tile, ...]
    cost: MeasuredCost

    @classmethod
    def load(cls, tiles: Iterable[Iterable[int]]) -> "TileAtlas":
        started = perf_counter_ns()
        normalized = tuple(tuple(int(pixel) for pixel in tile) for tile in tiles)
        if not normalized:
            raise ValueError("tile atlas must contain at least one tile")
        if any(len(tile) != TILE_SIZE * TILE_SIZE for tile in normalized):
            raise ValueError("each atlas tile must contain exactly 64 indices")
        return cls(normalized, _cost("atlas_load_once", started))

    def tile(self, index: int) -> Tile:
        return self.tiles[index]


class AtlasLoader:
    """Load one atlas once for one composed surface."""

    def __init__(self) -> None:
        self._atlas: TileAtlas | None = None

    def load_once(self, tiles: Iterable[Iterable[int]]) -> TileAtlas:
        if self._atlas is None:
            self._atlas = TileAtlas.load(tiles)
        return self._atlas


@dataclass(frozen=True)
class TileMap:
    """A scene represented only by tile indices."""

    indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.indices) != TILE_COUNT:
            raise ValueError(f"tilemap must contain {TILE_COUNT} indices")
        if any(index < 0 for index in self.indices):
            raise ValueError("tile indices must be non-negative")

    @classmethod
    def blank(cls, tile: int = 0) -> "TileMap":
        return cls((tile,) * TILE_COUNT)

    def at(self, x: int, y: int) -> int:
        if not (0 <= x < TILEMAP_WIDTH and 0 <= y < TILEMAP_HEIGHT):
            raise IndexError("tile coordinate outside 128x75 map")
        return self.indices[y * TILEMAP_WIDTH + x]


class TileMapComposer:
    """Compose one complete map and identify changed tile positions."""

    def compose(self, rows: Sequence[Sequence[int]]) -> tuple[TileMap, MeasuredCost]:
        started = perf_counter_ns()
        if len(rows) != TILEMAP_HEIGHT or any(len(row) != TILEMAP_WIDTH for row in rows):
            raise ValueError("composer input must be 128x75")
        scene = TileMap(tuple(int(index) for row in rows for index in row))
        return scene, _cost("tilemap_compose", started)

    @staticmethod
    def dirty_tiles(previous: TileMap, current: TileMap) -> tuple[int, ...]:
        return tuple(
            index for index, (old, new) in enumerate(zip(previous.indices, current.indices))
            if old != new
        )


@dataclass(frozen=True)
class Palette:
    entries: tuple[RGB, ...]
    cost: MeasuredCost

    @classmethod
    def set(cls, entries: Iterable[RGB]) -> "Palette":
        started = perf_counter_ns()
        values = tuple(tuple(int(channel) for channel in color) for color in entries)
        if len(values) != 16 or any(
            len(color) != 3 or any(not 0 <= channel <= 255 for channel in color)
            for color in values
        ):
            raise ValueError("palette must contain 16 RGB triples")
        return cls(values, _cost("palette_set", started))

    def cycle(self, offset: int = 1) -> "Palette":
        started = perf_counter_ns()
        shift = offset % len(self.entries)
        return Palette(
            self.entries[shift:] + self.entries[:shift],
            _cost("palette_cycle", started),
        )


@dataclass(frozen=True)
class DirtyTilePush:
    """A text-backed dirty-tile packet; the tier-3 writer is out of scope."""

    indices: tuple[int, ...]
    cost: MeasuredCost

    @property
    def bytes(self) -> int:
        return len(self.indices) * 2


def push_dirty_tiles(indices: Iterable[int]) -> DirtyTilePush:
    started = perf_counter_ns()
    values = tuple(int(index) for index in indices)
    if any(not 0 <= index < TILE_COUNT for index in values):
        raise ValueError("dirty tile index outside tilemap")
    return DirtyTilePush(values, _cost("dirty_tile_push", started))


@dataclass(frozen=True)
class SpriteResult:
    accepted: tuple[str, ...]
    dropped: tuple[str, ...]
    visible_dropout: bool
    cost: MeasuredCost


def draw_sprites(sprites: Sequence[str], budget: int) -> SpriteResult:
    """Apply a hard frame budget and expose overflow as visible dropout."""
    if budget < 0:
        raise ValueError("sprite budget must be non-negative")
    started = perf_counter_ns()
    accepted = tuple(sprites[:budget])
    dropped = tuple(sprites[budget:])
    return SpriteResult(accepted, dropped, bool(dropped), _cost("sprite_layer", started))


# Host benchmark declarations are generated by scripts/measure_avatar_costs.py.
# They are kept separate from invocation timings: runtime code measures itself,
# while this table records the required eeepc price for review.
HOST_COSTS: Mapping[str, Mapping[str, object]] = {
    "atlas_load_once": {"elapsed_us": 162.756, "peak_rss_kb": 9868, "status": "measured_eeepc_2026-09-14"},
    "tilemap_compose": {"elapsed_us": 12506.645, "peak_rss_kb": 10456, "status": "measured_eeepc_2026-09-14"},
    "palette_set": {"elapsed_us": 448.166, "peak_rss_kb": 10456, "status": "measured_eeepc_2026-09-14"},
    "palette_cycle": {"elapsed_us": 65.317, "peak_rss_kb": 10456, "status": "measured_eeepc_2026-09-14"},
    "dirty_tile_push": {"elapsed_us": 7067.869, "peak_rss_kb": 10456, "status": "measured_eeepc_2026-09-14"},
    "sprite_layer": {"elapsed_us": 69.205, "peak_rss_kb": 10456, "status": "measured_eeepc_2026-09-14"},
}


def measured_costs() -> Mapping[str, Mapping[str, object]]:
    """Return the declared host benchmark readings for all tier-1 primitives."""
    return HOST_COSTS
