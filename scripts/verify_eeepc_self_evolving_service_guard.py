#!/usr/bin/env python3
"""Guard eeepc self-evolving systemd service against stale release pins.

Usage:
    systemctl cat eeepc-self-evolving-agent.service | \
        scripts/verify_eeepc_self_evolving_service_guard.py

The guard is intentionally text-based so it can run during deploy/verify without
requiring dbus or root privileges. It fails when the effective unit pins a stale
release path instead of following the `/current` symlink.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

CURRENT_ROOT = '/opt/eeepc-agent/runtimes/self-evolving-agent/current'
RELEASE_ROOT_RE = re.compile(r'/opt/eeepc-agent/runtimes/self-evolving-agent/releases/[^\s:"\']+')


def _last_assignment(text: str, key: str) -> str | None:
    value: str | None = None
    prefix = f'{key}='
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
    return value


def _environment_values(text: str, name: str) -> list[str]:
    values: list[str] = []
    needle = f'{name}='
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith('Environment='):
            continue
        payload = line[len('Environment='):]
        for token in payload.split():
            if token.startswith(needle):
                values.append(token[len(needle):].strip().strip('"'))
    return values


def evaluate_service_text(text: str) -> dict[str, Any]:
    reasons: list[str] = []
    working_directory = _last_assignment(text, 'WorkingDirectory')
    exec_start = _last_assignment(text, 'ExecStart')
    policy_values = _environment_values(text, 'POLICY_FILE')
    policy_file = policy_values[-1] if policy_values else None

    if working_directory != CURRENT_ROOT:
        reasons.append('working_directory_not_current')
    if working_directory and RELEASE_ROOT_RE.search(working_directory):
        reasons.append('working_directory_pinned_to_release')

    if policy_file and RELEASE_ROOT_RE.search(policy_file):
        reasons.append('policy_file_pinned_to_release')
    if exec_start and RELEASE_ROOT_RE.search(exec_start):
        reasons.append('exec_start_pinned_to_release')
    if exec_start and CURRENT_ROOT not in exec_start:
        reasons.append('exec_start_not_current')

    # Preserve stable order while removing duplicates.
    ordered_reasons = list(dict.fromkeys(reasons))
    return {
        'schema_version': 'eeepc-self-evolving-service-guard-v1',
        'state': 'healthy' if not ordered_reasons else 'blocked',
        'reasons': ordered_reasons,
        'working_directory': working_directory,
        'policy_file': policy_file,
        'exec_start': exec_start,
    }


def main() -> int:
    result = evaluate_service_text(sys.stdin.read())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['state'] == 'healthy' else 2


if __name__ == '__main__':
    raise SystemExit(main())
