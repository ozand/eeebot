"""Bounded, deterministic Markdown lesson catalogue (#1343)."""
from __future__ import annotations

import argparse
import itertools
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

from nanobot.runtime.schemas import CONTROLLED_LESSON_TAGS

MAX_FILES = 200
MAX_BYTES = 128 * 1024
MAX_INDEX_BYTES = 256 * 1024
HEADER = "# Lesson index\n\n| lesson | prevents | tags |\n|---|---|---|\n"


def _cell(text: str, cap: int = 240) -> str:
    return " ".join(text.replace("|", " ").replace("[", "(").replace("]", ")").split())[:cap]


def generate_index(workspace: Path) -> dict:
    """Never rewrite lesson bodies; publish atomically or preserve the old index."""
    directory = Path(workspace) / "lessons"
    try:
        paths = list(itertools.islice((p for p in directory.glob("*.md")
                     if p.name not in {"README.md", "index.md"}), MAX_FILES + 1))
        if len(paths) > MAX_FILES:
            return {"status": "unavailable", "reason": "file_count_limit"}
        rows = []
        for path in sorted(paths):
            title, prevents, tags = path.stem, "unavailable: prevention missing", []
            try:
                if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                    raise ValueError("unsafe or oversized lesson")
                text = path.read_text(encoding="utf-8")
                heading = re.search(r"^# (.+)$", text, re.M)
                if heading:
                    title = heading[1]
                section = re.search(r"^## (?:Prevention|Prevention Mechanisms|Reusable Insight)\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
                if section and section[1].strip():
                    prevents = re.split(r"(?<=[.!?])\s+", section[1].strip(), maxsplit=1)[0]
                words = set(re.findall(r"[a-z0-9]+", text.lower()))
                tags = sorted(tag for tag in CONTROLLED_LESSON_TAGS
                              if set(re.findall(r"[a-z0-9]+", tag.lower())) <= words)
            except (OSError, UnicodeError, ValueError):
                prevents = "unavailable: unreadable or oversized lesson"
            rows.append(f"| [{_cell(title, 200)}]({quote(path.name, safe='')}) | {_cell(prevents)} | {', '.join(tags)} |\n")
        content = HEADER + "".join(rows)
        if len(content.encode()) > MAX_INDEX_BYTES:
            return {"status": "unavailable", "reason": "index_size_limit"}
        if not directory.is_dir():
            return {"status": "unavailable", "reason": "missing_directory"}
        target = directory / "index.md"
        temp = directory / ".index.md.tmp"
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, target)
        return {"status": "ok", "rows": len(rows)}
    except (OSError, ValueError):
        return {"status": "unavailable", "reason": "io_error"}


def read_index(path: Path) -> list[dict]:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_INDEX_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
        if not text.startswith(HEADER):
            return []
        lines = text[len(HEADER):].splitlines()
        if len(lines) > MAX_FILES:
            return []
        entries = []
        for line in lines:
            match = re.fullmatch(r"\| \[(.*?)\]\(([^)]+)\) \| (.*?) \| (.*?) \|", line)
            if not match:
                return []
            title, filename, prevention, tags = match.groups()
            filename = unquote(filename)
            if '/' in filename or '\\' in filename or not filename.endswith('.md') or filename in {'README.md', 'index.md'}:
                return []
            if not prevention or len(title) > 200 or len(prevention) > 240:
                return []
            tag_list = tags.split(', ') if tags else []
            if not set(tag_list) <= CONTROLLED_LESSON_TAGS:
                return []
            rel = f"lessons/{filename}"
            entries.append({"id": rel, "title": title, "category": tags,
                            "approach": f"Read {rel}: {prevention}", "path": rel})
        return entries
    except (OSError, UnicodeError, ValueError):
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("TARGET_WORKSPACE", ".")))
    args = parser.parse_args()
    print(generate_index(args.workspace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
