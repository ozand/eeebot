"""Deterministic journal-to-story preparation and provenance validation (#1622).

This module owns the harness-side trust boundary for narration. It does not
choose work, call a model, render video, or publish. It reduces ledger rows to
bounded beats, carries the source line and sign into every beat, builds a
model prompt from those beats, and validates a model-shaped response before an
artifact can be assembled.
"""
from __future__ import annotations

import gzip
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "journal-story-v1"
PRODUCER = "scripts.journal_story"
DEFAULT_MAX_BEATS = 8
# The journal source, named once (#1622 comment 2026-09-16): the cycle ledger
# under ``state/ledger`` — the live ``cycles.jsonl`` plus the dated
# ``cycles-YYYY-MM-DD.jsonl.gz`` archives the midnight rotation writes. Never
# ``state/reflector/reflections.jsonl``: that is model-written prose and may
# not supply a beat's sign or existence (ADR-015 rule 1).
LEDGER_SOURCE = "state/ledger"
_LIVE_LEDGER = "cycles.jsonl"
_MAX_LINE_BYTES = 256 * 1024
_UNKNOWN_MARKERS = frozenset({"probe_unavailable", "unavailable", "unknown"})
_POSITIVE_WORDS = frozenset({"fixed", "working", "succeeded", "success", "improved", "resolved"})
_NEGATIVE_WORDS = frozenset({"failed", "failure", "broken", "regressed", "rejected"})
_CONFIDENT_UNKNOWN_WORDS = frozenset({"finally", "clearly", "definitely", "fixed", "improved", "resolved", "succeeded"})


class StoryValidationError(ValueError):
    """Narration cannot be traced to the deterministic beat set."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sign(row: dict[str, Any]) -> str:
    """Return the sign from the row only; prose never participates here."""
    values = {
        _text(row.get("outcome")).lower(),
        _text(row.get("status")).lower(),
        _text(row.get("reason")).lower(),
        _text(row.get("verdict")).lower(),
    }
    if values & _UNKNOWN_MARKERS or any("probe_unavailable" in value for value in values):
        return "unknown"
    outcome = _text(row.get("outcome")).lower()
    if outcome == "success":
        return "worked"
    if outcome in {"failed", "failure", "error"}:
        return "failed"
    if outcome.startswith("skipped") or outcome in {"idle", "unchanged"}:
        return "unchanged"
    if outcome == "partial":
        return "unknown"
    return "unknown"


def _event_text(row: dict[str, Any]) -> str:
    for key in ("task_title", "task", "reason", "expected_outcome_claim", "serves"):
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _source(row: dict[str, Any]) -> dict[str, Any]:
    source_file = _text(row.get("_source_file"))
    source_line = row.get("_source_line")
    if not source_file or not isinstance(source_line, int):
        raise StoryValidationError("every selected row needs _source_file and _source_line")
    return {
        "file": source_file,
        "line": source_line,
        "cycle_id": _text(row.get("cycle_id")) or None,
        "phase": _text(row.get("phase")) or None,
    }


def _day_files(ledger_dir: Path, day: date) -> list[Path]:
    """Files that can hold rows stamped ``day``: neighbouring archives + live file.

    Rotation names an archive after the day the live file was last modified,
    so a UTC day's rows can sit in the archive named for it, for the day after
    (late writers), or still in the live file. Reading the bounded neighbour
    set — never the live file alone — is what keeps a rotation boundary from
    shrinking the day into a short, confident script.
    """
    names = [f"cycles-{(day + timedelta(days=offset)).isoformat()}.jsonl.gz" for offset in (-1, 0, 1)]
    return [*(ledger_dir / name for name in names), ledger_dir / _LIVE_LEDGER]


def load_day_journal(state_dir: str | Path, day: str) -> dict[str, Any]:
    """Read one UTC day of ledger rows through the rotation-aware path.

    Every returned row carries ``_source_file`` and ``_source_line`` so a beat
    can cite it. Unreadable lines and unreadable files are reported, not
    dropped: ``status`` is ``incomplete`` whenever any row of the day may be
    missing, so a thin day (``complete``, few rows) and a partly unreadable
    day never look alike (ADR-015 rule 5). No model is involved.
    """
    requested = date.fromisoformat(day)
    ledger_dir = Path(state_dir) / "ledger"
    rows: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    files_read: list[str] = []
    notes: list[str] = []
    if not ledger_dir.is_dir():
        notes.append("ledger_dir_missing")
    for path in _day_files(ledger_dir, requested) if not notes else []:
        if not path.is_file():
            continue
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    if len(line) > _MAX_LINE_BYTES:
                        unreadable.append({"file": path.name, "line": line_no, "reason": "line_too_long"})
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        unreadable.append({"file": path.name, "line": line_no, "reason": "malformed_json"})
                        continue
                    if not isinstance(row, dict):
                        unreadable.append({"file": path.name, "line": line_no, "reason": "not_an_object"})
                        continue
                    stamp = _parse_ts(row.get("ts"))
                    if stamp is None or stamp.astimezone(timezone.utc).date() != requested:
                        continue
                    rows.append({**row, "_source_file": path.name, "_source_line": line_no})
        except PermissionError:
            notes.append(f"permission:{path.name}")
            continue
        except (OSError, EOFError, gzip.BadGzipFile):
            notes.append(f"unreadable_file:{path.name}")
            continue
        files_read.append(path.name)
    rows.sort(key=lambda row: (_parse_ts(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc), row["_source_file"], row["_source_line"]))
    status = "incomplete" if unreadable or notes else "complete"
    return {
        "source": LEDGER_SOURCE,
        "day": requested.isoformat(),
        "status": status,
        "files_read": files_read,
        "rows": rows,
        "unreadable": unreadable,
        "notes": notes,
    }


def select_beats(
    rows: Iterable[dict[str, Any]],
    *,
    day: str | None = None,
    max_beats: int = DEFAULT_MAX_BEATS,
) -> list[dict[str, Any]]:
    """Select an ordered, deterministic, ledger-backed beat set.

    Terminal outcome rows are the event source. One row per cycle is retained
    — the cycle's *last* outcome row, because a retried cycle writes several
    and the last one is its terminal sign — sorted by timestamp and source
    location. When an outcome row carries no event text, the same cycle's
    ``proposed`` row supplies the title; the sign is never read from it. A
    quiet day therefore returns an empty list rather than padding material
    into a story.
    """
    if max_beats <= 0:
        raise ValueError("max_beats must be positive")
    candidates: list[tuple[datetime, str, int, dict[str, Any]]] = []
    titles: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("phase") == "proposed":
            cycle_id, title = _text(row.get("cycle_id")), _text(row.get("task_title"))
            if cycle_id and title:
                titles.setdefault(cycle_id, title)
            continue
        if row.get("phase") != "outcome":
            continue
        timestamp = _parse_ts(row.get("ts"))
        if timestamp is None:
            continue
        if day is not None and timestamp.astimezone(timezone.utc).date().isoformat() != day:
            continue
        try:
            source = _source(row)
        except StoryValidationError:
            continue
        candidates.append((timestamp, source["file"], source["line"], row))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    last_index: dict[str, int] = {}
    for index, (_timestamp, _file, _line, row) in enumerate(candidates):
        cycle_id = _text(row.get("cycle_id"))
        if cycle_id:
            last_index[cycle_id] = index
    beats: list[dict[str, Any]] = []
    for index, (_timestamp, _file, _line, row) in enumerate(candidates):
        cycle_id = _text(row.get("cycle_id"))
        if cycle_id and last_index[cycle_id] != index:
            continue
        source = _source(row)
        event = _event_text(row) or titles.get(cycle_id) or f"{_text(row.get('phase')) or 'journal'} event"
        beats.append({
            "beat_id": f"beat-{len(beats) + 1:03d}",
            "event": event,
            "sign": _sign(row),
            "cost": row.get("duration_ms") if isinstance(row.get("duration_ms"), (int, float)) else None,
            "source": source,
        })
        if len(beats) >= max_beats:
            break
    return beats


def build_story_prompt(beats: list[dict[str, Any]], glossary: dict[str, str] | None = None) -> str:
    """Build the only model input: selected beats, their signs and citations."""
    payload = {
        "instruction": "Write plain warm narration from these beats only. Do not add events.",
        "sign_rule": "The supplied sign is authoritative; preserve it exactly.",
        "glossary": glossary or {},
        "beats": beats,
        "response_shape": "list of {beat_id, text, sign, terms}",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _contains_any(text: str, words: frozenset[str]) -> bool:
    lowered = text.casefold()
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)


def validate_narration(
    narration: Any,
    beats: list[dict[str, Any]],
    *,
    glossary: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Reject uncited, sign-drifting, unknown, or undefined-term claims."""
    if not isinstance(narration, list) or not narration:
        raise StoryValidationError("narration must be a non-empty list")
    beat_map = {str(beat.get("beat_id")): beat for beat in beats}
    allowed_terms = set((glossary or {}).keys())
    validated: list[dict[str, Any]] = []
    for item in narration:
        if not isinstance(item, dict):
            raise StoryValidationError("each narration item must be an object")
        beat_id = _text(item.get("beat_id"))
        if beat_id not in beat_map:
            raise StoryValidationError(f"narration item has no selected beat: {beat_id or 'missing'}")
        text = _text(item.get("text"))
        if not text:
            raise StoryValidationError(f"narration item {beat_id} is empty")
        expected = str(beat_map[beat_id]["sign"])
        claimed = _text(item.get("sign"))
        if claimed != expected:
            raise StoryValidationError(f"sign drift for {beat_id}: expected {expected}, got {claimed or 'missing'}")
        if expected == "failed" and _contains_any(text, _POSITIVE_WORDS):
            raise StoryValidationError(f"positive wording contradicts failed beat {beat_id}")
        if expected == "worked" and _contains_any(text, _NEGATIVE_WORDS):
            raise StoryValidationError(f"negative wording contradicts worked beat {beat_id}")
        if expected == "unknown" and _contains_any(text, _CONFIDENT_UNKNOWN_WORDS):
            raise StoryValidationError(f"confident wording contradicts unknown beat {beat_id}")
        terms = item.get("terms", [])
        if not isinstance(terms, list) or any(str(term) not in allowed_terms for term in terms):
            raise StoryValidationError(f"undefined glossary term in beat {beat_id}")
        validated.append({"beat_id": beat_id, "text": text, "sign": expected, "terms": [str(term) for term in terms]})
    return validated


def assemble_story_artifact(
    beats: list[dict[str, Any]],
    narration: Any,
    *,
    prompt: str,
    model_output: Any,
    glossary: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and assemble the durable story artifact."""
    validated = validate_narration(narration, beats, glossary=glossary)
    return {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "beats": beats,
        "narration": validated,
        "prompt": prompt,
        "model_output": model_output,
        "citation_set": {item["beat_id"]: next(beat["source"] for beat in beats if beat["beat_id"] == item["beat_id"]) for item in validated},
    }
