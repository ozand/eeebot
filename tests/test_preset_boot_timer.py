"""The preset must not erase the bridge's first boot trigger (#2156)."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "host/eeepc/scripts/apply_preset.sh"
BASE = ROOT / "host/eeepc/systemd/eeepc-self-evolving-subagent-bridge.timer"


def effective_timer(text):
    triggers = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("OnBootSec=", "OnUnitActiveSec=", "OnUnitInactiveSec=")):
            key, value = line.split("=", 1)
            if not value:
                triggers.clear()  # systemd resets the complete timer list
            else:
                triggers.setdefault(key, []).append(value)
    return triggers


def run_preset(tmp_path, pause):
    """Run the real script against disposable paths with systemctl stubbed."""
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "sample.env").write_text(f'SELFEVO_CYCLE_PAUSE="{pause}"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "systemctl"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    text = SCRIPT.read_text(encoding="utf-8")
    replacements = {
        "PRESETS_DIR=/etc/eeepc-agent/presets": f"PRESETS_DIR='{presets}'",
        "PRESET_SYMLINK=/etc/eeepc-agent/preset.env": f"PRESET_SYMLINK='{tmp_path / 'preset.env'}'",
        "INSTANCE_ENV_FILE=/etc/eeepc-agent/instances/self-evolving-subagent-bridge.env":
            f"INSTANCE_ENV_FILE='{tmp_path / 'instance.env'}'",
        "TIMER_DROPIN_DIR=/etc/systemd/system/eeepc-self-evolving-subagent-bridge.timer.d":
            f"TIMER_DROPIN_DIR='{tmp_path / 'timer.d'}'",
        "if [[ $EUID -ne 0 ]]; then": "if false; then",
    }
    for before, after in replacements.items():
        assert text.count(before) == 1
        text = text.replace(before, after)
    script = tmp_path / "apply.sh"
    script.write_text(text, encoding="utf-8")
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
    subprocess.run(["bash", str(script), "sample"], env=env, check=True, capture_output=True)
    return tmp_path / "timer.d/preset.conf"


def test_generated_pause_keeps_boot_after_reset(tmp_path):
    dropin = run_preset(tmp_path, "3m")
    effective = effective_timer(BASE.read_text(encoding="utf-8") + "\n" + dropin.read_text(encoding="utf-8"))
    assert effective == {"OnBootSec": ["4m"], "OnUnitInactiveSec": ["3m"]}


def test_empty_pause_preserves_stock_timer(tmp_path):
    dropin = run_preset(tmp_path, "")
    assert not dropin.exists()
    assert effective_timer(BASE.read_text(encoding="utf-8")) == {
        "OnBootSec": ["4m"], "OnUnitActiveSec": ["15m"]
    }
