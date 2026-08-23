from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "host" / "eeepc" / "scripts" / "migrate_goal_priorities.py"
_spec = importlib.util.spec_from_file_location("goal_migration", _SCRIPT)
assert _spec and _spec.loader
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


def _legacy(path: Path) -> None:
    path.write_text(json.dumps({"text": """charter
Current priority targets:
(A) Priority 11 — First priority (V1): first body
(B) Priority 12 — Second priority (V2): second body
"""}), encoding="utf-8")


def test_migration_preserves_numbers_and_is_idempotent(tmp_path: Path):
    legacy = tmp_path / "goal_text.json"
    derived = tmp_path / "derived_priorities.json"
    _legacy(legacy)

    assert migration.migrate(legacy, derived) == 2
    first = json.loads(derived.read_text(encoding="utf-8"))
    assert [(p["number"], p["vector"]) for p in first["priorities"]] == [(11, "V1"), (12, "V2")]

    assert migration.migrate(legacy, derived) == 2
    second = json.loads(derived.read_text(encoding="utf-8"))
    assert second == first


def test_migration_preserves_existing_derived_priority(tmp_path: Path):
    legacy = tmp_path / "goal_text.json"
    derived = tmp_path / "derived_priorities.json"
    _legacy(legacy)
    derived.write_text(json.dumps({
        "schema_version": "derived-priorities-v1",
        "priorities": [{"label": "Minted", "body": "body", "vector": "V1", "number": 17,
                        "added_utc": "2026-01-01T00:00:00Z"}],
    }), encoding="utf-8")

    assert migration.migrate(legacy, derived) == 3
    numbers = [p["number"] for p in json.loads(derived.read_text(encoding="utf-8"))["priorities"]]
    assert numbers == [11, 12, 17]
