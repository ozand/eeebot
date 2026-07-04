#!/usr/bin/env python3
"""Thin wrapper — canonical implementation lives in nanobot/runtime/bridge.py.

Kept at this path so the systemd unit's ExecStart and deploy_release.sh's
file-copy-to-libexec step (see host/eeepc/scripts/deploy_release.sh) do not
need to change in this PR (#599). The file-copy deploy mechanism itself is
retired separately in #601.
"""
from nanobot.runtime.bridge import cli_main

if __name__ == '__main__':
    raise SystemExit(cli_main())
