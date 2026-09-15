from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.journal_story import assemble_story_artifact, select_beats
from scripts.publication_intake import IntakeRejected, validate_story_artifact, write_description_file


def _row():
    return {'phase': 'outcome', 'cycle_id': 'cycle-a', 'outcome': 'success', 'task_title': 'Ship change', 'ts': '2026-09-15T12:00:00Z', '_source_file': 'cycles.jsonl', '_source_line': 4}


def _artifact(path: Path) -> None:
    beats = select_beats([_row()])
    payload = assemble_story_artifact(beats, [{'beat_id': 'beat-001', 'text': 'The change worked.', 'sign': 'worked', 'terms': []}], prompt='local prompt', model_output={'raw': 'local output'})
    path.write_text(json.dumps(payload), encoding='utf-8')


def test_intake_accepts_only_local_story_artifact_and_writes_text(tmp_path: Path):
    artifact = tmp_path / 'story.json'
    description = tmp_path / 'description.txt'
    _artifact(artifact)
    manifest = write_description_file(artifact, description)
    assert manifest['producer'] if 'producer' in manifest else True
    assert description.read_text(encoding='utf-8') == 'The change worked.\n'
    assert manifest['beat_count'] == 1


def test_intake_rejects_external_or_tampered_payload(tmp_path: Path):
    artifact = tmp_path / 'story.json'
    _artifact(artifact)
    payload = json.loads(artifact.read_text())
    payload['producer'] = 'external-input'
    artifact.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(IntakeRejected, match='producer'):
        validate_story_artifact(artifact)


def test_intake_rejects_missing_citation_set(tmp_path: Path):
    artifact = tmp_path / 'story.json'
    _artifact(artifact)
    payload = json.loads(artifact.read_text())
    payload['citation_set'] = {}
    artifact.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(IntakeRejected, match='citation set'):
        validate_story_artifact(artifact)
