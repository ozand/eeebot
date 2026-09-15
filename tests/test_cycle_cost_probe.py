from __future__ import annotations

import time
from pathlib import Path

import pytest

from scripts.cycle_cost_probe import (
    FRAMEBUFFER_BYTES,
    CycleCostSampler,
    battery_probe,
    framebuffer_surface_probe,
    full_surface_push_cost,
    read_process_snapshot,
    service_account_group_membership_probe,
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
    assert result["cpu_seconds"]["value"] == pytest.approx(0.25)
    assert result["peak_rss_bytes"]["value"] == 250
    assert result["peak_temperature_millicelsius"]["value"] == 71000
    assert result["peak_temperature_millicelsius"]["sampled_during_cycle"] is True
    assert result["throttle_events"]["value"] == 3
    assert result["throttle_events"]["total_time_delta_ms"] == 18


def test_cycle_sampler_uses_peak_temperature_and_one_second_sampling(monkeypatch) -> None:
    from scripts import cycle_cost_probe as probe

    monkeypatch.setattr(probe, "SAMPLE_INTERVAL_SECONDS", 0.001)
    process = iter([
        {"cpu_ticks": 10, "clock_ticks": 100, "peak_rss_bytes": 100},
        {"cpu_ticks": 11, "clock_ticks": 100, "peak_rss_bytes": 200},
    ])
    temperatures = iter([
        {"type": "acpitz", "temperature_millicelsius": 53000},
        {"type": "acpitz", "temperature_millicelsius": 62000},
        {"type": "acpitz", "temperature_millicelsius": 55000},
    ])
    throttles = iter([
        {"count": 2, "total_time_ms": 4},
        {"count": 2, "total_time_ms": 4},
    ])
    sampler = probe.CycleCostSampler(
        process_reader=lambda _: next(process),
        thermal_reader=lambda _: next(temperatures),
        throttle_reader=lambda: next(throttles),
    )
    sampler.start("cycle-peak")
    time.sleep(0.01)
    result = sampler.finish()
    assert result["peak_temperature_millicelsius"]["value"] == 62000


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
    assert result["cpu_seconds"]["state"] == "probe_unavailable"
    assert result["cpu_seconds"]["value"] is None
    assert result["peak_rss_bytes"]["state"] == "probe_unavailable"
    assert result["peak_rss_bytes"]["value"] is None


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


def test_framebuffer_push_cost_can_measure_an_injected_writable_surface(tmp_path: Path) -> None:
    from scripts.cycle_cost_probe import measure_framebuffer_push

    target = tmp_path / "framebuffer"
    target.write_bytes(b"")
    result = measure_framebuffer_push(framebuffer_path=target, surface_bytes=16)
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


def test_battery_probe_distinguishes_presence_absence_and_unanswerable(tmp_path: Path) -> None:
    # 1. Unanswerable: portable chassis type (Notebook = 10), but no power supply or ACPI slot exposed
    chassis_dir = tmp_path / "chassis"
    chassis_dir.mkdir(parents=True)
    chassis_file = chassis_dir / "chassis_type"
    chassis_file.write_text("10\n", encoding="utf-8")
    empty_power = tmp_path / "empty_power"
    empty_power.mkdir(parents=True)
    empty_acpi = tmp_path / "empty_acpi"
    empty_acpi.mkdir(parents=True)

    res_unanswerable = battery_probe(
        power_supply_path=empty_power,
        acpi_devices_path=empty_acpi,
        chassis_path=chassis_file,
    )
    assert res_unanswerable["state"] == "unanswerable"
    assert "portable chassis type" in res_unanswerable["details"]

    # 2. Absent: ACPI PNP0C0A slot exists, but no battery power supply device (disconnected battery on notebook)
    acpi_dir = tmp_path / "acpi"
    (acpi_dir / "PNP0C0A_00").mkdir(parents=True)
    res_absent = battery_probe(
        power_supply_path=empty_power,
        acpi_devices_path=acpi_dir,
        chassis_path=chassis_file,
    )
    assert res_absent["state"] == "absent"
    assert "battery slot PNP0C0A_00 empty / disconnected" in res_absent["details"]

    # 3. Present: battery supply exists
    bat_dir = tmp_path / "bat_power"
    (bat_dir / "BAT0").mkdir(parents=True)
    (bat_dir / "BAT0" / "status").write_text("Discharging\n", encoding="utf-8")
    res_present = battery_probe(
        power_supply_path=bat_dir,
        acpi_devices_path=acpi_dir,
        chassis_path=chassis_file,
    )
    assert res_present["state"] == "present"
    assert "BAT0" in res_present["details"]


def test_service_account_group_membership_probe() -> None:
    res = service_account_group_membership_probe()
    assert res["state"] in ("present", "absent", "probe_unavailable")
    assert res["unit"] == "groups"


def test_cycle_cost_ledger_record_is_four_numbers_and_not_samples() -> None:
    from scripts.cycle_cost_probe import _result

    measurement = {
        "cpu_seconds": _result("present", 237.97, "seconds", "own process"),
        "peak_rss_bytes": _result("present", 82530304, "bytes", "own process VmHWM"),
        "peak_temperature_millicelsius": _result("present", 62000, "millicelsius", "sampled during cycle"),
        "throttle_events": _result("present", 0, "events", "counter delta during cycle"),
    }
    assert len(measurement) == 4
    assert all("samples" not in item for item in measurement.values())


def test_toolchain_measurements_are_explicitly_unavailable_without_host_load() -> None:
    result = toolchain_measurement_placeholders()
    assert set(result) == {"toolchain_build_peak_rss", "toolchain_cargo_build"}
    assert all(item["state"] == "probe_unavailable" for item in result.values())
    assert all(item["value"] is None for item in result.values())
    assert all("explicit operator authorization" in item["details"] for item in result.values())
