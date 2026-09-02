import json
from pathlib import Path

import yaml

from nanobot.runtime.knowledge_curator import promote_reflector_recommendations_to_v2


def _materialize_staged_lessons(workspace: Path, state_dir: Path) -> None:
    """#1209: the mint stages its cards; apply them the way the bridge pickup does."""
    from nanobot.runtime.knowledge_curator import (
        _STAGED_DIR,
        LESSONS_KIND,
        apply_staged_lesson_cards,
        load_staged_manifest,
    )
    for entry in load_staged_manifest(state_dir):
        if entry.get("kind") == LESSONS_KIND:
            payload_path = state_dir / "curator" / _STAGED_DIR / entry["payload_file"]
            apply_staged_lesson_cards(workspace, json.loads(payload_path.read_text(encoding="utf-8")))


def test_promote_reflector_avoids_existing_ids(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"

    lessons_dir = workspace / "lessons"
    lessons_dir.mkdir(parents=True)

    # Pre-populate lessons.yaml with the ID that would normally be minted
    lessons_yaml = lessons_dir / "lessons.yaml"
    pre_existing = {
        "lessons": [
            {
                "schema_version": 2,
                "id": "LESS-REF-abcdef123456-0", # We'll assume ordinal or discriminator might collide
                "title": "Existing problem",
                "problem": "Existing problem description",
                "solution": "Existing solution",
            }
        ]
    }
    lessons_yaml.write_text(yaml.dump(pre_existing), encoding="utf-8")

    reflector_dir = state_dir / "reflector"
    reflector_dir.mkdir(parents=True)

    # Two cycles on two days (#1171 mints on recurrence); the id derives from
    # the first one, whose base collides with the pre-existing card above.
    recommendations = [{"kind": "error_pattern", "detail": "Completely new problem"}]
    rows = [
        {"cycle_id": "cycle-abcdef123456", "timestamp": "2026-09-01T10:00:00Z", "recommendations": recommendations},
        {"cycle_id": "cycle-abcdef123457", "timestamp": "2026-09-02T10:00:00Z", "recommendations": recommendations},
    ]

    (reflector_dir / "reflections.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    promoted = promote_reflector_recommendations_to_v2(workspace=workspace, state_dir=state_dir, max_items=10)
    assert promoted == 1
    _materialize_staged_lessons(workspace, state_dir)  # #1209: cards are staged, the pickup writes them

    data = yaml.safe_load(lessons_yaml.read_text(encoding="utf-8"))
    lessons = data.get("lessons", [])
    assert len(lessons) == 2

    ids = [entry["id"] for entry in lessons]
    # No duplicate IDs should exist
    assert len(set(ids)) == 2
