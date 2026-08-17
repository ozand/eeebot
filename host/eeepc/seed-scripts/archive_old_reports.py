#!/usr/bin/env python3
"""archive_old_reports.py — Archive old cycle reports.

Restored 2026-08-17 (#884): this script is under a held-out behavioral
contract (heldout/checkers.py::check_archive_old_reports); the decay lane had
disabled it, which kept held-out RED and made #875 auto-promotion inert. It
is now protected from decay and executes normally.

Moves state/reports/*.json older than 30 days into monthly tar.gz archives
under state/reports/archive/, reducing inode and disk usage on the eeepc.

Usage:
    python3 scripts/archive_old_reports.py [--state-root PATH] [--apply] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def parse_file_date(path: Path) -> datetime:
    """Parse date from filename or fall back to mtime."""
    # Format: evolution-20260415T123000Z-cycle-0061eca4679d.json or proof-20260625T035232Z.json
    match = re.search(r"-(\d{4})(\d{2})(\d{2})T", path.name)
    if match:
        try:
            year, month, day = map(int, match.groups())
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            pass
    # Fallback to mtime
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

def archive_files(archive_path: Path, files: list[Path], state_root: Path) -> bool:
    """Add files to a tar.gz archive. If the archive exists, merge them."""
    import tempfile
    import shutil
    
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write to a temp file first to avoid corruption
    fd, temp_archive_path_str = tempfile.mkstemp(suffix=".tar.gz", dir=str(archive_path.parent))
    os.close(fd)
    temp_archive_path = Path(temp_archive_path_str)
    
    try:
        with tarfile.open(temp_archive_path, "w:gz") as tar_out:
            # If existing archive exists, copy its contents first
            if archive_path.exists() and archive_path.stat().st_size > 0:
                with tarfile.open(archive_path, "r:gz") as tar_in:
                    for member in tar_in.getmembers():
                        f_obj = tar_in.extractfile(member)
                        if f_obj is not None:
                            tar_out.addfile(member, f_obj)
                        else:
                            tar_out.addfile(member)
            
            # Add new files
            for f in files:
                try:
                    rel_path = f.relative_to(state_root)
                except ValueError:
                    rel_path = f.name
                tar_out.add(f, arcname=str(rel_path))
                
        # Replace old archive with new one
        if archive_path.exists():
            archive_path.unlink()
        shutil.move(str(temp_archive_path), str(archive_path))
        return True
    except Exception as e:
        if temp_archive_path.exists():
            temp_archive_path.unlink()
        print(f"ERROR archiving files: {e}", file=sys.stderr)
        return False

def main(bypass_deprecation: bool = False) -> int:
    parser = argparse.ArgumentParser(description="Archive old cycle reports")
    parser.add_argument("--state-root", type=str, default=None, help="Override state root path")
    parser.add_argument("--apply", action="store_true", help="Actually archive and delete files (default is dry-run)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON to stdout")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent

    # Determine state root
    if args.state_root:
        state_root = Path(args.state_root)
    else:
        local_state = repo_root / "state"
        system_state = Path("/var/lib/eeepc-agent/self-evolving-agent/state")
        if local_state.exists():
            state_root = local_state
        elif system_state.exists():
            state_root = system_state
        else:
            print("ERROR: No state directory found", file=sys.stderr)
            return 1

    reports_dir = state_root / "reports"
    if not reports_dir.exists() or not reports_dir.is_dir():
        if args.json:
            print(json.dumps({"error": "Reports directory does not exist", "path": str(reports_dir)}))
        else:
            print(f"Reports directory does not exist: {reports_dir}", file=sys.stderr)
        return 0

    now = _utc_now()
    cutoff = now - timedelta(days=30)

    # Find all .json files in reports_dir (not recursively, to avoid archive/ directory)
    json_files = [p for p in reports_dir.iterdir() if p.is_file() and p.suffix == ".json"]

    to_archive: dict[str, list[Path]] = {}
    total_files_found = len(json_files)
    total_files_to_archive = 0

    for f in json_files:
        file_date = parse_file_date(f)
        if file_date < cutoff:
            year_month = file_date.strftime("%Y-%m")
            to_archive.setdefault(year_month, []).append(f)
            total_files_to_archive += 1

    archive_results = {}
    errors = 0
    deleted_count = 0
    archived_count = 0

    # Sort keys to process chronologically
    for ym in sorted(to_archive.keys()):
        files = to_archive[ym]
        archive_name = f"reports_{ym}.tar.gz"
        archive_path = reports_dir / "archive" / archive_name
        
        if not args.apply:
            archive_results[ym] = {
                "archive_name": archive_name,
                "file_count": len(files),
                "status": "would_archive"
            }
            archived_count += len(files)
        else:
            success = archive_files(archive_path, files, state_root)
            if success:
                # Delete original files
                month_deleted = 0
                for f in files:
                    try:
                        f.unlink()
                        month_deleted += 1
                        deleted_count += 1
                    except OSError as e:
                        print(f"ERROR deleting {f}: {e}", file=sys.stderr)
                        errors += 1
                
                archive_results[ym] = {
                    "archive_name": archive_name,
                    "file_count": len(files),
                    "deleted_count": month_deleted,
                    "status": "archived",
                    "archive_size_bytes": archive_path.stat().st_size
                }
                archived_count += len(files)
            else:
                archive_results[ym] = {
                    "archive_name": archive_name,
                    "file_count": len(files),
                    "status": "failed"
                }
                errors += len(files)

    # Update health file if we actually applied changes
    if args.apply and deleted_count > 0:
        health_path = state_root / "current_health.json"
        health = {}
        if health_path.exists():
            try:
                health = json.loads(health_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                health = {}

        health["last_reports_archive_timestamp"] = now.isoformat()
        health["last_reports_archive_files_archived"] = archived_count
        health["last_reports_archive_files_deleted"] = deleted_count

        try:
            health_path.parent.mkdir(parents=True, exist_ok=True)
            health_path.write_text(
                json.dumps(health, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            print(f"ERROR updating health file: {e}", file=sys.stderr)

    result = {
        "total_files_found": total_files_found,
        "total_files_to_archive": total_files_to_archive,
        "archived_count": archived_count,
        "deleted_count": deleted_count,
        "errors": errors,
        "apply": args.apply,
        "archives": archive_results,
        "timestamp": now.isoformat(),
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if not args.apply:
            print(f"Dry-run: Would archive {total_files_to_archive} of {total_files_found} files into {len(to_archive)} monthly archives.")
            for ym, res in archive_results.items():
                print(f"  {ym}: {res['file_count']} files -> {res['archive_name']}")
        else:
            print(f"Archived {archived_count} files. Deleted {deleted_count} files. Errors: {errors}")
            for ym, res in archive_results.items():
                print(f"  {ym}: {res['status']} {res['file_count']} files -> {res['archive_name']} ({res.get('archive_size_bytes', 0)} bytes)")

    return 1 if errors > 0 else 0

def run_self_tests() -> int:
    """Self-test suite for archive_old_reports.py."""
    import tempfile
    import shutil
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

    # --- Test parse_file_date ---
    tmpdir = tempfile.mkdtemp(prefix="archive_reports_test_")
    try:
        dir_path = Path(tmpdir)
        
        # Test parsing from filename
        f1 = dir_path / "evolution-20260415T123000Z-cycle-0061eca4679d.json"
        f1.write_text("test")
        d1 = parse_file_date(f1)
        check("parse_file_date from filename year", d1.year == 2026)
        check("parse_file_date from filename month", d1.month == 4)
        check("parse_file_date from filename day", d1.day == 15)
        
        # Test fallback to mtime
        f2 = dir_path / "other_file.json"
        f2.write_text("test")
        # Set mtime to a specific date (e.g. 2025-05-10)
        dt = datetime(2025, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        mtime = dt.timestamp()
        os.utime(f2, (mtime, mtime))
        d2 = parse_file_date(f2)
        check("parse_file_date fallback to mtime year", d2.year == 2025)
        check("parse_file_date fallback to mtime month", d2.month == 5)
        check("parse_file_date fallback to mtime day", d2.day == 10)
        
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # --- Test archive_files (new and merge) ---
    tmpdir2 = tempfile.mkdtemp(prefix="archive_reports_test2_")
    try:
        state_root = Path(tmpdir2)
        reports_dir = state_root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        f1 = reports_dir / "evolution-1.json"
        f1.write_text("report 1")
        f2 = reports_dir / "evolution-2.json"
        f2.write_text("report 2")
        
        archive_path = reports_dir / "archive" / "reports_2026-04.tar.gz"
        
        # Create new archive
        success = archive_files(archive_path, [f1], state_root)
        check("archive_files create new returns True", success)
        check("archive file exists", archive_path.exists())
        
        # Verify contents
        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()
            check("archive contains f1", "reports/evolution-1.json" in names)
            check("archive does not contain f2", "reports/evolution-2.json" not in names)
            
        # Merge into existing archive
        success2 = archive_files(archive_path, [f2], state_root)
        check("archive_files merge returns True", success2)
        
        # Verify merged contents
        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()
            check("merged archive contains f1", "reports/evolution-1.json" in names)
            check("merged archive contains f2", "reports/evolution-2.json" in names)
            
    finally:
        shutil.rmtree(tmpdir2, ignore_errors=True)

    # --- Test main execution (dry-run and apply) ---
    tmpdir3 = tempfile.mkdtemp(prefix="archive_reports_test3_")
    try:
        state_root = Path(tmpdir3)
        reports_dir = state_root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        # Create some old files and some new files
        # Old file 1: 2026-04-15 (older than 30 days relative to now)
        f_old1 = reports_dir / "evolution-20260415T123000Z-cycle-1.json"
        f_old1.write_text("old 1")
        
        # Old file 2: 2026-05-20
        f_old2 = reports_dir / "evolution-20260520T123000Z-cycle-2.json"
        f_old2.write_text("old 2")
        
        # New file: current time
        now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        f_new = reports_dir / f"evolution-{now_str}-cycle-3.json"
        f_new.write_text("new")
        
        # Run main in dry-run mode
        import sys
        old_argv = sys.argv
        sys.argv = ["archive_old_reports.py", "--state-root", tmpdir3]
        try:
            rc = main(bypass_deprecation=True)
        finally:
            sys.argv = old_argv
            
        check("dry-run main returns 0", rc == 0)
        check("dry-run does not delete old 1", f_old1.exists())
        check("dry-run does not delete old 2", f_old2.exists())
        check("dry-run does not delete new", f_new.exists())
        check("dry-run does not create archive dir", not (reports_dir / "archive").exists())
        
        # Run main in apply mode
        sys.argv = ["archive_old_reports.py", "--state-root", tmpdir3, "--apply"]
        try:
            rc = main(bypass_deprecation=True)
        finally:
            sys.argv = old_argv
            
        check("apply main returns 0", rc == 0)
        check("apply deletes old 1", not f_old1.exists())
        check("apply deletes old 2", not f_old2.exists())
        check("apply keeps new", f_new.exists())
        
        # Check archives
        archive_dir = reports_dir / "archive"
        check("archive dir created", archive_dir.exists())
        archives = sorted(list(archive_dir.glob("*.tar.gz")))
        check("two archives created", len(archives) == 2)
        check("archive 1 name", archives[0].name == "reports_2026-04.tar.gz")
        check("archive 2 name", archives[1].name == "reports_2026-05.tar.gz")
        
        # Check health file
        health_path = state_root / "current_health.json"
        check("health file created", health_path.exists())
        if health_path.exists():
            health = json.loads(health_path.read_text())
            check("health has last_reports_archive_timestamp", "last_reports_archive_timestamp" in health)
            check("health has last_reports_archive_files_archived", health.get("last_reports_archive_files_archived") == 2)
            check("health has last_reports_archive_files_deleted", health.get("last_reports_archive_files_deleted") == 2)
            
    finally:
        shutil.rmtree(tmpdir3, ignore_errors=True)

    print(f"\nSelf-tests: {passed} passed, {failed} failed out of {passed + failed}")
    return 1 if failed > 0 else 0

if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--test":
        raise SystemExit(run_self_tests())
    raise SystemExit(main())
