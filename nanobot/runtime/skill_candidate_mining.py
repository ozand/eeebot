"""Deterministic recurring-action skill candidate miner (#1006)."""
from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DEFAULT_WINDOW_DAYS = 30
_DEFAULT_MIN_CYCLES = 8
_DEFAULT_MIN_DAYS = 3
_MAX_SAMPLES = 3
_DEFAULT_NGRAMS = (2, 3, 4, 5)
_TRIVIAL = frozenset({
    ("exec:pytest", "exec:git-commit"),
    ("exec:pytest", "exec:git-commit", "exec:git-push"),
})


def _trivial_patterns() -> frozenset[tuple[str, ...]]:
    """Return built-in loop patterns plus operator comma-separated additions.

    Format: ``exec:pytest>exec:git-commit``. This is an operator environment
    knob, not instance state, so the instance cannot disable suppression.
    """
    extra = os.environ.get("SELFEVO_SKILL_CANDIDATE_DENY", "")
    patterns = set(_TRIVIAL)
    for raw in extra.split(","):
        parts = tuple(part.strip() for part in raw.split(">") if part.strip())
        if len(parts) >= 2:
            patterns.add(parts)
    return frozenset(patterns)


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _iter_rows(state_dir: Path, window_days: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    index_dir = Path(state_dir) / "action_index"
    paths = sorted([*index_dir.glob("*.jsonl"), *index_dir.glob("*.jsonl.gz")])
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[call-arg]
                for line in fh:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict) or not isinstance(row.get("actions"), list):
                        continue
                    ts = _parse_ts(row.get("ts"))
                    if ts is not None and ts >= cutoff:
                        rows.append(row)
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
    return rows


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _grams(actions: list[str]) -> list[tuple[str, ...]]:
    result = []
    for n in _DEFAULT_NGRAMS:
        result.extend(tuple(actions[i:i+n]) for i in range(len(actions) - n + 1))
    return result


def _is_subsequence(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    return len(short) < len(long) and any(long[i:i+len(short)] == short for i in range(len(long) - len(short) + 1))


def _existing_skill_match(sequence: tuple[str, ...], selfevo_repo: Path | None) -> bool:
    if not selfevo_repo:
        return False
    try:
        needle = " ".join(sequence).lower()
        for path in (Path(selfevo_repo) / "skills").glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if needle in text or all(token in text for token in sequence):
                return True
    except Exception:
        return False
    return False


def mine(state_dir: Path, selfevo_repo: Path | None = None) -> list[dict[str, Any]]:
    """Return longest qualifying recurring n-grams; fail-open to empty."""
    try:
        min_cycles = _int_env("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", _DEFAULT_MIN_CYCLES)
        min_days = _int_env("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", _DEFAULT_MIN_DAYS)
        stats: dict[tuple[str, ...], dict[str, Any]] = defaultdict(lambda: {"cycles": set(), "days": set(), "samples": []})
        for row in _iter_rows(Path(state_dir), _int_env("SELFEVO_SKILL_CANDIDATE_WINDOW_DAYS", _DEFAULT_WINDOW_DAYS)):
            cycle = str(row.get("cycle_id") or "")
            day = str(row.get("ts") or "")[:10]
            if not cycle or not day:
                continue
            seen = set(_grams([str(a) for a in row["actions"]]))
            for gram in seen:
                stats[gram]["cycles"].add(cycle)
                stats[gram]["days"].add(day)
                if len(stats[gram]["samples"]) < _MAX_SAMPLES:
                    stats[gram]["samples"].append(cycle)
        trivial = _trivial_patterns()
        qualifying = {
            gram: data for gram, data in stats.items()
            if len(data["cycles"]) >= min_cycles
            and len(data["days"]) >= min_days
            and gram not in trivial
        }
        selected = {}
        for gram, data in qualifying.items():
            if any(_is_subsequence(gram, other) for other in qualifying):
                continue
            if _existing_skill_match(gram, selfevo_repo):
                continue
            selected[gram] = data
        return [
            {
                "sequence": list(gram),
                "cycles": len(data["cycles"]),
                "days": len(data["days"]),
                "samples": data["samples"],
            }
            for gram, data in sorted(selected.items(), key=lambda item: (-len(item[0]), item[0]))
        ]
    except Exception:
        return []


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Mine recurring action sequences for skill candidates")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--repo", type=Path, default=None)
    args = parser.parse_args()
    state = args.state_root or Path(os.environ.get("STATE_DIR", Path.home() / ".nanobot"))
    candidates = mine(state, args.repo)
    print(json.dumps({"candidates": candidates, "count": len(candidates)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
