#!/usr/bin/env python3
"""One-time migration: repair evolution tree parent links using git ancestry.

Usage:
    python3 scripts/migrate_evolution_tree.py [--state-dir PATH] [--repo PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from nanobot.runtime.evolution_tree import migrate_tree_ancestry

_DEFAULT_STATE_DIR = Path(
    os.environ.get("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state")
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair parent_sha links in tree.json using git ancestry."
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=_DEFAULT_STATE_DIR,
        help=f"State directory (default: {_DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Path to git repository (default: parent of state-dir / eeebot-self-evolving)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without modifying tree.json",
    )
    args = parser.parse_args()

    repo_root = args.repo or (args.state_dir.parent / "eeebot-self-evolving")
    result = migrate_tree_ancestry(args.state_dir, repo_root=repo_root, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
