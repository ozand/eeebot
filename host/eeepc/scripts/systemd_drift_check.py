#!/usr/bin/env python3
"""Compare installed eeebot/eeepc systemd units and drop-ins against the
release tree (#1701 Part 2, increment 2).

`deploy_release.sh` installs `host/eeepc/systemd/*.service`, `*.timer` and
`drop-ins/*.d/*.conf` on every deploy, but nothing checks afterwards that
the installed set still matches what the release shipped, or that nothing
was added by hand outside the deploy. #1663 found the crash recorder's
`ExecStopPost=` had lived in a drop-in the deploy skipped for 14 days --
installed nowhere, invisible everywhere, until someone happened to read
`systemctl cat`. #1701 measured five drop-ins on the host, four of which
exist in no repository this deploy reads, and a sixth: `.bak-*` backup
files systemd ignores that a reader does not.

A file present on the host and absent from THIS repo's release tree is
not automatically a defect -- three of those five drop-ins are tracked in
`ozand/eeebot-ops-dashboard` (the publisher's own units), and one
(`preset.conf`) is generated at runtime by `apply_preset.sh`, never a
static file. `KNOWN_OWNERS` below names those so they read as accounted
for, not as unknowns needing daily re-investigation; anything not in that
map is `owner: unknown` and counts toward `defect_count`.

Four-state discipline: `compare_systemd_drift` itself never raises --
an installed_dir or release_dir that doesn't exist yet just yields empty
sets (a legitimate result: everything the other side has is a finding,
not an error). `main()` wraps the whole comparison and downgrades to
`probe_unavailable` only if something unexpected still escapes it (e.g. a
permission error `glob()` itself cannot swallow), so a broken host never
silently reports "0 findings".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# #1701 PR #1714 measured these on the host (2026-09-17, read-only ssh).
# Keys are the path exactly as it would appear under `installed_not_in_release`
# below: "<unit>.service"/"<unit>.timer" for a whole unit, or
# "<unit>.service.d/<name>.conf" / "<unit>.timer.d/<name>.conf" for a drop-in.
KNOWN_OWNERS: dict[str, str] = {
    # Generated per-deploy-preset by apply_preset.sh; a static copy here
    # would silently reset whatever cadence the operator chose.
    "eeepc-self-evolving-subagent-bridge.timer.d/preset.conf": "preset",
    # Tracked in ozand/eeebot-ops-dashboard, installed by that repo's own
    # installer -- see #1701 PR #1714 for the exact paths.
    "eeepc-self-evolving-subagent-bridge.service.d/20-techtree-publish.conf": "dashboard",
    "eeebot-techtree-publish.service.d/sync.conf": "dashboard",
    "eeebot-techtree-publish.service": "dashboard",
    # In no repository as of #1701; recorded verbatim in PR #1714 for the
    # dashboard repo to adopt. Explicit "unknown" (not just the default)
    # so removing this line is a visible decision, not silence.
    "eeebot-techtree-publish.service.d/10-state-read.conf": "unknown",
}

_UNIT_PREFIXES = ("eeebot-", "eeepc-")
_UNIT_SUFFIXES = (".service", ".timer")

DEFAULT_INSTALLED_DIR = Path("/etc/systemd/system")
DEFAULT_RELEASE_DIR = Path(
    "/opt/eeepc-agent/runtimes/self-evolving-agent/current/host/eeepc/systemd"
)
DEFAULT_STATE_DIR = Path("/var/lib/eeepc-agent/self-evolving-agent/state")


def _iter_units(root: Path) -> dict[str, Path]:
    """Top-level eeebot-/eeepc- .service/.timer files directly under
    `root` -- never descending into `*.d` drop-in directories (a
    directory named `foo.service.d` does not match the `*.service`
    glob, since the pattern must match the whole name)."""
    found: dict[str, Path] = {}
    for prefix in _UNIT_PREFIXES:
        for suffix in _UNIT_SUFFIXES:
            for path in root.glob(f"{prefix}*{suffix}"):
                if path.is_file():
                    found[path.name] = path
    return found


def _iter_dropins(root: Path) -> dict[str, Path]:
    """`<unit>.service.d/*.conf` and `<unit>.timer.d/*.conf` directly
    under `root`, for eeebot-/eeepc- units only."""
    found: dict[str, Path] = {}
    for prefix in _UNIT_PREFIXES:
        for suffix in _UNIT_SUFFIXES:
            for conf in root.glob(f"{prefix}*{suffix}.d/*.conf"):
                if conf.is_file():
                    found[f"{conf.parent.name}/{conf.name}"] = conf
    return found


def _iter_strays(installed_dir: Path) -> list[str]:
    """`.bak*` files directly under installed_dir. systemd ignores them
    (they aren't valid unit suffixes); a drift reader must not."""
    return sorted(p.name for p in installed_dir.glob("*.bak*") if p.is_file())


def compare_systemd_drift(installed_dir: Path, release_dir: Path) -> dict:
    """Pure comparison -- fixture-testable, no host access, no systemctl.

    `installed_dir` mirrors `/etc/systemd/system`: unit files at its top
    level plus `<unit>.{service,timer}.d/*.conf` drop-in directories.
    `release_dir` mirrors the release tree's `host/eeepc/systemd/`: unit
    files at its top level plus `drop-ins/<unit>.{service,timer}.d/*.conf`.

    Returns ``{"state": "present", "findings": {...}, "defect_count": N}``.
    ``findings`` has four buckets:
    - ``installed_not_in_release``: on the host, not in this release tree.
      Each entry carries ``owner`` from `KNOWN_OWNERS` (default
      ``"unknown"``); only ``owner: "unknown"`` entries count toward
      `defect_count` -- a dashboard- or preset-owned file living
      elsewhere is expected, not a defect.
    - ``release_not_installed``: this release ships it, the host doesn't
      have it -- always a defect (the deploy didn't run, or something
      removed it after).
    - ``content_differs``: same path both sides, different bytes.
    - ``stray``: `.bak*` files under `installed_dir`.
    """
    installed = {**_iter_units(installed_dir), **_iter_dropins(installed_dir)}
    release = {
        **_iter_units(release_dir),
        **_iter_dropins(release_dir / "drop-ins"),
    }

    installed_not_in_release: list[dict[str, str]] = []
    content_differs: list[dict[str, str]] = []
    for rel, path in sorted(installed.items()):
        if rel not in release:
            installed_not_in_release.append({
                "path": rel,
                "owner": KNOWN_OWNERS.get(rel, "unknown"),
            })
            continue
        try:
            same = path.read_bytes() == release[rel].read_bytes()
        except OSError as exc:
            content_differs.append({"path": rel, "detail": f"unreadable: {type(exc).__name__}"})
            continue
        if not same:
            content_differs.append({"path": rel, "detail": "content differs from release"})

    release_not_installed = sorted(rel for rel in release if rel not in installed)
    stray = _iter_strays(installed_dir)

    defect_count = (
        sum(1 for f in installed_not_in_release if f["owner"] == "unknown")
        + len(release_not_installed)
        + len(content_differs)
        + len(stray)
    )

    return {
        "state": "present",
        "findings": {
            "installed_not_in_release": installed_not_in_release,
            "release_not_installed": release_not_installed,
            "content_differs": content_differs,
            "stray": stray,
        },
        "defect_count": defect_count,
    }


def _default_state_dir() -> Path:
    env = os.environ.get("EEEBOT_STATE_DIR")
    return Path(env) if env else DEFAULT_STATE_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-dir", type=Path, default=DEFAULT_INSTALLED_DIR)
    parser.add_argument("--release-dir", type=Path, default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--state-dir", type=Path, default=_default_state_dir())
    parser.add_argument(
        "--out", type=Path, default=None,
        help="override the output path (default: <state-dir>/systemd_drift.json)",
    )
    args = parser.parse_args(argv)

    try:
        result = compare_systemd_drift(args.installed_dir, args.release_dir)
    except Exception as exc:  # fail-open: a broken read must be visible, not fatal
        result = {
            "state": "probe_unavailable",
            "details": f"drift comparison failed: {type(exc).__name__}: {exc}",
        }

    result["scanned_at"] = datetime.now(timezone.utc).isoformat()
    result["installed_dir"] = str(args.installed_dir)
    result["release_dir"] = str(args.release_dir)

    out_path = args.out or (args.state_dir / "systemd_drift.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    defect_count = result.get("defect_count", 0)
    print(f"systemd-drift: {result['state']}, {defect_count} defect(s), wrote {out_path}")
    return 0  # informational probe: the finding is the signal, not the exit code


if __name__ == "__main__":
    sys.exit(main())
