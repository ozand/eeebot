"""Deterministic journal-to-story preparation and provenance validation (#1622).

This module owns the harness-side trust boundary for narration. It does not
choose work, call a model, render video, or publish. It reduces ledger rows to
bounded beats, carries the source line and sign into every beat, builds a
model prompt from those beats, and validates a model-shaped response before an
artifact can be assembled.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "journal-story-v1"
PRODUCER = "scripts.journal_story"
DEFAULT_MAX_BEATS = 8
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
    return f"{_text(row.get('phase')) or 'journal'} event"


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


def select_beats(
    rows: Iterable[dict[str, Any]],
    *,
    day: str | None = None,
    max_beats: int = DEFAULT_MAX_BEATS,
) -> list[dict[str, Any]]:
    """Select an ordered, deterministic, ledger-backed beat set.

    Terminal outcome rows are the event source. One row per cycle is retained,
    sorted by timestamp and source location. A quiet day therefore returns an
    empty list rather than padding material into a story.
    """
    if max_beats <= 0:
        raise ValueError("max_beats must be positive")
    candidates: list[tuple[datetime, str, int, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("phase") != "outcome":
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
    seen_cycles: set[str] = set()
    beats: list[dict[str, Any]] = []
    for index, (_timestamp, _file, _line, row) in enumerate(candidates):
        cycle_id = _text(row.get("cycle_id"))
        if cycle_id and cycle_id in seen_cycles:
            continue
        if cycle_id:
            seen_cycles.add(cycle_id)
        source = _source(row)
        beats.append({
            "beat_id": f"beat-{len(beats) + 1:03d}",
            "event": _event_text(row),
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
