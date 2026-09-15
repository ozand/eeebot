#!/usr/bin/env python3
"""Run bounded local CI suite on eeepc host and record state (#1593)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nanobot.runtime.local_ci import run_and_record_local_ci


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded local CI suite.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"),
        help="Path to the instance git repository to test.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("/var/lib/eeepc-agent/self-evolving-agent/state"),
        help="Path to the runtime state root.",
    )
    parser.add_argument(
        "--pytest-bin",
        type=str,
        default="/opt/eeepc-agent/venv/bin/python",
        help="Python binary to execute pytest with.",
    )
    parser.add_argument(
        "--target",
        dest="targets",
        action="append",
        default=None,
        help="Optional test file target override (can be repeated).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_and_record_local_ci(
        workspace=args.workspace,
        state_dir=args.state_dir,
        pytest_bin=args.pytest_bin,
        test_targets=args.targets,
    )
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
