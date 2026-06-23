#!/usr/bin/env python3
"""
smoke_test_loop.py - sanity check that key runtime files exist and are non-empty.

Usage:
    python3 scripts/smoke_test_loop.py [--state-root PATH] [--repo-root PATH]

Output:
    PASS: N/N checks
    or list of failures with exit code 1.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

DEFAULT_STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"
DEFAULT_REPO_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"


def run_checks(state_root: Path, repo_root: Path) -> tuple[int, list[str]]:
    """Run all smoke checks. Returns (total_checks, failures)."""
    failures: list[str] = []
    total = 0

    # 1. state/current_health.json exists and is a non-empty JSON object
    total += 1
    health = state_root / "current_health.json"
    if not health.exists():
        failures.append("state/current_health.json missing")
    else:
        try:
            d = json.loads(health.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or not d:
                failures.append("state/current_health.json is empty or not a JSON object")
        except Exception as e:
            failures.append(f"state/current_health.json unreadable: {e}")

    # 2. state/host_capabilities.json is a JSON object with at least 5 non-private keys
    total += 1
    caps = state_root / "host_capabilities.json"
    if not caps.exists():
        failures.append("state/host_capabilities.json missing")
    else:
        try:
            d = json.loads(caps.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                failures.append("state/host_capabilities.json is not a JSON object")
            else:
                keys = [k for k in d if not k.startswith("_")]
                if len(keys) < 5:
                    failures.append(
                        f"state/host_capabilities.json has only {len(keys)} keys (need >=5)"
                    )
        except Exception as e:
            failures.append(f"state/host_capabilities.json unreadable: {e}")

    # 3. memory/MEMORY.md has at least 10 lines
    total += 1
    mem = repo_root / "memory" / "MEMORY.md"
    if not mem.exists():
        failures.append("memory/MEMORY.md missing")
    else:
        try:
            lines = mem.read_text(encoding="utf-8").splitlines()
            if len(lines) < 10:
                failures.append(f"memory/MEMORY.md has only {len(lines)} lines (need >=10)")
        except Exception as e:
            failures.append(f"memory/MEMORY.md unreadable: {e}")

    # 4. at least 1 recorded cycle (cycle-*.json) in state/goals/history/
    # 5. and at least one such cycle within the last 2 hours (stall detection)
    total += 2
    hist_dir = state_root / "goals" / "history"
    if not hist_dir.exists():
        failures.append("state/goals/history/ missing")
    else:
        try:
            cycles = [p for p in hist_dir.glob("cycle-*.json") if p.is_file()]
            if not cycles:
                failures.append(
                    "state/goals/history/ has no cycle-*.json files -- no cycles recorded"
                )
            else:
                now = time.time()
                recent_cycles = [p for p in cycles if now - p.stat().st_mtime < 7200]
                if not recent_cycles:
                    failures.append("loop stalled: no cycle in last 2h")
        except Exception as e:
            failures.append(f"state/goals/history/ unreadable: {e}")

    return total, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the self-evolving loop")
    parser.add_argument("--state-root", default=DEFAULT_STATE_ROOT)
    parser.add_argument("--repo-root", default=DEFAULT_REPO_ROOT)
    args = parser.parse_args()

    total, failures = run_checks(Path(args.state_root), Path(args.repo_root))
    passed = total - len(failures)

    if not failures:
        print(f"PASS: {passed}/{total} checks")
        sys.exit(0)
    else:
        print(f"FAIL: {passed}/{total} checks")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
