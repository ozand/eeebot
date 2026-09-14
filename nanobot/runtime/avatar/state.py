"""Tier-2 avatar state mapping: observed signals only, including unknown."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .tiles import Palette, TileMap

UNKNOWN_POSE = "unknown"
POSE_SIGNAL: Mapping[str, str] = {
    "idle": "cycle_status",
    "working": "cycle_status",
    "throttled": "thermal_status",
    "dead": "cycle_status",
    UNKNOWN_POSE: "observed_signal_availability",
}
POSE_TILES: Mapping[str, int] = {"idle": 1, "working": 2, "throttled": 3, "dead": 4, UNKNOWN_POSE: 5}

@dataclass(frozen=True)
class ObservedState:
    cycle_status: str | None
    thermal_status: str | None
    signals: frozenset[str] = frozenset()


def _observed(value: Any, name: str, signals: frozenset[str]) -> bool:
    return name in signals and value not in (None, "", "missing", "probe_unavailable")


def pose_for(state: ObservedState) -> str:
    """Pure function: absent or unavailable harness signals always mean unknown."""
    if not _observed(state.cycle_status, "cycle_status", state.signals):
        return UNKNOWN_POSE
    if state.cycle_status in {"dead", "stalled", "crash_loop"}:
        return "dead"
    if _observed(state.thermal_status, "thermal_status", state.signals) and state.thermal_status in {"throttled", "critical"}:
        return "throttled"
    if state.cycle_status in {"working", "running"}:
        return "working"
    if state.cycle_status in {"idle", "success"}:
        return "idle"
    return UNKNOWN_POSE


def pose_tile(state: ObservedState) -> int:
    return POSE_TILES[pose_for(state)]


def select_palette(*, hour: int, cycle_health: str | None, palettes: Mapping[str, Palette]) -> Palette:
    """Select by observed time and health; unavailable health chooses degraded."""
    if not 0 <= hour <= 23:
        raise ValueError("hour must be between 0 and 23")
    health = cycle_health if cycle_health in {"healthy", "degraded", "dead", "unknown"} else "unknown"
    period = "night" if hour < 7 or hour >= 19 else "day"
    key = f"{period}_{health}"
    return palettes.get(key) or palettes[f"{period}_unknown"]


def compose_pose_scene(base: TileMap, state: ObservedState) -> TileMap:
    """Place one avatar tile in a copy of the scene; no rendering side effects."""
    values = list(base.indices)
    values[0] = pose_tile(state)
    return TileMap(tuple(values))
