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


# Metadata files in subagents root that are NOT queue items
_METADATA_FILES = {"archive_latest.json", "queue_index.json", "index.json"}


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
    parser.add_argument("--max-queue", type=int, default=9, help="Max remaining queue size; archive oldest to reach this (default: 9)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON to stdout")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent

    # Determine state root
    if args.state_root:
        state_root = Path(args.state_root)
    else:
        # Try repo-local state first, then system path
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
        # Only files directly in subagents_root, excluding directories and metadata
        for p in subagents_root.glob("*.json"):
            if p.is_file() and p.name not in _METADATA_FILES:
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

    # Count-based archival: if queue still exceeds --max-queue, archive oldest remaining
    if not args.dry_run:
        # Re-collect remaining files after age-based cleanup
        remaining: list[Path] = []
        if requests_dir.exists():
            remaining.extend(requests_dir.glob("*.json"))
        if results_dir.exists():
            remaining.extend(results_dir.glob("*.json"))
        if subagents_root.exists():
            for p in subagents_root.glob("*.json"):
                if p.is_file() and p.name not in _METADATA_FILES:
                    remaining.append(p)

        # Sort by mtime ascending (oldest first)
        remaining.sort(key=lambda x: x.stat().st_mtime)

        excess = len(remaining) - args.max_queue
        if excess > 0:
            print(f"Queue has {len(remaining)} files, archiving {excess} oldest to reach max-queue={args.max_queue}")
            for req_path in remaining[:excess]:
                dest = archive_dir / req_path.name
                try:
                    req_path.rename(dest)
                    archived += 1
                except OSError as e:
                    print(f"  ERROR archiving {req_path.name}: {e}", file=sys.stderr)
                    errors += 1
    else:
        # Dry-run: show what would be archived by count
        remaining: list[Path] = []
        if requests_dir.exists():
            remaining.extend(requests_dir.glob("*.json"))
        if results_dir.exists():
            remaining.extend(results_dir.glob("*.json"))
        if subagents_root.exists():
            for p in subagents_root.glob("*.json"):
                if p.is_file() and p.name not in _METADATA_FILES:
                    remaining.append(p)
        remaining.sort(key=lambda x: x.stat().st_mtime)
        excess = len(remaining) - args.max_queue
        if excess > 0:
            print(f"Queue has {len(remaining)} files, would archive {excess} oldest to reach max-queue={args.max_queue}")
            for req_path in remaining[:excess]:
                file_mtime = datetime.fromtimestamp(req_path.stat().st_mtime, tz=timezone.utc)
                print(f"  WOULD ARCHIVE (count): {req_path.name} (modified: {file_mtime.isoformat()})")

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
    # Record total remaining queue count (root + requests + results) for monitoring
    # Exclude metadata files from the count
    remaining_root = len([p for p in subagents_root.glob("*.json") if p.is_file() and p.name not in _METADATA_FILES]) if subagents_root.exists() else 0
    remaining_req = len(list(requests_dir.glob("*.json"))) if requests_dir.exists() else 0
    remaining_res = len(list(results_dir.glob("*.json"))) if results_dir.exists() else 0
    health["subagent_queue_count"] = remaining_root + remaining_req + remaining_res

    if not args.dry_run:
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(
            json.dumps(health, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    result = {
        "archived": archived,
        "kept": skipped,
        "errors": errors,
        "dry_run": args.dry_run,
        "subagent_queue_count": health.get("subagent_queue_count", 0),
        "subagent_archive_count": health.get("subagent_archive_count", 0),
        "timestamp": _utc_now().isoformat(),
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Cleanup complete: {archived} archived, {skipped} kept, {errors} errors")
        if args.dry_run:
            print("(dry-run mode — no files were moved)")
    # NOTE: Lessons compilation is now handled in real-time by the coordinator
    # (nanobot/runtime/lessons.py :: update_lessons_from_cycle) — no batch compile needed.
    return 0


def run_self_tests() -> int:
    """Self-test suite for cleanup_subagent_queue.py."""
    import shutil
    import tempfile
    import time

    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS: {name}")
        else:
            failed += 1
            print(f"  FAIL: {name} — {detail}")

    # --- Test _utc_now ---
    now = _utc_now()
    check("_utc_now returns datetime", isinstance(now, datetime))
    check("_utc_now is timezone-aware", now.tzinfo is not None)

    # --- Test _parse_timestamp ---
    ts = _parse_timestamp("2026-01-01T12:00:00Z")
    check("_parse_timestamp parses ISO with Z", ts is not None)
    check("_parse_timestamp Z gives correct year", ts is not None and ts.year == 2026)

    ts2 = _parse_timestamp("2026-01-01T12:00:00+00:00")
    check("_parse_timestamp parses ISO with offset", ts2 is not None)

    check("_parse_timestamp None returns None", _parse_timestamp(None) is None)
    check("_parse_timestamp empty returns None", _parse_timestamp("") is None)
    check("_parse_timestamp invalid returns None", _parse_timestamp("not-a-date") is None)

    # --- Test archival logic with temp directory ---
    tmpdir = tempfile.mkdtemp(prefix="cleanup_test_")
    try:
        state_root = Path(tmpdir)
        requests_dir = state_root / "subagents" / "requests"
        results_dir = state_root / "subagents" / "results"
        subagents_root = state_root / "subagents"
        archive_dir = state_root / "subagents" / "archive"
        health_path = state_root / "current_health.json"

        requests_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        # Create a stale request file (older than 24h)
        stale_file = requests_dir / "stale_req.json"
        stale_file.write_text(json.dumps({"request_id": "stale-1", "created_at": "2025-01-01T00:00:00Z"}))
        # Set mtime to 48 hours ago
        old_time = time.time() - (48 * 3600)
        os.utime(stale_file, (old_time, old_time))

        # Create a fresh request file
        fresh_file = requests_dir / "fresh_req.json"
        fresh_file.write_text(json.dumps({"request_id": "fresh-1", "created_at": _utc_now().isoformat()}))

        # Create a stale result file
        stale_result = results_dir / "stale_result.json"
        stale_result.write_text(json.dumps({"result_id": "stale-r1"}))
        os.utime(stale_result, (old_time, old_time))

        # Create a file in subagents root
        stale_root = subagents_root / "stale_root.json"
        stale_root.write_text(json.dumps({"id": "root-stale"}))
        os.utime(stale_root, (old_time, old_time))

        # Run cleanup with --hours 24
        import sys
        old_argv = sys.argv
        sys.argv = ["cleanup_subagent_queue.py", "--hours", "24", "--state-root", tmpdir]
        try:
            rc = main()
        finally:
            sys.argv = old_argv

        check("main returns 0", rc == 0)
        check("stale request archived", not stale_file.exists())
        check("stale request in archive", (archive_dir / "stale_req.json").exists())
        check("fresh request kept", fresh_file.exists())
        check("stale result archived", not stale_result.exists())
        check("stale result in archive", (archive_dir / "stale_result.json").exists())
        check("stale root file archived", not stale_root.exists())
        check("stale root in archive", (archive_dir / "stale_root.json").exists())

        # Check health file was written
        check("health file exists", health_path.exists())
        if health_path.exists():
            health = json.loads(health_path.read_text())
            check("health has cleanup count", "last_subagent_cleanup_count" in health)
            check("health cleanup count is 3", health.get("last_subagent_cleanup_count") == 3)
            check("health has timestamp", "last_subagent_cleanup_timestamp" in health)
            check("health has archive count", "subagent_archive_count" in health)
            check("health has remaining count", "subagent_requests_remaining" in health)
            check("health has queue_count", "subagent_queue_count" in health)
            check("health queue_count is 1", health.get("subagent_queue_count") == 1)  # only fresh_req.json remains

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # --- Test dry-run mode ---
    tmpdir2 = tempfile.mkdtemp(prefix="cleanup_dryrun_")
    try:
        state_root2 = Path(tmpdir2)
        requests_dir2 = state_root2 / "subagents" / "requests"
        requests_dir2.mkdir(parents=True, exist_ok=True)

        dry_stale = requests_dir2 / "dry_stale.json"
        dry_stale.write_text(json.dumps({"id": "dry-1"}))
        old_time2 = time.time() - (48 * 3600)
        os.utime(dry_stale, (old_time2, old_time2))

        old_argv = sys.argv
        sys.argv = ["cleanup_subagent_queue.py", "--hours", "24", "--dry-run", "--state-root", tmpdir2]
        try:
            rc = main()
        finally:
            sys.argv = old_argv

        check("dry-run returns 0", rc == 0)
        check("dry-run keeps file in place", dry_stale.exists())
        check("dry-run does not create archive", not (state_root2 / "subagents" / "archive").exists() or
              len(list((state_root2 / "subagents" / "archive").glob("*.json"))) == 0)
    finally:
        shutil.rmtree(tmpdir2, ignore_errors=True)

    # --- Test empty state directory ---
    tmpdir3 = tempfile.mkdtemp(prefix="cleanup_empty_")
    try:
        state_root3 = Path(tmpdir3)
        # No subagents directory at all
        old_argv = sys.argv
        sys.argv = ["cleanup_subagent_queue.py", "--hours", "24", "--state-root", tmpdir3]
        try:
            rc = main()
        finally:
            sys.argv = old_argv

        check("empty state returns 0", rc == 0)
        check("empty state creates health file", (state_root3 / "current_health.json").exists())
    finally:
        shutil.rmtree(tmpdir3, ignore_errors=True)

    # --- Test --json output mode ---
    tmpdir4 = tempfile.mkdtemp(prefix="cleanup_json_")
    try:
        state_root4 = Path(tmpdir4)
        requests_dir4 = state_root4 / "subagents" / "requests"
        requests_dir4.mkdir(parents=True, exist_ok=True)

        json_stale = requests_dir4 / "json_stale.json"
        json_stale.write_text(json.dumps({"id": "json-1"}))
        old_time4 = time.time() - (48 * 3600)
        os.utime(json_stale, (old_time4, old_time4))

        old_argv = sys.argv
        sys.argv = ["cleanup_subagent_queue.py", "--hours", "24", "--json", "--state-root", tmpdir4]
        try:
            rc = main()
        finally:
            sys.argv = old_argv

        check("json mode returns 0", rc == 0)
        check("json mode archives stale file", not json_stale.exists())
    finally:
        shutil.rmtree(tmpdir4, ignore_errors=True)

    # --- Test count-based archival (exceeds max-queue) ---
    tmpdir5 = tempfile.mkdtemp(prefix="cleanup_count_")
    try:
        state_root5 = Path(tmpdir5)
        requests_dir5 = state_root5 / "subagents" / "requests"
        requests_dir5.mkdir(parents=True, exist_ok=True)

        # Create 5 fresh files (all recent, so age-based won't touch them)
        for i in range(5):
            f = requests_dir5 / f"count_{i}.json"
            f.write_text(json.dumps({"id": f"count-{i}"}))

        old_argv = sys.argv
        sys.argv = ["cleanup_subagent_queue.py", "--hours", "24", "--max-queue", "2", "--state-root", tmpdir5]
        try:
            rc = main()
        finally:
            sys.argv = old_argv

        check("count-based returns 0", rc == 0)
        remaining = len(list(requests_dir5.glob("*.json")))
        check("count-based reduces to max-queue", remaining == 2)
        archived_count = len(list((state_root5 / "subagents" / "archive").glob("*.json")))
        check("count-based archives excess", archived_count == 3)
    finally:
        shutil.rmtree(tmpdir5, ignore_errors=True)

    print(f"\nSelf-tests: {passed} passed, {failed} failed out of {passed + failed}")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--test":
        raise SystemExit(run_self_tests())
    raise SystemExit(main())
