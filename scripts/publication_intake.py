"""Validate the local story artifact before handing it to the publisher (#1614)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.journal_story import StoryValidationError, validate_narration

SCHEMA_VERSION = "publication-intake-v1"
PRODUCER = "scripts.journal_story"


class IntakeRejected(ValueError):
    """The artifact is not a trusted locally-produced story."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntakeRejected(f"cannot read artifact: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise IntakeRejected("artifact must be a JSON object")
    return value


def validate_story_artifact(path: Path) -> dict[str, Any]:
    """Return a sanitized publication manifest; never accept external prose."""
    artifact = _read_json(path)
    if artifact.get("schema_version") != "journal-story-v1":
        raise IntakeRejected("unsupported story schema")
    if artifact.get("producer") != PRODUCER:
        raise IntakeRejected("untrusted artifact producer")
    beats = artifact.get("beats")
    narration = artifact.get("narration")
    if not isinstance(beats, list) or not isinstance(narration, list):
        raise IntakeRejected("artifact lacks beats or narration")
    try:
        validated = validate_narration(narration, beats, glossary={})
    except StoryValidationError as exc:
        raise IntakeRejected(str(exc)) from exc
    citation_set = artifact.get("citation_set")
    if not isinstance(citation_set, dict) or set(citation_set) != {item["beat_id"] for item in validated}:
        raise IntakeRejected("citation set does not cover narration")
    return {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "beat_count": len(beats),
        "narration_count": len(validated),
        "citation_set": citation_set,
        "text": "\n".join(item["text"] for item in validated),
    }


def write_description_file(artifact_path: Path, output_path: Path) -> dict[str, Any]:
    """Write only locally validated narration text for youtube_publish.py."""
    manifest = validate_story_artifact(artifact_path)
    output_path.write_text(manifest["text"] + "\n", encoding="utf-8")
    return {**manifest, "description_path": str(output_path)}
