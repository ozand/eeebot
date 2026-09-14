from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.cycle_cost_probe import (
    FRAMEBUFFER_BYTES,
    CycleCostSampler,
    battery_probe,
    framebuffer_surface_probe,
    full_surface_push_cost,
    read_process_snapshot,
    toolchain_measurement_placeholders,
)


def test_process_snapshot_uses_own_proc_files_and_parses_linux_layout(tmp_path: Path) -> None:
    proc = tmp_path / "self"
    proc.mkdir()
    (proc / "stat").write_text(
        "123 (python worker) R 1 2 3 4 5 6 7 8 9 10 11 12 13\n",
        encoding="utf-8",
    )
    (proc / "status").write_text("VmHWM:\t7 kB\nVmRSS:\t5 kB\n", encoding="utf-8")

    assert read_process_snapshot(proc, clock_ticks=100) == {
        "cpu_ticks": 23,
        "clock_ticks": 100,
        "peak_rss_bytes": 7 * 1024,
    }


def test_cycle_sampler_reports_attributable_deltas_and_load_sampling() -> None:
    process = iter([
        {"cpu_ticks": 10, "clock_ticks": 100, "peak_rss_bytes": 100},
        {"cpu_ticks": 35, "clock_ticks": 100, "peak_rss_bytes": 250},
    ])
    thermal = iter([
        {"zone": "/sys/class/thermal/thermal_zone0", "type": "acpitz", "temperature_millicelsius": 62000},
        {"zone": "/sys/class/thermal/thermal_zone0", "type": "acpitz", "temperature_millicelsius": 71000},
    ])
    throttle = iter([
        {"count": 4, "total_time_ms": 10, "max_time_ms": 5},
        {"count": 7, "total_time_ms": 28, "max_time_ms": 12},
    ])
    sampler = CycleCostSampler(
        process_reader=lambda _: next(process),
        thermal_reader=lambda _: next(thermal),
        throttle_reader=lambda: next(throttle),
    )
    sampler.start("cycle-cost-1")
    result = sampler.finish()

    assert result["cycle_id"] == "cycle-cost-1"
    assert result["sampled_during_cycle"] is True
    assert result["cycle_cpu_seconds"]["value"] == pytest.approx(0.25)
    assert result["cycle_peak_rss"]["value"] == 250
    assert result["thermal_under_load"]["value"] == 71000
    assert result["thermal_under_load"]["sampled_during_cycle"] is True
    assert result["throttle_under_load"]["value"] == 3
    assert result["throttle_under_load"]["total_time_delta_ms"] == 18


def test_cycle_sampler_keeps_unavailable_distinct_from_zero() -> None:
    process = iter([
        {"cpu_ticks": 10, "clock_ticks": 100, "peak_rss_bytes": 100},
        PermissionError("restricted"),
    ])

    def fail(_):
        value = next(process)
        if isinstance(value, Exception):
            raise value
        return value

    sampler = CycleCostSampler(
        process_reader=fail,
        thermal_reader=lambda _: {"type": "acpitz", "temperature_millicelsius": 62000},
        throttle_reader=lambda: {"count": 1, "total_time_ms": 0, "max_time_ms": 0},
    )
    sampler.start("cycle-unavailable")
    result = sampler.finish()
    assert result["cycle_cpu_seconds"]["state"] == "probe_unavailable"
    assert result["cycle_cpu_seconds"]["value"] is None
    assert result["cycle_peak_rss"]["state"] == "probe_unavailable"
    assert result["cycle_peak_rss"]["value"] is None


def test_framebuffer_probe_uses_verified_1024x600_32bpp_shape(tmp_path: Path) -> None:
    size = tmp_path / "virtual_size"
    bpp = tmp_path / "bits_per_pixel"
    size.write_text("1024,600\n", encoding="utf-8")
    bpp.write_text("32\n", encoding="utf-8")
    result = framebuffer_surface_probe(size_path=size, bpp_path=bpp)
    assert result["state"] == "present"
    assert result["value"] == 2_457_600
    assert result["unit"] == "bytes"
    assert result["value"] == FRAMEBUFFER_BYTES


def test_framebuffer_push_cost_is_unavailable_when_device_cannot_open(tmp_path: Path) -> None:
    from scripts.cycle_cost_probe import measure_framebuffer_push

    result = measure_framebuffer_push(framebuffer_path=tmp_path / "missing", surface_bytes=16)
    assert result["state"] == "present"
    assert result["value"] >= 0


def test_framebuffer_probe_failure_is_unavailable(tmp_path: Path) -> None:
    result = framebuffer_surface_probe(size_path=tmp_path / "missing", bpp_path=tmp_path / "missing-bpp")
    assert result["state"] == "probe_unavailable"
    assert result["value"] is None


def test_full_surface_push_reports_microseconds_and_exact_payload_size() -> None:
    observed: list[bytes] = []
    result = full_surface_push_cost(observed.append, surface_bytes=16)
    assert result["state"] == "present"
    assert result["unit"] == "microseconds"
    assert result["value"] >= 0
    assert observed == [b"\0" * 16]


def test_battery_is_permanently_unavailable_without_zero() -> None:
    result = battery_probe()
    assert result["state"] == "probe_unavailable"
    assert result["value"] is None
    assert "no BAT* device" in result["details"]


def test_toolchain_measurements_are_explicitly_unavailable_without_host_load() -> None:
    result = toolchain_measurement_placeholders()
    assert set(result) == {"toolchain_build_peak_rss", "toolchain_cargo_build"}
    assert all(item["state"] == "probe_unavailable" for item in result.values())
    assert all(item["value"] is None for item in result.values())
    assert all("explicit operator authorization" in item["details"] for item in result.values())
