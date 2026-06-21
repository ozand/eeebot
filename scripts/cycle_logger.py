#!/usr/bin/env python3
"""
cycle_logger.py — prepend one-line cycle summary to memory/HISTORY.md (newest-first).

Usage (from eeebot-self-evolving repo root):
    python3 scripts/cycle_logger.py --test
    python3 scripts/cycle_logger.py --cycle CYCLE_ID --action "feat: did X" --files scripts/foo.py

Rules:
- Duplicate cycle_id entries are silently skipped.
- Prepends to memory/HISTORY.md (newest-first), creating it if absent.
- Line format: "- YYYY-MM-DD: [CYCLE_ID] ACTION (files: F1, F2)"
"""
from __future__ import annotations
import argparse
import datetime
import sys
from pathlib import Path


HISTORY_FILE = "memory/HISTORY.md"


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
        lines = [l for l in final.splitlines() if l.strip()]
        assert "cycle-xyz999" in lines[0], f"Newest must be first line, got: {lines[0]}"
        assert "cycle-abc123" in lines[1], f"Older must be second line, got: {lines[1]}"

        print("PASS: 3/3 self-tests passed (newest-first order verified)")
    finally:
        shutil.rmtree(tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepend cycle summary to HISTORY.md (newest-first)")
    parser.add_argument("--repo-root", default=".", help="Path to eeebot-self-evolving repo")
    parser.add_argument("--cycle", help="Cycle ID")
    parser.add_argument("--action", help="One-line description of what was done")
    parser.add_argument("--files", nargs="*", default=[], help="Files changed")
    parser.add_argument("--test", action="store_true", help="Run self-tests and exit")
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()

    if args.test:
        _self_test(root)
        sys.exit(0)

    if not args.cycle or not args.action:
        parser.error("--cycle and --action are required unless --test is given")

    written = append_cycle_summary(root, args.cycle, args.action, args.files or None)
    if written:
        print(f"Written: [{args.cycle}] {args.action}")
    else:
        print(f"Skipped (duplicate): {args.cycle}")


if __name__ == "__main__":
    main()
