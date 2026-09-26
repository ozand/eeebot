#!/usr/bin/env python3
"""Standalone host capability probe for eeebot (#1557, ADR-036 D3).

Re-scans host hardware and writes state/host_capabilities.json.
Decoupled from eeebot_dashboard.py per ADR-036 Phase D3, so host capability
inventory updates run from their own dedicated systemd unit without requiring
a dashboard server or HTTP routes.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.cycle_cost_probe import (
        battery_probe,
        framebuffer_surface_probe,
        service_account_group_membership_probe,
        toolchain_measurement_placeholders,
    )
except ModuleNotFoundError:
    from cycle_cost_probe import (
        battery_probe,
        framebuffer_surface_probe,
        service_account_group_membership_probe,
        toolchain_measurement_placeholders,
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
_SYSTEM_STATE_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/state")


def get_default_state_dir() -> Path:
    """Resolve the default state directory according to host environment."""
    if os.getenv("EEEBOT_STATE_DIR"):
        return Path(os.getenv("EEEBOT_STATE_DIR"))
    if _SYSTEM_STATE_DIR.exists():
        return _SYSTEM_STATE_DIR
    return REPO_ROOT / "state"


def refresh_host_capabilities(
    *,
    trigger: str = "dashboard_refresh",
    cycle_id: str | None = None,
    process_probe: Callable[[], dict[str, Any]] | None = None,
    state_dir: Path | None = None,
    scan_timestamp: str | None = None,
) -> dict[str, Any]:
    """Re-scan host hardware and write a triggered inventory (#1557, ADR-036)."""

    def result(state: str, details: str) -> dict[str, Any]:
        return {
            "state": state,
            "available": state == "present",
            "details": details,
        }

    def read_probe(path: Path, parse: Any) -> dict[str, Any]:
        try:
            return result(*parse(path.read_text()))
        except Exception as exc:
            return result("probe_unavailable", f"probe failed: {type(exc).__name__}")

    def command_probe(command: list[str], parse: Any) -> dict[str, Any]:
        try:
            output = subprocess.check_output(command, stderr=subprocess.DEVNULL).decode()
        except Exception as exc:
            return result("probe_unavailable", f"probe failed: {type(exc).__name__}")
        try:
            state, details = parse(output)
        except Exception as exc:
            return result("probe_unavailable", f"probe output unreadable: {type(exc).__name__}")
        return result(state, details)

    caps: dict[str, Any] = {}

    if process_probe is None:
        caps["cycle_body"] = result(
            "probe_unavailable",
            "own-process sample requires an active cycle; daily probe is idle",
        )
    else:
        try:
            body = process_probe()
            if not isinstance(body, dict):
                raise TypeError("process probe must return an object")
            state = str(body.get("state") or "probe_unavailable")
            if state not in {"present", "absent", "present_uninitialized", "probe_unavailable"}:
                raise ValueError("invalid process probe state")
            caps["cycle_body"] = body
        except Exception as exc:
            caps["cycle_body"] = result("probe_unavailable", f"probe failed: {type(exc).__name__}")

    caps["battery_draw"] = battery_probe()
    caps["service_account_groups"] = service_account_group_membership_probe()

    # Camera
    try:
        videos = sorted(str(path) for path in Path("/dev").glob("video*"))
        caps["camera"] = result(
            "present" if videos else "absent",
            f"Detected {', '.join(videos)}" if videos else "not detected",
        )
    except Exception as exc:
        caps["camera"] = result("probe_unavailable", f"probe failed: {type(exc).__name__}")

    # Bluetooth
    try:
        bluetooth_devices = sorted(Path("/sys/class/bluetooth").glob("hci*"))
        if bluetooth_devices:
            states: list[tuple[str, str, str | None]] = []
            for device in bluetooth_devices:
                rfkill_paths = sorted(device.glob("rfkill*"))
                if not rfkill_paths:
                    states.append((device.name, "", None))
                    continue
                rfkill = rfkill_paths[0]
                soft = (rfkill / "soft").read_text(encoding="utf-8").strip()
                hard = (rfkill / "hard").read_text(encoding="utf-8").strip()
                states.append((device.name, soft, hard))
            if any(soft == "0" and hard == "0" for _, soft, hard in states):
                details = f"Detected {', '.join(name for name, _, _ in states)} via rfkill"
                caps["bluetooth"] = result("present", details)
            else:
                blocked_parts = []
                for name, soft, hard in states:
                    if hard is None:
                        reason = "no rfkill state"
                    else:
                        flags = ("soft" if soft == "1" else "") + ("+" if soft == "1" and hard == "1" else "") + ("hard" if hard == "1" else "")
                        reason = f"{flags} blocked"
                    blocked_parts.append(f"{name} ({reason})")
                caps["bluetooth"] = result("present_uninitialized", f"{', '.join(blocked_parts)} via rfkill")
        else:
            rfkill_devices = []
            for path in sorted(Path("/sys/class/rfkill").glob("rfkill*")):
                try:
                    if (path / "type").read_text(encoding="utf-8").strip().lower() == "bluetooth":
                        rfkill_devices.append(path.name)
                except (FileNotFoundError, OSError):
                    continue
            caps["bluetooth"] = result(
                "present_uninitialized" if rfkill_devices else "absent",
                f"Detected Bluetooth rfkill: {', '.join(rfkill_devices)} (hci unavailable)" if rfkill_devices else "not detected",
            )
    except OSError as exc:
        caps["bluetooth"] = result("probe_unavailable", f"probe failed: {type(exc).__name__}")

    # WiFi
    def parse_wifi(output: str) -> tuple[str, str]:
        wifi_ifaces = [line.split(":")[1].strip() for line in output.splitlines() if "wlan" in line or "wlp" in line]
        return (
            ("present", f"Detected {', '.join(wifi_ifaces)}")
            if wifi_ifaces
            else ("absent", "not detected")
        )

    caps["wifi"] = command_probe(["ip", "-o", "link", "show"], parse_wifi)

    # Microphone
    def parse_microphone(text: str) -> tuple[str, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return (
            ("present", lines[0])
            if lines and any("[" in line for line in lines)
            else ("absent", "not detected")
        )

    caps["microphone"] = read_probe(Path("/proc/asound/cards"), parse_microphone)

    # CPU
    caps["cpu"] = read_probe(
        Path("/proc/cpuinfo"),
        lambda text: (
            "present",
            next(line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("model name")),
        ),
    )

    # Memory
    def parse_memory(text: str) -> tuple[str, str]:
        lines = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in text.splitlines() if ":" in line}
        if "MemTotal" not in lines:
            return "absent", "not detected"
        return "present", f"Mem: total={lines.get('MemTotal', '?')} available={lines.get('MemAvailable', '?')}"

    caps["memory"] = read_probe(Path("/proc/meminfo"), parse_memory)

    # Disk
    def parse_disk(text: str) -> tuple[str, str]:
        parts = text.strip().splitlines()[1].split()
        return "present", f"{parts[0]} {parts[1]} total, {parts[2]} used, {parts[3]} free, {parts[4]} used on /"

    caps["disk"] = command_probe(["df", "-h", "/"], parse_disk)
    caps["kernel"] = command_probe(["uname", "-r"], lambda output: ("present", output.strip()) if output.strip() else ("absent", "not detected"))
    caps["uptime"] = command_probe(["uptime", "-p"], lambda output: ("present", output.strip()) if output.strip() else ("absent", "not detected"))

    # Screen resolution and one full-surface transfer cost
    try:
        screen_res = "unknown"
        fb_size_path = Path("/sys/class/graphics/fb0/virtual_size")
        if fb_size_path.exists():
            screen_res = fb_size_path.read_text().strip().replace(",", "x")
        else:
            for mode_path in Path("/sys/class/drm").glob("card*-*/modes"):
                if mode_path.exists():
                    lines = mode_path.read_text().strip().splitlines()
                    if lines:
                        screen_res = lines[0].strip()
                        break
        screen_state = "present" if screen_res != "unknown" else "absent"
        caps["screen"] = result(
            screen_state,
            f"Physical Resolution: {screen_res}" if screen_res != "unknown" else "not detected",
        )
        caps["screen_push"] = framebuffer_surface_probe()
    except Exception as exc:
        caps["screen"] = result("probe_unavailable", f"probe failed: {type(exc).__name__}")

    caps.update(toolchain_measurement_placeholders())

    if cycle_id:
        caps["_cycle_id"] = cycle_id
    caps["_scan_timestamp"] = scan_timestamp or datetime.now(timezone.utc).isoformat()
    caps["_probe_trigger"] = trigger

    effective_state_dir = state_dir or get_default_state_dir()
    host_caps_path = effective_state_dir / "host_capabilities.json"
    host_caps_path.parent.mkdir(parents=True, exist_ok=True)
    host_caps_path.write_text(json.dumps(caps, indent=2) + "\n", encoding="utf-8")

    return caps


def main() -> None:
    trigger = os.getenv("EEEBOT_CAPABILITY_PROBE_TRIGGER", "cli_refresh")
    caps = refresh_host_capabilities(trigger=trigger)
    print("Host capabilities refreshed:")
    for name, info in caps.items():
        if name.startswith("_"):
            continue
        state = info.get("state", "present" if info.get("available") else "absent")
        status = {
            "present": "✓",
            "present_uninitialized": "!",
            "absent": "✗",
            "probe_unavailable": "?",
        }.get(state, "?")
        print(f"  {status} {name} [{state}]: {info.get('details', 'unknown')}")
    print(f"\nScan timestamp: {caps.get('_scan_timestamp', 'unknown')}")


if __name__ == "__main__":
    main()
