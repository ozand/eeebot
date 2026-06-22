#!/usr/bin/env python3
"""
memory_archiver.py — L0/L1 memory split for eeebot-self-evolving.

Archives old HISTORY.md entries to MEMORY_ARCHIVE.md with weekly summaries.
Uses cl/gemini-3.5-flash-low via LiteLLM for summarization; falls back to
deterministic summary when LLM is unavailable.

Usage:
    python3 scripts/memory_archiver.py --repo-root . [--dry-run] [--force]
    python3 scripts/memory_archiver.py --repo-root . --state-root /var/lib/eeepc-agent/self-evolving-agent/state

Triggers:
    - MEMORY.md > 50 lines (excluding Identity/Rules/How sections)
    - Last archive entry > 6 days ago
    - --force flag

Archive format (MEMORY_ARCHIVE.md):
    ## Week YYYY-WNN (YYYY-MM-DD)
    <3-sentence LLM or deterministic summary>
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────────

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://100.82.9.44:4001/v1")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "sk-litellm-master")
SUMMARY_MODEL = "cl/gemini-3.5-flash-low"
SUMMARY_MAX_TOKENS = 200
SUMMARY_TIMEOUT = 30  # seconds

MEMORY_FILE = "memory/MEMORY.md"
HISTORY_FILE = "memory/HISTORY.md"
ARCHIVE_FILE = "memory/MEMORY_ARCHIVE.md"

# Keep last N days of raw HISTORY entries (older → archive)
HISTORY_KEEP_DAYS = 14
# Archive threshold: last archive older than this many days
ARCHIVE_STALE_DAYS = 6
# Memory line threshold (excluding boilerplate sections)
MEMORY_LINE_THRESHOLD = 50


# ── LLM summarization ─────────────────────────────────────────────────────────

def _summarize_with_llm(entries_text: str, week_label: str) -> str | None:
    """Call cl/gemini-3.5-flash-low to produce a 3-sentence week summary.

    Returns None on any error (LLM unavailable, timeout, etc).
    """
    try:
        import urllib.request
        import urllib.error

        prompt = (
            f"Summarize the following agent activity log for {week_label} in exactly 3 sentences. "
            "Focus on: (1) what concrete artifacts were produced, (2) what was learned or improved, "
            "(3) what patterns emerged. Be specific about file names and actions. "
            "Do not use bullet points — write 3 plain sentences.\n\n"
            f"Activity log:\n{entries_text[:3000]}"
        )

        payload = json.dumps({
            "model": SUMMARY_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": SUMMARY_MAX_TOKENS,
            "temperature": 0,
        }).encode()

        req = urllib.request.Request(
            f"{LITELLM_BASE_URL}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LITELLM_API_KEY}",
            },
            method="POST",
        )

        import socket
        socket.setdefaulttimeout(SUMMARY_TIMEOUT)
        with urllib.request.urlopen(req, timeout=SUMMARY_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
            return result["choices"][0]["message"]["content"].strip()

    except Exception as exc:
        return None  # LLM unavailable — caller uses deterministic fallback


def _deterministic_summary(entries: list[str], week_label: str) -> str:
    """Rules-based summary when LLM unavailable."""
    total = len(entries)
    # Extract action verbs and file mentions
    actions: list[str] = []
    files: set[str] = set()
    for entry in entries:
        # Action patterns: "feat:", "fix:", "chore:", "Implemented", "Added", "Created"
        m = re.search(r'(feat|fix|chore|refactor|docs|test|Implemented|Added|Created|Updated)[:\s]+([^()\n]{5,60})', entry)
        if m:
            actions.append(m.group(2).strip()[:50])
        # File patterns: scripts/*.py, nanobot/**/*.py, memory/*.md
        file_matches = re.findall(r'((?:scripts|nanobot|memory|tests|docs)/[\w./\-]+\.(?:py|md|yaml|json))', entry)
        files.update(file_matches[:2])

    top_actions = "; ".join(list(dict.fromkeys(actions))[:4]) or "various improvements"
    top_files = ", ".join(sorted(files)[:5]) or "multiple files"
    return (
        f"{week_label}: {total} agent cycle(s) recorded. "
        f"Key actions: {top_actions}. "
        f"Files touched: {top_files}."
    )


# ── HISTORY.md parsing ────────────────────────────────────────────────────────

def _parse_history_entries(history_text: str) -> list[dict[str, Any]]:
    """Parse HISTORY.md lines into list of {date, text} dicts.

    Supports both formats:
      - "- YYYY-MM-DD: ..." (standard)
      - "- [cycle-xxx] ..." (cycle_logger format)
    """
    entries = []
    for line in history_text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        # Try to extract date
        date_match = re.match(r"- (\d{4}-\d{2}-\d{2})[:\s]", line)
        if date_match:
            try:
                date = datetime.date.fromisoformat(date_match.group(1))
            except ValueError:
                date = datetime.date.today()
        else:
            date = datetime.date.today()
        entries.append({"date": date, "text": line})
    return entries


def _week_label(d: datetime.date) -> str:
    """Return 'Week YYYY-WNN (YYYY-MM-DD)' for given date."""
    iso = d.isocalendar()
    return f"Week {iso[0]}-W{iso[1]:02d} ({d.isoformat()})"


# ── Archive logic ─────────────────────────────────────────────────────────────

def _last_archive_date(archive_text: str) -> datetime.date | None:
    """Parse most recent ## Week header from MEMORY_ARCHIVE.md."""
    matches = re.findall(r"## Week \d{4}-W\d{2} \((\d{4}-\d{2}-\d{2})\)", archive_text)
    if not matches:
        return None
    try:
        return max(datetime.date.fromisoformat(m) for m in matches)
    except ValueError:
        return None


def _needs_archiving(
    memory_text: str,
    archive_text: str,
    force: bool = False,
) -> bool:
    """Return True if archiving should run."""
    if force:
        return True
    # Threshold 1: last archive > ARCHIVE_STALE_DAYS old
    last = _last_archive_date(archive_text)
    if last is None or (datetime.date.today() - last).days > ARCHIVE_STALE_DAYS:
        return True
    # Threshold 2: MEMORY.md backlog section > MEMORY_LINE_THRESHOLD lines
    active_match = re.search(r"## Active backlog.*?(?=\n## |\Z)", memory_text, re.DOTALL)
    if active_match:
        active_lines = len([l for l in active_match.group(0).splitlines() if l.strip()])
        if active_lines > MEMORY_LINE_THRESHOLD:
            return True
    return False


def archive(
    repo_root: Path,
    state_root: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Main archive function. Returns result dict with action, summary, files_changed."""
    memory_path = repo_root / MEMORY_FILE
    history_path = repo_root / HISTORY_FILE
    archive_path = repo_root / ARCHIVE_FILE

    memory_text = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    history_text = history_path.read_text(encoding="utf-8") if history_path.exists() else ""
    archive_text = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""

    if not _needs_archiving(memory_text, archive_text, force=force):
        return {"action": "skipped", "reason": "not_needed"}

    # Parse history entries
    all_entries = _parse_history_entries(history_text)
    cutoff = datetime.date.today() - datetime.timedelta(days=HISTORY_KEEP_DAYS)
    old_entries = [e for e in all_entries if e["date"] < cutoff]
    recent_entries = [e for e in all_entries if e["date"] >= cutoff]

    if not old_entries:
        # Nothing old enough to archive — still create archive entry for current week
        old_entries = all_entries[-10:] if len(all_entries) > 10 else all_entries
        recent_entries = all_entries[len(old_entries):]

    # Group old entries by ISO week
    weeks: dict[str, list[str]] = {}
    for e in old_entries:
        wlabel = _week_label(e["date"])
        weeks.setdefault(wlabel, []).append(e["text"])

    # Build archive section
    new_archive_sections: list[str] = []
    for wlabel, week_entries in sorted(weeks.items()):
        # Skip if already in archive
        safe_label = wlabel.split(" (")[0]
        if safe_label in archive_text:
            if verbose:
                print(f"  Skipping already archived: {safe_label}")
            continue

        entries_text = "\n".join(week_entries)
        summary = _summarize_with_llm(entries_text, wlabel)
        if summary is None:
            if verbose:
                print(f"  LLM unavailable for {wlabel}, using deterministic fallback")
            summary = _deterministic_summary(week_entries, wlabel)

        section = f"\n## {wlabel}\n{summary}\n"
        new_archive_sections.append(section)
        if verbose:
            print(f"  Archived: {wlabel} ({len(week_entries)} entries)")

    files_changed: list[str] = []

    if new_archive_sections:
        new_archive_text = archive_text + "\n".join(new_archive_sections)
        if not dry_run:
            archive_path.write_text(new_archive_text, encoding="utf-8")
        files_changed.append(ARCHIVE_FILE)

    # Truncate HISTORY.md: keep only recent entries
    if recent_entries and len(recent_entries) < len(all_entries):
        new_history = "\n".join(e["text"] for e in recent_entries) + "\n"
        if not dry_run:
            history_path.write_text(new_history, encoding="utf-8")
        files_changed.append(HISTORY_FILE)

    return {
        "action": "archived" if not dry_run else "dry_run",
        "weeks_archived": len(new_archive_sections),
        "old_entries_archived": len(old_entries),
        "recent_entries_kept": len(recent_entries),
        "files_changed": files_changed,
    }


# ── Bridge trigger helper ─────────────────────────────────────────────────────

def should_archive(repo_root: Path) -> bool:
    """Quick check for bridge — should archiver run now?"""
    memory_path = repo_root / MEMORY_FILE
    archive_path = repo_root / ARCHIVE_FILE
    memory_text = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    archive_text = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
    return _needs_archiving(memory_text, archive_text)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Archive old HISTORY.md entries to MEMORY_ARCHIVE.md")
    parser.add_argument("--repo-root", default=".", help="Path to eeebot-self-evolving repo root")
    parser.add_argument("--state-root", help="Path to agent state root (optional)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    parser.add_argument("--force", action="store_true", help="Force archiving even if not needed")
    parser.add_argument("--verbose", action="store_true", help="Print progress")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    state_root = Path(args.state_root).resolve() if args.state_root else None

    result = archive(
        repo_root=repo_root,
        state_root=state_root,
        dry_run=args.dry_run,
        force=args.force,
        verbose=args.verbose or args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["action"] in ("archived", "skipped", "dry_run") else 1)


if __name__ == "__main__":
    main()
