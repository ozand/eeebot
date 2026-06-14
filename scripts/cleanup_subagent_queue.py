#!/usr/bin/env python3
"""Archive stale subagent requests older than 24h.

Usage:
    python3 scripts/cleanup_subagent_queue.py [--hours N] [--dry-run] [--state-root PATH]

Archives requests from state/subagents/requests/ that are older than N hours
(default 24) into state/subagents/archive/. Updates state/current_health.json
with cleanup counts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(ts: str | None) -> datetime | None:
    """Best-effort ISO timestamp parsing."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive stale subagent requests")
    parser.add_argument("--hours", type=int, default=24, help="Age threshold in hours (default: 24)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be archived without moving files")
    parser.add_argument("--state-root", type=str, default=None, help="Override state root path")
    args = parser.parse_args()

    # Determine state root
    if args.state_root:
        state_root = Path(args.state_root)
    else:
        # Try repo-local state first, then system path
        repo_root = Path(__file__).resolve().parent.parent
        local_state = repo_root / "state"
        system_state = Path("/var/lib/eeepc-agent/self-evolving-agent/state")

        if local_state.exists():
            state_root = local_state
        elif system_state.exists():
            state_root = system_state
        else:
            print("ERROR: No state directory found", file=sys.stderr)
            return 1

    requests_dir = state_root / "subagents" / "requests"
    results_dir = state_root / "subagents" / "results"
    subagents_root = state_root / "subagents"
    archive_dir = state_root / "subagents" / "archive"
    health_path = state_root / "current_health.json"

    archive_dir.mkdir(parents=True, exist_ok=True)

    cutoff = _utc_now() - timedelta(hours=args.hours)
    archived = 0
    skipped = 0
    errors = 0

    # Collect files from requests, results and root of subagents directory
    targets: list[Path] = []

    if requests_dir.exists():
        targets.extend(requests_dir.glob("*.json"))
    if results_dir.exists():
        targets.extend(results_dir.glob("*.json"))
    if subagents_root.exists():
        # Only files directly in subagents_root, excluding directories
        for p in subagents_root.glob("*.json"):
            if p.is_file():
                targets.append(p)

    for req_path in sorted(targets, key=lambda x: x.name):
        try:
            payload = json.loads(req_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}

        # Determine age from file mtime since created_at is often null
        file_mtime = datetime.fromtimestamp(req_path.stat().st_mtime, tz=timezone.utc)

        if file_mtime < cutoff:
            if args.dry_run:
                print(f"  WOULD ARCHIVE: {req_path.name} (modified: {file_mtime.isoformat()})")
            else:
                dest = archive_dir / req_path.name
                try:
                    req_path.rename(dest)
                    archived += 1
                except OSError as e:
                    print(f"  ERROR archiving {req_path.name}: {e}", file=sys.stderr)
                    errors += 1
        else:
            skipped += 1

    # Update health file
    health = {}
    if health_path.exists():
        try:
            health = json.loads(health_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            health = {}

    health["last_subagent_cleanup_count"] = archived
    health["last_subagent_cleanup_timestamp"] = _utc_now().isoformat()
    health["subagent_cleanup_count"] = health.get("subagent_cleanup_count", 0) + archived
    health["subagent_archive_count"] = len(list(archive_dir.glob("*.json")))
    health["subagent_requests_remaining"] = len(list(requests_dir.glob("*.json"))) if requests_dir.exists() else 0

    if not args.dry_run:
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(
            json.dumps(health, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    print(f"Cleanup complete: {archived} archived, {skipped} kept, {errors} errors")
    if args.dry_run:
        print("(dry-run mode — no files were moved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
