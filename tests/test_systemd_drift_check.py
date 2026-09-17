"""#1701 Part 2, increment 2: installed eeebot/eeepc systemd units and
drop-ins must be compared against the release tree, so a drop-in added on
the host outside the deploy (#1663: the crash recorder's ExecStopPost=,
invisible for 14 days) becomes a named finding instead of silence.

No manufactured host failure: these drive the pure comparison function
against fixture trees built in tmp_path, never the real host.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "host" / "eeepc" / "scripts" / "systemd_drift_check.py"
_spec = importlib.util.spec_from_file_location("systemd_drift_check", _SCRIPT)
assert _spec and _spec.loader
sdc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sdc)


def _write(path: Path, content: str = "placeholder\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_no_drift_when_installed_matches_release(tmp_path) -> None:
    installed = tmp_path / "etc-systemd-system"
    release = tmp_path / "release-systemd"
    _write(installed / "eeebot-reflector.service", "unit A\n")
    _write(release / "eeebot-reflector.service", "unit A\n")
    _write(installed / "eeepc-self-evolving-subagent-bridge.service.d" / "override.conf", "[Service]\nFoo=bar\n")
    _write(release / "drop-ins" / "eeepc-self-evolving-subagent-bridge.service.d" / "override.conf", "[Service]\nFoo=bar\n")

    result = sdc.compare_systemd_drift(installed, release)
    assert result["state"] == "present"
    assert result["defect_count"] == 0
    assert result["findings"] == {
        "installed_not_in_release": [],
        "release_not_installed": [],
        "content_differs": [],
        "stray": [],
    }


def test_unknown_owner_installed_not_in_release_counts_as_defect(tmp_path) -> None:
    installed = tmp_path / "etc-systemd-system"
    release = tmp_path / "release-systemd"
    release.mkdir(parents=True)
    _write(installed / "eeepc-mystery-thing.service", "mystery\n")

    result = sdc.compare_systemd_drift(installed, release)
    assert result["defect_count"] == 1
    assert result["findings"]["installed_not_in_release"] == [
        {"path": "eeepc-mystery-thing.service", "owner": "unknown"},
    ]


def test_known_owner_installed_not_in_release_is_not_a_defect(tmp_path) -> None:
    """The five drop-ins named in #1701 PR #1714 are expected absent from
    THIS repo's release tree (dashboard- or preset-owned) -- a finding for
    audit, never counted toward defect_count."""
    installed = tmp_path / "etc-systemd-system"
    release = tmp_path / "release-systemd"
    release.mkdir(parents=True)
    _write(
        installed / "eeepc-self-evolving-subagent-bridge.timer.d" / "preset.conf",
        "[Timer]\nOnUnitActiveSec=3m\n",
    )

    result = sdc.compare_systemd_drift(installed, release)
    assert result["defect_count"] == 0
    assert result["findings"]["installed_not_in_release"] == [
        {"path": "eeepc-self-evolving-subagent-bridge.timer.d/preset.conf", "owner": "preset"},
    ]


def test_release_not_installed_is_always_a_defect(tmp_path) -> None:
    """The release ships a unit the host does not have -- the deploy did
    not run, or something removed it after. Always a defect, unlike the
    reverse direction, which has known exceptions."""
    installed = tmp_path / "etc-systemd-system"
    release = tmp_path / "release-systemd"
    installed.mkdir(parents=True)
    _write(release / "eeebot-strategist.timer", "timer\n")

    result = sdc.compare_systemd_drift(installed, release)
    assert result["defect_count"] == 1
    assert result["findings"]["release_not_installed"] == ["eeebot-strategist.timer"]


def test_content_differs_flagged(tmp_path) -> None:
    installed = tmp_path / "etc-systemd-system"
    release = tmp_path / "release-systemd"
    _write(installed / "eeebot-host-metrics.service", "installed version\n")
    _write(release / "eeebot-host-metrics.service", "release version\n")

    result = sdc.compare_systemd_drift(installed, release)
    assert result["defect_count"] == 1
    assert result["findings"]["content_differs"] == [
        {"path": "eeebot-host-metrics.service", "detail": "content differs from release"},
    ]
    # Not double-counted anywhere else.
    assert result["findings"]["installed_not_in_release"] == []
    assert result["findings"]["release_not_installed"] == []


def test_stray_bak_files_flagged(tmp_path) -> None:
    """#1701: three `.bak-20260817-880` files left in /etc/systemd/system
    that systemd ignores (not a valid unit suffix) but a reader must not."""
    installed = tmp_path / "etc-systemd-system"
    release = tmp_path / "release-systemd"
    installed.mkdir(parents=True)
    release.mkdir(parents=True)
    _write(installed / "eeepc-monitor.service.bak-20260817-880", "old\n")

    result = sdc.compare_systemd_drift(installed, release)
    assert result["defect_count"] == 1
    assert result["findings"]["stray"] == ["eeepc-monitor.service.bak-20260817-880"]


def test_dropin_directory_not_confused_with_unit_file(tmp_path) -> None:
    """A `<unit>.service.d` directory must never match the `*.service`
    glob (it would read its own drop-in directory as a bogus 'unit')."""
    installed = tmp_path / "etc-systemd-system"
    release = tmp_path / "release-systemd"
    _write(installed / "eeebot-reflector.service.d" / "extra.conf", "[Service]\n")
    release.mkdir(parents=True)

    result = sdc.compare_systemd_drift(installed, release)
    # The drop-in itself is a legitimate finding (unknown owner); the
    # directory `eeebot-reflector.service.d` must NOT also appear as if it
    # were a unit file.
    paths = {f["path"] for f in result["findings"]["installed_not_in_release"]}
    assert paths == {"eeebot-reflector.service.d/extra.conf"}


def test_five_known_drop_ins_ownership_map_matches_1714(tmp_path) -> None:
    """Locks the ownership map itself against silent drift -- if this
    changes, it must be a reviewed edit, not an accident."""
    assert sdc.KNOWN_OWNERS == {
        "eeepc-self-evolving-subagent-bridge.timer.d/preset.conf": "preset",
        "eeepc-self-evolving-subagent-bridge.service.d/20-techtree-publish.conf": "dashboard",
        "eeebot-techtree-publish.service.d/sync.conf": "dashboard",
        "eeebot-techtree-publish.service": "dashboard",
        "eeebot-techtree-publish.service.d/10-state-read.conf": "unknown",
    }


def test_main_writes_json_result(tmp_path) -> None:
    installed = tmp_path / "etc-systemd-system"
    release = tmp_path / "release-systemd"
    state_dir = tmp_path / "state"
    _write(installed / "eeebot-reflector.service", "same\n")
    _write(release / "eeebot-reflector.service", "same\n")

    rc = sdc.main([
        "--installed-dir", str(installed),
        "--release-dir", str(release),
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    written = json.loads((state_dir / "systemd_drift.json").read_text(encoding="utf-8"))
    assert written["state"] == "present"
    assert written["defect_count"] == 0
    assert "scanned_at" in written


def test_main_is_fail_open_on_unexpected_comparison_error(tmp_path, monkeypatch) -> None:
    """A broken host read must be visible as probe_unavailable, never a
    crashed timer and never a silent '0 findings'."""
    state_dir = tmp_path / "state"

    def _boom(installed_dir, release_dir):
        raise PermissionError("no access")

    monkeypatch.setattr(sdc, "compare_systemd_drift", _boom)
    rc = sdc.main([
        "--installed-dir", str(tmp_path / "installed"),
        "--release-dir", str(tmp_path / "release"),
        "--state-dir", str(state_dir),
    ])
    assert rc == 0  # fail-open: the finding is the state file, not the exit code
    written = json.loads((state_dir / "systemd_drift.json").read_text(encoding="utf-8"))
    assert written["state"] == "probe_unavailable"
    assert "PermissionError" in written["details"]
