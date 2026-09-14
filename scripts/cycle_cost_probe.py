"""Bounded, attributable cost probes for one eeebot bridge cycle.

The probe is deliberately separate from host-wide metrics.  It reads only the
calling process' proc entries and the host paths verified for the eeepc, and it
never turns an unreadable sensor into a numeric zero.  A caller may use
``CycleCostSampler`` around an explicitly authorized cycle; the daily hardware
refresh can persist the resulting record without pretending that an idle
refresh measured a cycle.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

CAPABILITY_STATES = frozenset(
    {"present", "absent", "present_uninitialized", "probe_unavailable"}
)
THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0")
THROTTLE_GLOB = "/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count"
THROTTLE_TIME_GLOB = "/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_total_time_ms"
THROTTLE_MAX_GLOB = "/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_max_time_ms"
FRAMEBUFFER_SIZE = Path("/sys/class/graphics/fb0/virtual_size")
FRAMEBUFFER_BPP = Path("/sys/class/graphics/fb0/bits_per_pixel")
FRAMEBUFFER_WIDTH = 1024
FRAMEBUFFER_HEIGHT = 600
FRAMEBUFFER_BITS_PER_PIXEL = 32
FRAMEBUFFER_BYTES = FRAMEBUFFER_WIDTH * FRAMEBUFFER_HEIGHT * FRAMEBUFFER_BITS_PER_PIXEL // 8
PERMANENT_BATTERY_REASON = "no BAT* device; host exposes only AC0 type=Mains"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _result(state: str, value: int | float | None, unit: str, details: str, **extra: Any) -> dict[str, Any]:
    if state not in CAPABILITY_STATES:
        raise ValueError(f"unknown capability state: {state}")
    row: dict[str, Any] = {
        "state": state,
        "value": value,
        "unit": unit,
        "details": details,
        "measured_at": _now(),
    }
    row.update(extra)
    return row


def unavailable(reason: str, unit: str = "") -> dict[str, Any]:
    """Return a truthful unavailable result; ``value`` is never fabricated."""
    return _result("probe_unavailable", None, unit, reason)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _parse_kb(value: str) -> int:
    parts = value.split()
    if len(parts) < 2 or not parts[0].isdigit() or parts[1].lower() != "kb":
        raise ValueError(f"invalid kB value: {value!r}")
    return int(parts[0]) * 1024


def read_process_snapshot(proc_root: Path = Path("/proc/self"), clock_ticks: int | None = None) -> dict[str, int]:
    """Read CPU counters and peak RSS for this process only."""
    stat = _read_text(proc_root / "stat")
    closing = stat.rfind(")")
    if closing < 0:
        raise ValueError("/proc stat has no command terminator")
    fields = stat[closing + 2 :].split()
    if len(fields) < 13:
        raise ValueError("/proc stat is truncated")
    ticks = int(clock_ticks or os.sysconf("SC_CLK_TCK"))
    if ticks <= 0:
        raise ValueError("invalid clock tick rate")
    status_values = {
        line.split(":", 1)[0]: line.split(":", 1)[1].strip()
        for line in _read_text(proc_root / "status").splitlines()
        if ":" in line
    }
    return {
        "cpu_ticks": int(fields[11]) + int(fields[12]),
        "clock_ticks": ticks,
        "peak_rss_bytes": _parse_kb(status_values["VmHWM"]),
    }


def read_thermal_snapshot(thermal_zone: Path = THERMAL_ZONE) -> dict[str, Any]:
    """Read the verified thermal zone and identify the sampling source."""
    zone_type = _read_text(thermal_zone / "type")
    temperature = int(_read_text(thermal_zone / "temp"))
    return {"zone": str(thermal_zone), "type": zone_type, "temperature_millicelsius": temperature}


def _glob_paths(pattern: str) -> list[Path]:
    # Path.glob does not accept an absolute pattern on all supported Python
    # versions; split at the first wildcard and glob from its stable root.
    marker = min((i for i in (pattern.find("*"), pattern.find("?")) if i >= 0), default=-1)
    if marker < 0:
        path = Path(pattern)
        return [path] if path.exists() else []
    prefix = pattern[:marker].rstrip("/")
    root = Path(prefix).parent
    suffix = pattern[len(str(root).rstrip("/")) + 1 :]
    try:
        return sorted(root.glob(suffix))
    except OSError:
        return []


def read_throttle_snapshot() -> dict[str, Any]:
    """Read kernel thermal-throttle counters, not a guessed boolean."""
    counts = _glob_paths(THROTTLE_GLOB)
    totals = _glob_paths(THROTTLE_TIME_GLOB)
    maximums = _glob_paths(THROTTLE_MAX_GLOB)
    if not counts:
        raise FileNotFoundError(THROTTLE_GLOB)
    return {
        "count": sum(int(_read_text(path)) for path in counts),
        "total_time_ms": sum(int(_read_text(path)) for path in totals),
        "max_time_ms": max((int(_read_text(path)) for path in maximums), default=0),
        "paths": [str(path) for path in counts],
    }


class CycleCostSampler:
    """Capture start/end readings for one active bridge cycle."""

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc/self"),
        thermal_zone: Path = THERMAL_ZONE,
        throttle_reader: Callable[[], dict[str, Any]] = read_throttle_snapshot,
        process_reader: Callable[[Path], dict[str, int]] = read_process_snapshot,
        thermal_reader: Callable[[Path], dict[str, Any]] = read_thermal_snapshot,
    ) -> None:
        self.proc_root = proc_root
        self.thermal_zone = thermal_zone
        self.throttle_reader = throttle_reader
        self.process_reader = process_reader
        self.thermal_reader = thermal_reader
        self.cycle_id: str | None = None
        self.started_at: str | None = None
        self._process_start: dict[str, int] | None = None
        self._thermal_start: dict[str, Any] | None = None
        self._throttle_start: dict[str, Any] | None = None

    def start(self, cycle_id: str) -> None:
        self.cycle_id = cycle_id
        self.started_at = _now()
        try:
            self._process_start = self.process_reader(self.proc_root)
        except Exception:
            self._process_start = None
        try:
            self._thermal_start = self.thermal_reader(self.thermal_zone)
        except Exception:
            self._thermal_start = None
        try:
            self._throttle_start = self.throttle_reader()
        except Exception:
            self._throttle_start = None

    def finish(self) -> dict[str, Any]:
        if self._process_start is None or self.started_at is None:
            raise RuntimeError("cycle sampler was not started")
        finished_at = _now()
        measured: dict[str, Any] = {
            "cycle_id": self.cycle_id,
            "sampled_during_cycle": True,
            "started_at": self.started_at,
            "finished_at": finished_at,
        }
        try:
            if self._process_start is None:
                raise RuntimeError("process baseline unavailable")
            process_end = self.process_reader(self.proc_root)
            ticks = process_end["cpu_ticks"] - self._process_start["cpu_ticks"]
            measured["cycle_cpu_seconds"] = _result(
                "present", max(0.0, ticks / self._process_start["clock_ticks"]), "seconds",
                "own process user+system CPU time delta",
                cycle_id=self.cycle_id,
            )
            measured["cycle_peak_rss"] = _result(
                "present", process_end["peak_rss_bytes"], "bytes",
                "own process peak VmHWM", cycle_id=self.cycle_id,
            )
        except Exception as exc:
            measured["cycle_cpu_seconds"] = unavailable(f"own-process CPU probe failed: {type(exc).__name__}", "seconds")
            measured["cycle_peak_rss"] = unavailable(f"own-process RSS probe failed: {type(exc).__name__}", "bytes")

        try:
            if self._thermal_start is None:
                raise RuntimeError("thermal baseline unavailable")
            thermal_end = self.thermal_reader(self.thermal_zone)
            measured["thermal_under_load"] = _result(
                "present", thermal_end["temperature_millicelsius"], "millicelsius",
                f"{thermal_end['type']} sampled between cycle start and finish",
                cycle_id=self.cycle_id, sampled_during_cycle=True,
            )
        except Exception as exc:
            measured["thermal_under_load"] = unavailable(f"thermal probe failed: {type(exc).__name__}", "millicelsius")

        try:
            if self._throttle_start is None:
                raise RuntimeError("throttle baseline unavailable")
            throttle_end = self.throttle_reader()
            delta = max(0, int(throttle_end["count"]) - int(self._throttle_start["count"]))
            measured["throttle_under_load"] = _result(
                "present", delta, "events",
                "thermal-throttle counter delta between cycle start and finish",
                cycle_id=self.cycle_id, sampled_during_cycle=True,
                total_time_delta_ms=max(0, int(throttle_end["total_time_ms"]) - int(self._throttle_start["total_time_ms"])),
            )
        except Exception as exc:
            measured["throttle_under_load"] = unavailable(f"thermal-throttle probe failed: {type(exc).__name__}", "events")
        measured["sampled_at"] = finished_at
        return measured


def framebuffer_surface_probe(
    *,
    size_path: Path = FRAMEBUFFER_SIZE,
    bpp_path: Path = FRAMEBUFFER_BPP,
) -> dict[str, Any]:
    """Return the full-surface byte cost from the verified framebuffer paths."""
    try:
        width, height = (int(part) for part in _read_text(size_path).split(",", 1))
        bpp = int(_read_text(bpp_path))
        value = width * height * bpp // 8
        state = "present" if value > 0 else "absent"
        return _result(state, value if state == "present" else None, "bytes", f"{width}x{height} at {bpp} bpp")
    except Exception as exc:
        return unavailable(f"framebuffer surface probe failed: {type(exc).__name__}", "bytes")


def full_surface_push_cost(write_surface: Callable[[bytes], Any], surface_bytes: int = FRAMEBUFFER_BYTES) -> dict[str, Any]:
    """Measure one push through an injected writer; never opens /dev/fb0 itself."""
    if surface_bytes <= 0:
        raise ValueError("surface_bytes must be positive")
    payload = bytes(surface_bytes)
    started = time.perf_counter_ns()
    write_surface(payload)
    elapsed_us = (time.perf_counter_ns() - started) / 1000.0
    return _result("present", elapsed_us, "microseconds", f"one {surface_bytes}-byte full-surface push")


def measure_framebuffer_push(
    *,
    framebuffer_path: Path = Path("/dev/fb0"),
    surface_bytes: int = FRAMEBUFFER_BYTES,
) -> dict[str, Any]:
    """Measure a single full-frame write to the verified framebuffer device."""
    try:
        with framebuffer_path.open("wb", buffering=0) as framebuffer:
            return full_surface_push_cost(framebuffer.write, surface_bytes)
    except Exception as exc:
        return unavailable(f"framebuffer push probe failed: {type(exc).__name__}", "microseconds")


def battery_probe() -> dict[str, Any]:
    """Permanent host fact: battery draw is not measurable on this machine."""
    return unavailable(PERMANENT_BATTERY_REASON, "watts")


def toolchain_measurement_placeholders() -> dict[str, dict[str, Any]]:
    """Return explicit no-run records for measurements requiring authorization."""
    return {
        "toolchain_build_peak_rss": unavailable(
            "build measurement not run: requires explicit operator authorization for host load", "bytes"
        ),
        "toolchain_cargo_build": unavailable(
            "cargo measurement not run: requires explicit operator authorization for host load", "seconds"
        ),
    }


def _proc_rss_bytes(pid: int) -> int:
    values = {
        line.split(":", 1)[0]: line.split(":", 1)[1].strip()
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines()
        if ":" in line
    }
    return _parse_kb(values["VmRSS"])


def _proc_children(pid: int) -> list[int]:
    raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8").strip()
    return [int(value) for value in raw.split()] if raw else []


def _process_tree_rss_bytes(pid: int) -> int:
    pending = [pid]
    seen: set[int] = set()
    total = 0
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            total += _proc_rss_bytes(current)
            pending.extend(_proc_children(current))
        except (FileNotFoundError, PermissionError, OSError, KeyError, ValueError):
            continue
    if not seen:
        raise FileNotFoundError(f"/proc/{pid}")
    return total


def measure_command_peak_rss(command: Sequence[str], cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    """Measure a command and its descendant tree; callers must opt in explicitly."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return unavailable(f"command probe failed: {type(exc).__name__}", "bytes")

    peak_bytes = 0
    sampling_error: Exception | None = None
    while process.poll() is None:
        try:
            peak_bytes = max(peak_bytes, _process_tree_rss_bytes(process.pid))
        except Exception as exc:
            sampling_error = exc
        if time.monotonic() - started >= timeout_seconds:
            process.kill()
            process.communicate()
            return unavailable(f"command probe timed out after {timeout_seconds}s", "bytes")
        time.sleep(0.05)
    process.communicate()
    try:
        peak_bytes = max(peak_bytes, _process_tree_rss_bytes(process.pid))
    except Exception as exc:
        sampling_error = sampling_error or exc
    if sampling_error is not None and peak_bytes == 0:
        return unavailable(f"child RSS probe failed: {type(sampling_error).__name__}", "bytes")
    if process.returncode != 0:
        return unavailable(f"command exited with status {process.returncode}", "bytes")
    return _result(
        "present", peak_bytes, "bytes",
        f"command and descendants; elapsed={time.monotonic() - started:.3f}s",
    )


def measure_command_duration(command: Sequence[str], cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    """Measure completion time for an explicitly authorized command."""
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return unavailable(f"command probe timed out after {timeout_seconds}s", "seconds")
    except Exception as exc:
        return unavailable(f"command probe failed: {type(exc).__name__}", "seconds")
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        return unavailable(f"command exited with status {completed.returncode}", "seconds")
    return _result("present", elapsed, "seconds", f"command completed within {timeout_seconds}s")


def merge_cost_measurements(caps: dict[str, Any], measurements: Mapping[str, Any] | None) -> dict[str, Any]:
    """Attach an authorized cycle measurement without changing old capability keys."""
    if not measurements:
        return caps
    for key, value in measurements.items():
        if isinstance(value, dict) and "state" in value:
            caps[key] = value
        elif key not in {"cycle_id", "sampled_during_cycle", "started_at", "finished_at", "sampled_at"}:
            caps[key] = value
    for key in ("cycle_id", "sampled_during_cycle", "started_at", "finished_at", "sampled_at"):
        if key in measurements:
            caps[f"_{key}"] = measurements[key]
    return caps
