"""Bounded, steering-only hints from the reflector journal (#1008, #1089)."""
from __future__ import annotations

import gzip
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


def _extract_items(row: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Extract candidate (text, kind, evidence) tuples from a reflection row."""
    extracted: list[tuple[str, str, str]] = []

    # 1. Nested recommendations from live journal schema
    recs = row.get("recommendations")
    if isinstance(recs, list):
        for item in recs:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or ("approach_hint" if item.get("approach_hint") else ""))
            text = str(
                item.get("approach_hint")
                or (item.get("detail") if kind == "approach_hint" or not kind else "")
                or item.get("text")
                or ""
            ).strip()
            if not text and item.get("error_pattern"):
                text = str(item.get("error_pattern")).strip()
                if not kind:
                    kind = "error_pattern"
            if text:
                evidence = str(item.get("evidence") or "")
                extracted.append((text, kind, evidence))

    # 2. Nested findings from live journal schema
    findings = row.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or ("error_pattern" if item.get("error_pattern") else ""))
            text = str(
                item.get("error_pattern")
                or (item.get("detail") if kind == "error_pattern" or not kind else "")
                or item.get("text")
                or ""
            ).strip()
            if not text and item.get("approach_hint"):
                text = str(item.get("approach_hint")).strip()
                if not kind:
                    kind = "approach_hint"
            if text:
                evidence = str(item.get("evidence") or "")
                extracted.append((text, kind, evidence))

    # 3. Flat row fields (legacy schema or direct flat rows)
    flat_kind = str(
        row.get("kind")
        or ("approach_hint" if row.get("approach_hint") else ("error_pattern" if row.get("error_pattern") else ""))
    )
    flat_text = str(
        row.get("approach_hint")
        or row.get("error_pattern")
        or row.get("detail")
        or (row.get("summary") if not extracted else "")
        or ""
    ).strip()
    if flat_text and not any(t == flat_text for t, _, _ in extracted):
        extracted.append((flat_text, flat_kind, str(row.get("evidence") or "")))

    return extracted


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
        from nanobot.runtime import reflector

        # Establish the journal order first (oldest archive to newest live
        # file), then take one bounded tail from that ordered stream. Reading
        # newest-to-oldest while spending the existing byte budget preserves
        # the same newest-record semantics after a rotation without reading an
        # entire archive into memory.
        paths = reflector.reflection_files(state_dir)
        tail_chunks: list[list[str]] = []
        remaining = _MAX_TAIL_BYTES
        for path in reversed(paths):
            if remaining <= 0:
                break
            try:
                opener = gzip.open if path.name.endswith(".gz") else open
                with opener(path, "rb") as fh:  # type: ignore[call-arg]
                    fh.seek(0, 2)
                    size = fh.tell()
                    take = min(size, remaining)
                    fh.seek(max(0, size - take))
                    data = fh.read(take)
                lines = data.decode("utf-8", errors="replace").splitlines()
                if size > take and lines:
                    lines = lines[1:]
                tail_chunks.append(lines)
                remaining -= take
            except Exception:
                continue
        tail_lines: list[str] = []
        for chunk in reversed(tail_chunks):
            tail_lines.extend(chunk)

        candidates: list[tuple[int, str, str]] = []
        for line in reversed(tail_lines):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            ts = _parse(row.get("ts") or row.get("created_at") or row.get("timestamp"))
            if ts is None or ts < cutoff:
                continue
            items = _extract_items(row)
            if not items:
                continue
            for text, kind, evidence in items:
                if not text:
                    continue
                shared = len(
                    task_words
                    & _words(
                        f"{row.get('task_title','')} {row.get('target_path','')} {row.get('summary','')} {evidence} {text}"
                    )
                )
                if not shared:
                    continue
                preferred = (
                    1
                    if kind in {"approach_hint", "error_pattern"}
                    or row.get("approach_hint")
                    or row.get("error_pattern")
                    else 0
                )
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
    return (
        "## Recent reflections (steering hints)\n"
        + "\n".join(f"- {h[:_MAX_HINT_CHARS]}" for h in hints)[:_MAX_SECTION_CHARS]
        + "\n"
    )

