import json
from pathlib import Path

import yaml

from nanobot.runtime.knowledge_curator import promote_reflector_recommendations_to_v2


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

    row = {
        "cycle_id": "cycle-abcdef123456",
        "recommendations": [
            {
                "kind": "error_pattern",
                "detail": "Completely new problem",
            }
        ]
    }

    (reflector_dir / "reflections.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    promoted = promote_reflector_recommendations_to_v2(workspace=workspace, state_dir=state_dir, max_items=10)
    assert promoted == 1

    data = yaml.safe_load(lessons_yaml.read_text(encoding="utf-8"))
    lessons = data.get("lessons", [])
    assert len(lessons) == 2

    ids = [entry["id"] for entry in lessons]
    # No duplicate IDs should exist
    assert len(set(ids)) == 2
