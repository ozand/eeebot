"""Tests for host deployment script and systemd unit integrity (#1037)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    assert '"CRITICAL: $ghost_unit is still active after purge"' in content

    # #1236: a long-running dashboard must restart after current activation,
    # while rollback restores it before restarting the bridge.
    assert 'DASHBOARD_UNIT=eeebot-dashboard.service' in content
    assert 'sudo systemctl restart "$DASHBOARD_UNIT"' in content
    assert 'systemctl show "$DASHBOARD_UNIT" -p MainPID --value' in content
    assert 'readlink "/proc/$DASHBOARD_PID/cwd"' in content
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

