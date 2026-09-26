"""Tests for ADR-036 D3 Part A: host capability probe moved to its own unit.

Verifies:
- The probe output written by scripts/probe_host_capabilities.py is byte-for-byte
  identical to the output from scripts/eeebot_dashboard.py on the same input (ADR-036).
- The systemd service host/eeepc/systemd/eeebot-host-capabilities.service executes
  the dedicated probe script rather than the dashboard server (ADR-036).
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts import eeebot_dashboard, probe_host_capabilities


def test_probe_moved_output_unchanged(tmp_path: Path, monkeypatch) -> None:
    """ADR-036 D3: output of scripts/probe_host_capabilities.py is byte-for-byte
    identical to scripts/eeebot_dashboard.py on the same input.
    """
    old_state = tmp_path / "old_state"
    new_state = tmp_path / "new_state"
    old_state.mkdir()
    new_state.mkdir()

    frozen_timestamp = "2026-09-26T00:00:00.000000+00:00"
    trigger = "systemd_timer"

    monkeypatch.setattr("scripts.cycle_cost_probe._now", lambda: "2026-09-26T00:00:00Z")
    monkeypatch.setattr("pathlib.Path.glob", lambda self, pattern: [])
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    monkeypatch.setattr(
        "pathlib.Path.read_text",
        lambda self, *args, **kwargs: {
            "/proc/cpuinfo": "model name : Intel(R) Atom(TM) CPU N270   @ 1.60GHz\n",
            "/proc/meminfo": "MemTotal: 1018240 kB\nMemAvailable: 512000 kB\n",
            "/proc/asound/cards": " 0 [Intel          ]: HDA-Intel - HDA Intel\n",
        }.get(str(self).replace("\\", "/"), ""),
    )
    monkeypatch.setattr(
        "subprocess.check_output",
        lambda command, **kwargs: {
            ("lsusb",): b"",
            ("ip", "-o", "link", "show"): b"1: lo: <LOOPBACK> \n2: eth0: <BROADCAST> \n",
            ("df", "-h", "/"): b"Filesystem  Size  Used Avail Use% Mounted on\n/dev/sda1  15G  5G  10G  33% /\n",
            ("uname", "-r"): b"6.1.0-25-686\n",
            ("uptime", "-p"): b"up 1 day, 2 hours\n",
        }[tuple(command)],
    )

    # 1. Run old probe via eeebot_dashboard
    monkeypatch.setattr(eeebot_dashboard, "STATE_DIR", old_state)
    eeebot_dashboard.refresh_host_capabilities(
        trigger=trigger,
        scan_timestamp=frozen_timestamp,
    )

    # 2. Run new probe directly via probe_host_capabilities
    probe_host_capabilities.refresh_host_capabilities(
        trigger=trigger,
        state_dir=new_state,
        scan_timestamp=frozen_timestamp,
    )

    old_file = old_state / "host_capabilities.json"
    new_file = new_state / "host_capabilities.json"

    assert old_file.is_file(), "old_state host_capabilities.json must exist"
    assert new_file.is_file(), "new_state host_capabilities.json must exist"

    old_bytes = old_file.read_bytes()
    new_bytes = new_file.read_bytes()

    assert new_bytes == old_bytes, (
        f"ADR-036: probe output must be byte-for-byte identical:\n"
        f"OLD:\n{old_bytes.decode('utf-8')}\n"
        f"NEW:\n{new_bytes.decode('utf-8')}"
    )

    parsed = json.loads(new_bytes.decode("utf-8"))
    assert parsed["_probe_trigger"] == trigger
    assert parsed["_scan_timestamp"] == frozen_timestamp
    assert parsed["cpu"]["details"] == "Intel(R) Atom(TM) CPU N270   @ 1.60GHz"
    assert parsed["kernel"]["details"] == "6.1.0-25-686"


def test_unit_file_executes_dedicated_probe_script() -> None:
    """ADR-036 D3: host/eeepc/systemd/eeebot-host-capabilities.service executes
    scripts/probe_host_capabilities.py, not eeebot_dashboard.py.
    """
    repo_root = Path(__file__).resolve().parents[1]
    unit_path = repo_root / "host" / "eeepc" / "systemd" / "eeebot-host-capabilities.service"
    assert unit_path.is_file(), f"unit file {unit_path} must exist"

    content = unit_path.read_text(encoding="utf-8")
    assert "probe_host_capabilities.py" in content, (
        "ADR-036: eeebot-host-capabilities.service must execute scripts/probe_host_capabilities.py"
    )
    assert "eeebot_dashboard.py --refresh-host-caps" not in content, (
        "ADR-036: eeebot-host-capabilities.service must not invoke eeebot_dashboard.py"
    )
