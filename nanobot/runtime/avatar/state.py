"""Avatar state contract and published state-file writer.

Per ADR-014 and ADR-018 ("the harness judges, the instance draws"):
- The harness owns the verdict: which signals exist, whether they are observed,
  how they resolve into posture, and that the "unknown" state always exists.
- The seam to the instance is a published state file carrying schema version,
  resolved pose, signal derived from, and four-state status.
- No imports cross the boundary in either direction.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

STATE_FILE_VERSION = 1
DEFAULT_AVATAR_STATE_FILENAME = "avatar_state.json"

UNKNOWN_POSE = "unknown"
POSE_SIGNAL: Mapping[str, str] = {
    "idle": "cycle_status",
    "working": "cycle_status",
    "throttled": "thermal_status",
    "dead": "cycle_status",
    UNKNOWN_POSE: "observed_signal_availability",
}


@dataclass(frozen=True)
class ObservedState:
    cycle_status: str | None
    thermal_status: str | None
    signals: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ResolvedAvatarState:
    version: int
    pose: str
    signal: str
    status: str
    timestamp_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def status_for(pose: str) -> str:
    """Map pose to 4-state status: healthy, degraded, dead, unknown."""
    if pose == UNKNOWN_POSE:
        return "unknown"
    if pose == "throttled":
        return "degraded"
    if pose == "dead":
        return "dead"
    return "healthy"


def resolve_avatar_state(
    state: ObservedState,
    *,
    version: int = STATE_FILE_VERSION,
    timestamp_utc: str | None = None,
) -> ResolvedAvatarState:
    pose = pose_for(state)
    signal = POSE_SIGNAL.get(pose, "observed_signal_availability")
    status = status_for(pose)
    ts = timestamp_utc or datetime.now(timezone.utc).isoformat()
    return ResolvedAvatarState(
        version=version,
        pose=pose,
        signal=signal,
        status=status,
        timestamp_utc=ts,
    )


def _validate_published_state(resolved: ResolvedAvatarState) -> None:
    if resolved.version != STATE_FILE_VERSION:
        raise ValueError(f"unsupported avatar state schema version: {resolved.version!r}")
    expected_signal = POSE_SIGNAL.get(resolved.pose)
    if expected_signal is None or resolved.signal != expected_signal:
        raise ValueError(f"pose contract signal mismatch for {resolved.pose!r}")
    if status_for(resolved.pose) != resolved.status:
        raise ValueError(f"pose contract status mismatch for {resolved.pose!r}")


def write_avatar_state_file(
    resolved: ResolvedAvatarState,
    path: Path | str,
) -> Path:
    """Atomically write a schema- and pose-contract-validated state file."""
    _validate_published_state(resolved)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(resolved.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp = dest.with_suffix(f".tmp.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(dest)
    return dest
