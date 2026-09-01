import json
from pathlib import Path

import yaml

from nanobot.runtime.knowledge_curator import promote_reflector_recommendations_to_v2


def test_promote_reflector_recommendations_grants_unique_ids(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"

    lessons_dir = workspace / "lessons"
    lessons_dir.mkdir(parents=True)

    reflector_dir = state_dir / "reflector"
    reflector_dir.mkdir(parents=True)

    # One cycle with MULTIPLE recommendations
    row = {
        "cycle_id": "cycle-abcdef123456",
        "recommendations": [
            {
                "kind": "error_pattern",
                "detail": "The system fails to allocate memory during the large array initialization.",
            },
            {
                "kind": "approach_hint",
                "detail": "A null reference exception occurs inside the secondary API endpoint parser.",
            }
        ]
    }

    (reflector_dir / "reflections.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    promoted = promote_reflector_recommendations_to_v2(workspace=workspace, state_dir=state_dir, max_items=10)
    assert promoted == 2

    lessons_yaml = lessons_dir / "lessons.yaml"
    assert lessons_yaml.exists()

    data = yaml.safe_load(lessons_yaml.read_text(encoding="utf-8"))
    lessons = data.get("lessons", [])
    assert len(lessons) == 2

    id1 = lessons[0]["id"]
    id2 = lessons[1]["id"]

    assert id1 != id2, f"Collision detected: both lessons have ID {id1}"
    assert id1.startswith("LESS-REF-abcdef123456")
    assert id2.startswith("LESS-REF-abcdef123456")
    print(lessons_yaml.read_text(encoding="utf-8"))
