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


def test_promote_reflector_recommendations_grants_unique_ids(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"

    lessons_dir = workspace / "lessons"
    lessons_dir.mkdir(parents=True)

    reflector_dir = state_dir / "reflector"
    reflector_dir.mkdir(parents=True)

    # One cycle with MULTIPLE recommendations — repeated by a second cycle on
    # another day, since #1171 mints on recurrence. Ids derive from the FIRST
    # cycle that made each recommendation.
    recommendations = [
        {
            "kind": "error_pattern",
            "detail": "The system fails to allocate memory during the large array initialization.",
        },
        {
            "kind": "approach_hint",
            "detail": "A null reference exception occurs inside the secondary API endpoint parser.",
        }
    ]
    rows = [
        {"cycle_id": "cycle-abcdef123456", "timestamp": "2026-09-01T10:00:00Z", "recommendations": recommendations},
        {"cycle_id": "cycle-abcdef123457", "timestamp": "2026-09-02T10:00:00Z", "recommendations": recommendations},
    ]

    (reflector_dir / "reflections.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    promoted = promote_reflector_recommendations_to_v2(workspace=workspace, state_dir=state_dir, max_items=10)
    assert promoted == 2
    _materialize_staged_lessons(workspace, state_dir)  # #1209: cards are staged, the pickup writes them

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
