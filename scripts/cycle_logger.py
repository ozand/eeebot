#!/usr/bin/env python3
"""
cycle_logger.py — prepend one-line cycle summary to memory/HISTORY.md (newest-first).

Usage (from eeebot-self-evolving repo root):
    python3 scripts/cycle_logger.py --test
    python3 scripts/cycle_logger.py --cycle CYCLE_ID --action "feat: did X" --files scripts/foo.py
    python3 scripts/cycle_logger.py --list [--count N] [--json]
    python3 scripts/cycle_logger.py --dedup [--dry-run]
    python3 scripts/cycle_logger.py --compact [--dry-run]
    python3 scripts/cycle_logger.py --stats [--json]

Rules:
- Duplicate cycle_id entries are silently skipped.
- Prepends to memory/HISTORY.md (newest-first), creating it if absent.
- Line format: "- YYYY-MM-DD: [CYCLE_ID] ACTION (files: F1, F2)"
"""
from __future__ import annotations
import argparse
import datetime
import json
import re
import sys
from pathlib import Path


HISTORY_FILE = "memory/HISTORY.md"
_CYCLE_RE = re.compile(r"^\- (\d{4}-\d{2}-\d{2}):\s*\[([^\]]+)\]\s*(.+)$")
_BRACKET_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\]\s*(.+)$")
_VERIFIED_RE = re.compile(
    r"(?i)(verified|confirmed|already\s+implemented|no\s+changes\s+needed)"
)


def append_cycle_summary(
    repo_root: str | Path,
    cycle_id: str,
    action: str,
    files_changed: list[str] | None = None,
) -> bool:
    """
    Prepend one line to memory/HISTORY.md (newest entry first).

    Returns True if line was written, False if cycle_id already present.
    """
    root = Path(repo_root)
    hist = root / HISTORY_FILE
    hist.parent.mkdir(parents=True, exist_ok=True)

    existing = hist.read_text(encoding="utf-8") if hist.exists() else ""
    if cycle_id in existing:
        return False  # duplicate

    date_str = datetime.date.today().isoformat()
    files_part = f" (files: {', '.join(files_changed)})" if files_changed else ""
    line = f"- {date_str}: [{cycle_id}] {action}{files_part}\n"

    # Prepend: new line goes to the top
    hist.write_text(line + existing, encoding="utf-8")
    return True


def list_cycles(repo_root: str | Path, count: int | None = None) -> list[dict]:
    """
    Parse HISTORY.md and return a list of cycle entries (newest-first).

    Each entry: {"date": str, "cycle_id": str, "action": str}
    """
    root = Path(repo_root)
    hist = root / HISTORY_FILE
    if not hist.exists():
        return []

    entries: list[dict] = []
    for line in hist.read_text(encoding="utf-8").splitlines():
        m = _CYCLE_RE.match(line.strip())
        if m:
            entries.append({"date": m.group(1), "cycle_id": m.group(2), "action": m.group(3)})
    return entries[:count] if count else entries


def dedup_cycles(repo_root: str | Path, dry_run: bool = False) -> dict:
    """
    Remove duplicate cycle entries from HISTORY.md, keeping the first (newest) occurrence.

    Returns {"total": int, "duplicates_removed": int, "kept": int, "dry_run": bool}.
    """
    root = Path(repo_root)
    hist = root / HISTORY_FILE
    if not hist.exists():
        return {"total": 0, "duplicates_removed": 0, "kept": 0, "dry_run": dry_run}

    lines = hist.read_text(encoding="utf-8").splitlines(keepends=True)
    seen: set[str] = set()
    kept_lines: list[str] = []
    duplicates_removed = 0

    for line in lines:
        m = _CYCLE_RE.match(line.strip())
        if m:
            cycle_id = m.group(2)
            if cycle_id in seen:
                duplicates_removed += 1
                continue
            seen.add(cycle_id)
        kept_lines.append(line)

    result = {
        "total": len(lines),
        "duplicates_removed": duplicates_removed,
        "kept": len(kept_lines),
        "dry_run": dry_run,
    }

    if not dry_run and duplicates_removed > 0:
        hist.write_text("".join(kept_lines), encoding="utf-8")

    return result


def compact_history(repo_root: str | Path, dry_run: bool = False, max_kept: int = 3) -> dict:
    """
    Collapse repeated verification entries for the same task into a single summary line.

    Lines that match the "verified/confirmed/already implemented" pattern for the same
    priority/task are collapsed into one entry: the first (newest) occurrence is kept,
    and a count of collapsed entries is appended.

    Returns {"total": int, "collapsed": int, "kept": int, "dry_run": bool}.
    """
    root = Path(repo_root)
    hist = root / HISTORY_FILE
    if not hist.exists():
        return {"total": 0, "collapsed": 0, "kept": 0, "dry_run": dry_run}

    lines = hist.read_text(encoding="utf-8").splitlines(keepends=True)
    if not lines:
        return {"total": 0, "collapsed": 0, "kept": 0, "dry_run": dry_run}

    # Group lines by task/priority key
    # Extract a "task key" from each line — e.g. "Priority 5" or "cycle_logger.py"
    def _task_key(line: str) -> str | None:
        # Try to extract "Priority N" or a script name
        m_priority = re.search(r"Priority\s+(\d+)", line, re.IGNORECASE)
        if m_priority:
            return f"Priority {m_priority.group(1)}"
        m_script = re.search(r"(scripts/\w+\.py)", line)
        if m_script:
            return m_script.group(1)
        return None

    # Identify "verification-only" lines (no real code change)
    def _is_verification_only(line: str) -> bool:
        return bool(_VERIFIED_RE.search(line))

    # Process: keep non-verification lines, collapse verification lines by task key
    kept_lines: list[str] = []
    task_groups: dict[str, list[str]] = {}  # task_key -> list of verification lines
    collapsed_count = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept_lines.append(line)
            continue

        key = _task_key(stripped)
        if key and _is_verification_only(stripped):
            task_groups.setdefault(key, []).append(line)
        else:
            kept_lines.append(line)

    # For each task group, keep the first (newest) entry and append a count
    for key, group_lines in task_groups.items():
        if len(group_lines) <= 1:
            kept_lines.append(group_lines[0])
        else:
            # Keep the first (newest) line, append count
            first_line = group_lines[0].rstrip("\n")
            suffix = f" (+{len(group_lines) - 1} more verifications)"
            kept_lines.append(first_line + suffix + "\n")
            collapsed_count += len(group_lines) - 1

    result = {
        "total": len(lines),
        "collapsed": collapsed_count,
        "kept": len(kept_lines),
        "dry_run": dry_run,
    }

    if not dry_run and collapsed_count > 0:
        hist.write_text("".join(kept_lines), encoding="utf-8")

    return result


def cycle_stats(repo_root: str | Path) -> dict:
    """
    Compute statistics about HISTORY.md cycle entries.

    Returns dict with: total_lines, cycle_entries, unique_cycles,
    duplicates, date_range, top_actions.
    """
    root = Path(repo_root)
    hist = root / HISTORY_FILE
    if not hist.exists():
        return {
            "total_lines": 0, "cycle_entries": 0, "unique_cycles": 0,
            "duplicates": 0, "date_range": None, "top_actions": [],
        }

    text = hist.read_text(encoding="utf-8")
    lines = text.splitlines()
    entries = list_cycles(root)
    cycle_ids = [e["cycle_id"] for e in entries]
    unique = set(cycle_ids)

    dates = [e["date"] for e in entries if e["date"]]
    date_range = None
    if dates:
        date_range = {"earliest": min(dates), "latest": max(dates)}

    # Top actions (by frequency of prefix before first paren or long tail)
    from collections import Counter
    action_prefixes: Counter = Counter()
    for e in entries:
        act = e["action"].split("(")[0].strip().rstrip("—–-")
        action_prefixes[act] += 1

    return {
        "total_lines": len(lines),
        "cycle_entries": len(entries),
        "unique_cycles": len(unique),
        "duplicates": len(entries) - len(unique),
        "date_range": date_range,
        "top_actions": action_prefixes.most_common(5),
    }


def _self_test(repo_root: Path) -> None:
    import tempfile, shutil
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "memory").mkdir()
        # First write — should succeed
        ok1 = append_cycle_summary(tmp, "cycle-abc123", "feat: added X", ["scripts/x.py"])
        assert ok1, "First write must return True"
        content = (tmp / HISTORY_FILE).read_text()
        assert "cycle-abc123" in content
        assert "feat: added X" in content
        assert "scripts/x.py" in content

        # Duplicate — should return False, no change
        ok2 = append_cycle_summary(tmp, "cycle-abc123", "feat: added X again", [])
        assert not ok2, "Duplicate must return False"
        content2 = (tmp / HISTORY_FILE).read_text()
        assert content2 == content, "File must not change on duplicate"

        # Second distinct cycle — must appear FIRST (newest-first)
        ok3 = append_cycle_summary(tmp, "cycle-xyz999", "fix: corrected Y")
        assert ok3
        final = (tmp / HISTORY_FILE).read_text()
        assert final.count("cycle-") == 2
        lines = [ln for ln in final.splitlines() if ln.strip()]
        assert "cycle-xyz999" in lines[0], f"Newest must be first line, got: {lines[0]}"
        assert "cycle-abc123" in lines[1], f"Older must be second line, got: {lines[1]}"

        # Test list_cycles
        entries = list_cycles(tmp)
        assert len(entries) == 2, f"Expected 2 entries, got {len(entries)}"
        assert entries[0]["cycle_id"] == "cycle-xyz999"
        assert entries[1]["cycle_id"] == "cycle-abc123"

        # Test list_cycles with count
        entries_1 = list_cycles(tmp, count=1)
        assert len(entries_1) == 1
        assert entries_1[0]["cycle_id"] == "cycle-xyz999"

        # Test list_cycles on empty
        assert list_cycles(tmp / "nonexistent") == []

        # Test dedup_cycles — create file with duplicate cycle_ids
        (tmp / HISTORY_FILE).write_text(
            "- 2026-07-01: [cycle-dup1] first\n"
            "- 2026-07-02: [cycle-dup2] unique\n"
            "- 2026-07-03: [cycle-dup1] duplicate\n"
            "- 2026-07-04: [cycle-dup3] another\n"
            "- 2026-07-05: [cycle-dup2] also duplicate\n",
            encoding="utf-8",
        )
        # Dry run first
        res = dedup_cycles(tmp, dry_run=True)
        assert res["duplicates_removed"] == 2, f"Expected 2 dups, got {res['duplicates_removed']}"
        assert res["kept"] == 3
        assert res["dry_run"] is True
        # File should be unchanged after dry run
        assert (tmp / HISTORY_FILE).read_text().count("cycle-dup1") == 2

        # Actual dedup
        res2 = dedup_cycles(tmp, dry_run=False)
        assert res2["duplicates_removed"] == 2
        assert res2["dry_run"] is False
        final_text = (tmp / HISTORY_FILE).read_text()
        assert final_text.count("cycle-dup1") == 1, "Should keep only first occurrence"
        assert final_text.count("cycle-dup2") == 1
        assert final_text.count("cycle-dup3") == 1
        lines_after = [ln for ln in final_text.splitlines() if ln.strip()]
        assert len(lines_after) == 3, f"Expected 3 lines, got {len(lines_after)}"

        # Dedup on empty file
        (tmp / HISTORY_FILE).write_text("", encoding="utf-8")
        res3 = dedup_cycles(tmp)
        assert res3["duplicates_removed"] == 0
        assert res3["kept"] == 0

        # Test compact_history — create file with repeated verification entries
        (tmp / HISTORY_FILE).write_text(
            "- 2026-07-01: [cycle-v1] feat: added X (files: scripts/x.py)\n"
            "- 2026-07-02: [cycle-v2] chore: confirm Priority 5 (cycle_logger.py) verified for cycle-abc — 9/9 self-tests pass\n"
            "- 2026-07-03: [cycle-v3] chore: confirm Priority 5 (cycle_logger.py) verified for cycle-def — 9/9 self-tests pass\n"
            "- 2026-07-04: [cycle-v4] chore: confirm Priority 5 (cycle_logger.py) verified for cycle-ghi — 9/9 self-tests pass\n"
            "- 2026-07-05: [cycle-v5] fix: corrected Y (files: scripts/y.py)\n",
            encoding="utf-8",
        )
        # Dry run first
        res = compact_history(tmp, dry_run=True)
        assert res["collapsed"] == 2, f"Expected 2 collapsed, got {res['collapsed']}"
        assert res["dry_run"] is True
        # File should be unchanged after dry run
        assert (tmp / HISTORY_FILE).read_text().count("cycle-v") == 5

        # Actual compact
        res2 = compact_history(tmp, dry_run=False)
        assert res2["collapsed"] == 2
        assert res2["dry_run"] is False
        final_text = (tmp / HISTORY_FILE).read_text()
        # Should have 3 lines: feat, chore (with +2 suffix), fix
        lines_after = [ln for ln in final_text.splitlines() if ln.strip()]
        assert len(lines_after) == 3, f"Expected 3 lines, got {len(lines_after)}: {lines_after}"
        # The compacted line should mention "+2 more verifications"
        assert "+2 more verifications" in final_text, f"Missing verification count in: {final_text}"
        # Non-verification lines should be preserved
        assert "feat: added X" in final_text
        assert "fix: corrected Y" in final_text

        # Compact on empty file
        (tmp / HISTORY_FILE).write_text("", encoding="utf-8")
        res3 = compact_history(tmp)
        assert res3["collapsed"] == 0
        assert res3["kept"] == 0

        # Test cycle_stats — restore file with entries first
        (tmp / HISTORY_FILE).write_text(
            "- 2026-07-01: [cycle-s1] action A\n"
            "- 2026-07-02: [cycle-s2] action B\n"
            "- 2026-07-03: [cycle-s3] action C\n",
            encoding="utf-8",
        )
        stats = cycle_stats(tmp)
        assert stats["cycle_entries"] == 3, f"Expected 3 entries, got {stats['cycle_entries']}"
        assert stats["unique_cycles"] == 3
        assert stats["duplicates"] == 0
        assert stats["date_range"] is not None
        assert stats["date_range"]["earliest"] == "2026-07-01"
        assert stats["date_range"]["latest"] == "2026-07-03"

        # Stats on empty
        (tmp / HISTORY_FILE).write_text("", encoding="utf-8")
        stats_empty = cycle_stats(tmp)
        assert stats_empty["cycle_entries"] == 0
        assert stats_empty["date_range"] is None

        print("PASS: 14/14 self-tests passed (newest-first + list_cycles + dedup + compact + stats verified)")
    finally:
        shutil.rmtree(tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepend cycle summary to HISTORY.md (newest-first), dedup, compact, or show stats")
    parser.add_argument("--repo-root", default=".", help="Path to eeebot-self-evolving repo")
    parser.add_argument("--cycle", help="Cycle ID")
    parser.add_argument("--action", help="One-line description of what was done")
    parser.add_argument("--files", nargs="*", default=[], help="Files changed")
    parser.add_argument("--test", action="store_true", help="Run self-tests and exit")
    parser.add_argument("--list", action="store_true", help="List recent cycle entries")
    parser.add_argument("--count", type=int, default=None, help="Limit number of entries (--list)")
    parser.add_argument("--json", action="store_true", help="Output as JSON (--list)")
    parser.add_argument("--dedup", action="store_true", help="Remove duplicate cycle entries from HISTORY.md")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing (--dedup)")
    parser.add_argument("--stats", action="store_true", help="Show HISTORY.md statistics")
    parser.add_argument("--compact", action="store_true", help="Collapse repeated verification entries in HISTORY.md")
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()

    if args.test:
        _self_test(root)
        sys.exit(0)

    if args.dedup:
        result = dedup_cycles(root, dry_run=args.dry_run)
        mode = "DRY RUN" if result["dry_run"] else "DEDUP"
        print(f"[{mode}] total={result['total']}, duplicates_removed={result['duplicates_removed']}, kept={result['kept']}")
        sys.exit(0)

    if args.list:
        entries = list_cycles(root, count=args.count)
        if args.json:
            print(json.dumps(entries, indent=2))
        else:
            if not entries:
                print("No cycle entries found.")
            for e in entries:
                print(f"- {e['date']}: [{e['cycle_id']}] {e['action']}")
        sys.exit(0)

    if args.stats:
        result = cycle_stats(root)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Lines: {result['total_lines']}")
            print(f"Cycle entries: {result['cycle_entries']}")
            print(f"Unique cycles: {result['unique_cycles']}")
            print(f"Duplicates: {result['duplicates']}")
            if result["date_range"]:
                print(f"Date range: {result['date_range']['earliest']} to {result['date_range']['latest']}")
            if result["top_actions"]:
                print("Top actions:")
                for act, cnt in result["top_actions"]:
                    print(f"  {cnt}x {act}")
        sys.exit(0)

    if args.compact:
        result = compact_history(root, dry_run=args.dry_run)
        mode = "DRY RUN" if result["dry_run"] else "COMPACT"
        print(f"[{mode}] total={result['total']}, collapsed={result['collapsed']}, kept={result['kept']}")
        sys.exit(0)

    if not args.cycle or not args.action:
        parser.error("--cycle and --action are required unless --test, --list, --stats, or --dedup is given")

    written = append_cycle_summary(root, args.cycle, args.action, args.files or None)
    if written:
        print(f"Written: [{args.cycle}] {args.action}")
    else:
        print(f"Skipped (duplicate): {args.cycle}")


if __name__ == "__main__":
    main()
