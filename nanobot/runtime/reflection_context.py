"""Bounded, steering-only hints from the reflector journal (#1008)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_MAX_HINTS = 3
_MAX_HINT_CHARS = 200
_MAX_SECTION_CHARS = 800
_MAX_TAIL_BYTES = 128_000
_TTL_DAYS = 7
_WORD_RE = re.compile(r"[A-Za-z0-9_/-]{4,}")


def _words(value: Any) -> set[str]:
    return {x.lower() for x in _WORD_RE.findall(str(value or ""))}


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
    except Exception:
        return None


def _tail_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _MAX_TAIL_BYTES))
            data = fh.read()
        return data.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []


def build_reflection_hints(
    state_dir: Path,
    task_title: str,
    target_path: str = "",
    *,
    now: datetime | None = None,
) -> list[str]:
    """Return up to three recent matching journal hints, fail-open."""
    try:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=_TTL_DAYS)
        task_words = _words(f"{task_title} {target_path}")
        if not task_words:
            return []
        path = Path(state_dir) / "reflector" / "reflections.jsonl"
        candidates: list[tuple[int, str, str]] = []
        for line in reversed(_tail_lines(path)):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            ts = _parse(row.get("ts") or row.get("created_at") or row.get("timestamp"))
            if ts is None or ts < cutoff:
                continue
            text = str(row.get("approach_hint") or row.get("error_pattern") or row.get("detail") or row.get("summary") or "").strip()
            if not text:
                continue
            shared = len(task_words & _words(f"{row.get('task_title','')} {row.get('target_path','')} {text}"))
            if not shared:
                continue
            kind = str(row.get("kind") or "")
            preferred = 1 if kind in {"approach_hint", "error_pattern"} or row.get("approach_hint") or row.get("error_pattern") else 0
            candidates.append((preferred * 100 + shared, ts.isoformat(), text[:_MAX_HINT_CHARS]))
        candidates.sort(key=lambda x: (-x[0], x[1]), reverse=False)
        out: list[str] = []
        total = 0
        for _score, _ts, text in candidates:
            if text in out:
                continue
            rendered = f"- {text}"
            if total + len(rendered) + (1 if out else 0) > _MAX_SECTION_CHARS:
                continue
            out.append(text)
            total += len(rendered) + (1 if len(out) > 1 else 0)
            if len(out) >= _MAX_HINTS:
                break
        return out
    except Exception:
        return []


def render_reflection_hints(hints: list[str]) -> str:
    if not hints:
        return ""
    return "## Recent reflections (steering hints)\n" + "\n".join(f"- {h[:_MAX_HINT_CHARS]}" for h in hints)[:_MAX_SECTION_CHARS] + "\n"
