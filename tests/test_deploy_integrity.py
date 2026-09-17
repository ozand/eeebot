"""Tests for host deployment script and systemd unit integrity (#1037)."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEMD_DIR = REPO_ROOT / "host" / "eeepc" / "systemd"
DROP_INS_DIR = SYSTEMD_DIR / "drop-ins"
DEPLOY_SCRIPT = REPO_ROOT / "host" / "eeepc" / "scripts" / "deploy_release.sh"
INSTALL_SCRIPT = REPO_ROOT / "host" / "eeepc" / "scripts" / "install.sh"

_SECTION_HEADER = re.compile(r"\[[A-Za-z]+\]")
_DIRECTIVE = re.compile(r"[A-Za-z][A-Za-z0-9]*=.*")


def test_deploy_installs_systemd_drop_ins_with_unit_discipline() -> None:
    """#1701 part 2: the unit copy skipped ``*.d/*.conf``.

    #1663 found the crash recorder's ``ExecStopPost=`` never installed for 14
    days because it lived in a drop-in only install.sh (first-time setup)
    copied. The deploy must walk the same ``drop-ins/<unit>.d/<name>.conf``
    layout install.sh does, create the ``.d`` directory, and apply the same
    root:root 0644 repair the units get -- all before the daemon-reload that
    makes them effective.
    """
    content = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    block = content[content.index("syncing systemd units + reloading"):content.index("sync_timer()")]

    assert 'for dropin_source in "$RELEASE_DIR"/host/eeepc/systemd/drop-ins/*.d/*.conf; do' in block
    assert '[ -f "$dropin_source" ] || continue' in block
    assert 'dropin_dir="$(basename "$(dirname "$dropin_source")")"' in block
    assert 'dropin_name="$(basename "$dropin_source")"' in block
    assert 'sudo mkdir -p "/etc/systemd/system/$dropin_dir"' in block
    assert 'sudo cp "$dropin_source" "/etc/systemd/system/$dropin_dir/$dropin_name"' in block
    assert 'sudo chown root:root "/etc/systemd/system/$dropin_dir/$dropin_name"' in block
    assert 'sudo chmod 0644 "/etc/systemd/system/$dropin_dir/$dropin_name"' in block
    assert block.index("drop-ins/*.d/*.conf") < block.index("sudo systemctl daemon-reload"), (
        "drop-ins must be on disk before the reload that makes them effective")
    assert block.count("sudo systemctl daemon-reload") == 1, "one reload after units and drop-ins"

    # The deploy and first-time install must agree on where drop-ins live, or
    # the two writers drift apart again.
    install = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert '"$src"/drop-ins/**/*.conf' in install
    assert 'local rel="${f#"$src/drop-ins/"}"' in install


def test_repo_drop_ins_are_unit_fragments_for_shipped_units() -> None:
    """Every tracked drop-in parses as a systemd unit fragment and attaches to a unit this repo ships.

    A file the deploy glob cannot see (wrong extension, nested one level too
    deep) would be tracked but never installed -- the #1663 shape again.
    """
    confs = sorted(DROP_INS_DIR.glob("*.d/*.conf"))
    assert confs, f"expected at least one drop-in under {DROP_INS_DIR}"
    everything = {path for path in DROP_INS_DIR.rglob("*") if path.is_file()}
    assert everything == set(confs), (
        f"files the deploy glob drop-ins/*.d/*.conf does not reach: {sorted(everything - set(confs))}")

    shipped_units = {path.name for path in SYSTEMD_DIR.glob("*.service")} | {path.name for path in SYSTEMD_DIR.glob("*.timer")}
    for conf in confs:
        unit_dir = conf.parent.name
        assert unit_dir.endswith(".d"), f"{conf.parent} is not a <unit>.d directory"
        assert unit_dir[:-2] in shipped_units, f"{conf} attaches to {unit_dir[:-2]}, which this repo does not ship"

        lines = [line.strip() for line in conf.read_text(encoding="utf-8").splitlines()]
        body = [line for line in lines if line and not line.startswith(("#", ";"))]
        assert body, f"{conf} has no directives"
        assert _SECTION_HEADER.fullmatch(body[0]), f"{conf} must open with a [Section] header, got {body[0]!r}"
        for line in body[1:]:
            assert _SECTION_HEADER.fullmatch(line) or _DIRECTIVE.fullmatch(line), f"{conf}: not a unit directive: {line!r}"


def test_ghost_units_removed_from_repo() -> None:
    systemd_dir = REPO_ROOT / "host" / "eeepc" / "systemd"
    ghost_service = systemd_dir / "eeepc-network-fallback.service"
    ghost_timer = systemd_dir / "eeepc-network-fallback.timer"
    assert not ghost_service.exists(), f"Ghost unit {ghost_service} should be removed"
    assert not ghost_timer.exists(), f"Ghost unit {ghost_timer} should be removed"


def test_systemd_resource_limits_bridge_and_verifier() -> None:
    systemd_dir = REPO_ROOT / "host" / "eeepc" / "systemd"
    bridge_service = systemd_dir / "eeepc-self-evolving-subagent-bridge.service"
    verifier_service = systemd_dir / "eeepc-promotion-verifier.service"

    assert bridge_service.exists()
    assert verifier_service.exists()

    bridge_content = bridge_service.read_text(encoding="utf-8")
    verifier_content = verifier_service.read_text(encoding="utf-8")

    for content, name in [(bridge_content, "bridge"), (verifier_content, "verifier")]:
        assert "MemoryMax=512M" in content, f"{name} must define MemoryMax=512M"
        assert "MemoryHigh=400M" in content, f"{name} must define MemoryHigh=400M"
        assert "CPUQuota=150%" in content, f"{name} must define CPUQuota=150%"
        assert "Nice=10" in content, f"{name} must define Nice=10"
        assert "CPUSchedulingPolicy=other" in content, f"{name} must define CPUSchedulingPolicy=other"
        assert "IOSchedulingClass=best-effort" in content, f"{name} must define IOSchedulingClass=best-effort"
        assert "IOSchedulingPriority=5" in content, f"{name} must define IOSchedulingPriority=5"


def test_host_capabilities_probe_runs_daily_as_eeepc_agent() -> None:
    systemd_dir = REPO_ROOT / "host" / "eeepc" / "systemd"
    service = (systemd_dir / "eeebot-host-capabilities.service").read_text(encoding="utf-8")
    timer = (systemd_dir / "eeebot-host-capabilities.timer").read_text(encoding="utf-8")
    install = (REPO_ROOT / "host" / "eeepc" / "scripts" / "install.sh").read_text(encoding="utf-8")
    deploy = (REPO_ROOT / "host" / "eeepc" / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")

    assert "User=eeepc-agent" in service
    assert "Group=eeepc-agent" in service
    assert "eeebot_dashboard.py --refresh-host-caps" in service
    assert "Environment=EEEBOT_CAPABILITY_PROBE_TRIGGER=systemd_timer" in service
    assert "ReadWritePaths=/var/lib/eeepc-agent/self-evolving-agent/state" in service
    assert "OnCalendar=*-*-* 01:00:00" in timer
    assert "OnUnitActiveSec=" not in timer
    assert "Persistent=true" in timer
    assert "Unit=eeebot-host-capabilities.service" in timer
    assert "eeebot-host-capabilities.timer" in install
    assert "sync_timer eeebot-host-capabilities.timer required" in deploy


def test_host_metrics_schedule_is_enabled_and_writable() -> None:
    systemd_dir = REPO_ROOT / "host" / "eeepc" / "systemd"
    service = (systemd_dir / "eeebot-host-metrics.service").read_text(encoding="utf-8")
    timer = (systemd_dir / "eeebot-host-metrics.timer").read_text(encoding="utf-8")
    install = (REPO_ROOT / "host" / "eeepc" / "scripts" / "install.sh").read_text(encoding="utf-8")
    deploy = (REPO_ROOT / "host" / "eeepc" / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")

    assert "collect_host_metrics.py --state-dir /var/lib/eeepc-agent/self-evolving-agent/state" in service
    assert "ReadWritePaths=/var/lib/eeepc-agent/self-evolving-agent/state/feeds /var/lib/eeepc-agent/self-evolving-agent/state/host_metrics" in service
    assert "OnBootSec=10min" in timer
    assert "OnUnitActiveSec=6h" in timer
    assert "Persistent=true" in timer
    assert "eeebot-host-metrics.timer" in install
    assert "sync_timer eeebot-host-metrics.timer required" in deploy


def test_deploy_script_fail_closed_and_ghost_cleanup() -> None:
    deploy_script = REPO_ROOT / "host" / "eeepc" / "scripts" / "deploy_release.sh"
    assert deploy_script.exists()
    content = deploy_script.read_text(encoding="utf-8")

    # Critical chowns must not be swallowed with || true
    assert 'sudo chown -R root:root "$RELEASE_DIR" "$VENV_BASE" 2>/dev/null || true' not in content
    assert 'sudo chown -R root:root "$RELEASE_DIR" "$VENV_BASE"' in content

    # Release directory and scaffolding ownership checked BEFORE current symlink update
    ownership_fix_pos = content.index("fixing ownership and permissions on release")
    stat_release_pos = content.index('stat -c \'%u:%g\' "$RELEASE_DIR"')
    symlink_update_pos = content.index("updating current symlink")
    assert ownership_fix_pos < stat_release_pos < symlink_update_pos

    # Post-hoc critical ownership checks
    assert "CRITICAL:" in content

    # Ghost unit removal on host without swallowing stop/disable failures
    assert "sudo systemctl disable --now eeepc-network-fallback.timer 2>/dev/null || true" not in content
    assert "sudo systemctl stop eeepc-network-fallback.service 2>/dev/null || true" not in content
    assert "eeepc-network-fallback.timer" in content
    assert "eeepc-network-fallback.service" in content
    assert "sudo rm -f /etc/systemd/system/eeepc-network-fallback.timer /etc/systemd/system/eeepc-network-fallback.service" in content
    # #1259: CRITICALs go through `die` (same text, returns 1 so the ERR trap fires)
    assert 'die "$ghost_unit is still active after purge"' in content

    # #1461: cp preserves an existing destination inode's ownership and mode.
    # The repair must be scoped to exactly the service/timer files this sync writes
    # and must fail the deploy if either assertion cannot be applied.
    assert 'for unit_source in "$RELEASE_DIR"/host/eeepc/systemd/*.service "$RELEASE_DIR"/host/eeepc/systemd/*.timer; do' in content
    assert 'unit_name="$(basename "$unit_source")"' in content
    assert 'sudo chown root:root "/etc/systemd/system/$unit_name"' in content
    assert 'sudo chmod 0644 "/etc/systemd/system/$unit_name"' in content
    assert 'sudo systemctl daemon-reload' in content
    assert content.index('sudo chown root:root "/etc/systemd/system/$unit_name"') < content.index('sudo systemctl daemon-reload', content.index('syncing systemd units + reloading'))
    assert '/etc/systemd/system/*' not in content[content.index('syncing systemd units + reloading'):content.index('sync_timer()')]

    # #1236: a long-running dashboard must restart after current activation,
    # while rollback restores it before restarting the bridge.
    assert 'DASHBOARD_UNIT=eeebot-dashboard.service' in content
    assert 'sudo systemctl restart "$DASHBOARD_UNIT"' in content
    assert 'systemctl show "$DASHBOARD_UNIT" -p MainPID --value' in content
    assert 'readlink -v "/proc/$DASHBOARD_PID/cwd"' in content
    assert 'sudo systemctl restart eeebot-dashboard.service && sudo systemctl restart eeepc-self-evolving-subagent-bridge.service' in content
    assert content.index('updating current symlink') < content.index('sudo systemctl restart "$DASHBOARD_UNIT"') < content.index('Ensure bridge service is restarted correctly')

    # Timer synchronization honors disabled units and verifies both enabled and active states
    assert "sync_timer" in content
    assert "administratively disabled" in content
    assert "eeepc-promotion-verifier.timer required" in content
    assert "final_state" in content

    # Presence checks use systemd's load state, not list-unit-files exit status.
    assert 'systemctl show "$timer" -p LoadState --value' in content
    assert 'systemctl show "$ghost_unit" -p LoadState --value' in content
    # Both retired units are stopped and disabled before files are removed.
    assert 'sudo systemctl stop "$ghost_unit"' in content
    assert 'sudo systemctl disable "$ghost_unit"' in content
    assert 'for ghost_unit in eeepc-network-fallback.timer eeepc-network-fallback.service' in content
    assert 'ghost_load_state" != "not-found"' in content

