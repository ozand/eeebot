#!/usr/bin/env python3
"""One-time #944 migration from mixed goal_text.json to derived_priorities.json."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_PATTERN = re.compile(
    r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[\u2014-]\s*(.+?):(.*?)(?=\n\([A-Za-z]\)|\Z)",
    re.DOTALL,
)


def parse_priorities(text: str) -> list[dict[str, object]]:
    marker = "Current priority targets:"
    if marker not in text:
        return []
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entries: list[dict[str, object]] = []
    for match in _PATTERN.finditer(text.split(marker, 1)[1]):
        number = int(match.group(1))
        label_raw = match.group(2).strip()
        tag = re.search(r"\((V1|V2)\)\s*$", label_raw)
        vector = tag.group(1) if tag else "V1"
        label = re.sub(r"\s*\((V1|V2)\)\s*$", "", label_raw).strip()
        body = re.sub(r"\s+", " ", match.group(3)).strip()[:600]
        if label and body and number > 0:
            entries.append({"label": label, "body": body, "vector": vector,
                            "number": number, "added_utc": now})
    return entries


def migrate(goal_text_path: Path, derived_path: Path) -> int:
    legacy = json.loads(goal_text_path.read_text(encoding="utf-8"))
    migrated = parse_priorities(str(legacy.get("text") or ""))
    if not migrated:
        raise ValueError("legacy goal_text.json contains no parseable priorities")

    existing: list[dict[str, object]] = []
    if derived_path.exists():
        current = json.loads(derived_path.read_text(encoding="utf-8"))
        if current.get("schema_version") != "derived-priorities-v1" or not isinstance(current.get("priorities"), list):
            raise ValueError("existing derived_priorities.json is invalid")
        existing = current["priorities"]

    by_number = {int(item.get("number") or 0): item for item in existing if isinstance(item, dict)}
    for item in migrated:
        by_number.setdefault(int(item["number"]), item)
    output = {"schema_version": "derived-priorities-v1",
              "priorities": [by_number[number] for number in sorted(by_number) if number > 0]}

    derived_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = derived_path.with_name(derived_path.name + ".tmp")
    tmp.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, derived_path)
    return len(output["priorities"])


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: migrate_goal_priorities.py GOAL_TEXT DERIVED", file=sys.stderr)
        return 2
    try:
        count = migrate(Path(sys.argv[1]), Path(sys.argv[2]))
    except Exception as exc:
        print(f"goal-priority migration failed: {exc}", file=sys.stderr)
        return 1
    print(f"goal-priority migration preserved {count} priorities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
