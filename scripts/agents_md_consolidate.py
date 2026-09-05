#!/usr/bin/env python3
"""
agents_md_consolidate.py — operator-only AGENTS.md consolidation (#1313).

#1188 measured a file that only ever grows; #1300/#1302 gave the loop a
declared-droppable marker (``<!-- prompt-fit: droppable -->``) so the cap
never silently deletes standing instructions; #1313 measured that marking a
section droppable buys reserve once but does not bound the file, and that
nothing ever removes. This script is the removal path — run by the operator,
never by the loop (#1193 already made ``AGENTS.md`` operator-owned; this
script is not imported by, or reachable from, any runtime/loop code path).

It removes ONLY the ``## `` sections named explicitly on the command line,
and ONLY if every one of them already carries the exact droppable marker.
Anything else — a section that does not exist, or exists but is critical
(unmarked) — refuses the WHOLE run: no partial removal, no write. Dry-run
(no write) is the default; ``--apply`` is required to actually change the
file, and the write is atomic (temp file in the same directory, then
``os.replace``).

Usage:
    python3 scripts/agents_md_consolidate.py --file AGENTS.md \\
        --section "Big optional appendix" --section "Small optional note"
    python3 scripts/agents_md_consolidate.py --file AGENTS.md \\
        --section "Big optional appendix" --apply

``--section`` accepts the heading with or without its leading ``## ``.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from nanobot.agent.context import ContextBuilder

DROPPABLE_MARKER = ContextBuilder.DROPPABLE_MARKER


def _normalize_heading(raw: str) -> str:
    raw = raw.strip()
    return raw if raw.startswith("## ") else f"## {raw}"


def _split(text: str) -> list[tuple[str, str]]:
    """Reuse the one place this repo defines "how a bootstrap file splits
    into ## sections" — do not re-implement it here."""
    return ContextBuilder._split_bootstrap_sections(text)


class ConsolidationRefusedError(Exception):
    """A named section could not be safely removed; nothing was written."""


def plan_removal(text: str, requested_headings: list[str]) -> dict:
    """Validate the request against *text* and return a plan describing what
    would be removed and what would remain. Raises :class:`ConsolidationRefusedError`
    (naming every offending section) instead of removing anything partially."""
    units = _split(text)
    by_heading: dict[str, list[int]] = {}
    for i, (heading, _) in enumerate(units):
        by_heading.setdefault(heading, []).append(i)

    missing = [h for h in requested_headings if h not in by_heading]
    if missing:
        raise ConsolidationRefusedError(
            "section(s) not found (no matching '## ' heading): " + ", ".join(missing)
        )

    ambiguous = [h for h in requested_headings if len(by_heading[h]) != 1]
    if ambiguous:
        raise ConsolidationRefusedError(
            "duplicate heading(s) are ambiguous and cannot be removed safely: "
            + ", ".join(ambiguous)
        )

    unmarked = [
        h for h in requested_headings
        if DROPPABLE_MARKER not in units[by_heading[h][0]][1]
    ]
    if unmarked:
        raise ConsolidationRefusedError(
            "refusing critical/unmarked section(s) — missing "
            f"'{DROPPABLE_MARKER}': " + ", ".join(unmarked)
        )

    remove_indices = {i for h in requested_headings for i in by_heading[h]}
    removed_chars = {units[i][0]: len(units[i][1]) for i in sorted(remove_indices)}
    kept_units = [(h, t) for i, (h, t) in enumerate(units) if i not in remove_indices]
    new_text = "".join(t for _, t in kept_units)
    droppable_reserve_chars = sum(len(t) for _, t in kept_units if DROPPABLE_MARKER in t)
    return {
        "removed": removed_chars,
        "removed_total": sum(removed_chars.values()),
        "new_chars": len(new_text),
        "droppable_reserve_chars": droppable_reserve_chars,
        "new_text": new_text,
    }


def _print_report(plan: dict, *, applied: bool) -> None:
    verb = "Removed" if applied else "Would remove"
    for heading, chars in plan["removed"].items():
        print(f"{verb}: {heading} ({chars} chars)")
    print(f"Total removed: {plan['removed_total']} chars")
    print(f"Resulting file size: {plan['new_chars']} chars")
    print(f"Remaining declared-droppable reserve: {plan['droppable_reserve_chars']} chars")
    if not applied:
        print("Dry run — no file written. Pass --apply to write.")


def _atomic_write(path: Path, content: str) -> None:
    """Replace the real file atomically without replacing a symlink itself."""
    target = path.resolve(strict=True)
    tmp_path = target.with_name(target.name + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default="AGENTS.md", help="path to the AGENTS.md-shaped file (default: AGENTS.md)")
    parser.add_argument(
        "--section", action="append", dest="sections", default=[],
        help="a '## ' heading to remove (with or without the '## ' prefix); repeatable",
    )
    parser.add_argument("--apply", action="store_true", help="actually write the file (default: dry-run)")
    args = parser.parse_args(argv)

    if not args.sections:
        print("error: at least one --section is required (nothing to consolidate)", file=sys.stderr)
        return 2

    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2

    requested = [_normalize_heading(s) for s in args.sections]
    text = path.read_text(encoding="utf-8")

    try:
        plan = plan_removal(text, requested)
    except ConsolidationRefusedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.apply:
        _atomic_write(path, plan["new_text"])
    _print_report(plan, applied=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
