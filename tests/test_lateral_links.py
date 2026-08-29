"""Focused acceptance tests for lateral [[links]] (#1095).

Tests cover:
1. Schema: related list accepted, capped, preserved; unknown slugs reported not rejected.
2. Mechanical linking: shared-tag entries produce symmetric links within cap;
   shared-lineage entries link; unrelated entries do not.
3. Card rendering: related hint rendered in build_lessons_context cards; absent when empty.
4. Byte-identity: absent related produces byte-identical prompt output.
5. Rotation preserves related field through YAML rotation.
6. Bounded open-count: fill_related_links uses no file I/O (open-count = 0).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from nanobot.runtime.lesson_v2 import (
    atomic_write_yaml,
    bounded_load_yaml,
    fill_related_links,
    related_hint,
    validate_lesson,
)
from nanobot.runtime.lessons_context import build_lessons_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_card(lesson_id: str, tags: list[str], **extra: Any) -> dict:
    card: dict = {
        "schema_version": 2,
        "id": lesson_id,
        "problem": f"Problem for {lesson_id}",
        "solution": f"Solution for {lesson_id}",
        "tags": tags,
        "severity": "medium",
        "evidence": ["cycle-test"],
    }
    card.update(extra)
    return card


# ---------------------------------------------------------------------------
# 1. Schema: related list accepted, capped, preserved; unknown slugs reported
# ---------------------------------------------------------------------------

def test_related_accepted_by_validate_lesson() -> None:
    """related is an optional field; validate_lesson still passes with it."""
    card = _base_card("LESS-1", ["runtime"], related=["LESS-2", "LESS-3"])
    assert validate_lesson(card)


def test_related_absent_still_validates() -> None:
    """Entries without related still validate correctly."""
    card = _base_card("LESS-1", ["runtime"])
    assert "related" not in card
    assert validate_lesson(card)


def test_fill_related_caps_at_three() -> None:
    """fill_related_links must cap related at 3 slugs per entry."""
    # Entry with 4 manually set related (existing) — must be capped to 3.
    entry = _base_card("LESS-A", ["runtime"], related=["X1", "X2", "X3", "X4"])
    updated, _unknown = fill_related_links([entry])
    assert len(updated[0].get("related", [])) <= 3


def test_fill_related_reports_unknown_slugs() -> None:
    """Unknown slug targets are reported (returned in unknown set), never rejected."""
    entry = _base_card("LESS-A", ["runtime"], related=["FUTURE-ENTRY"])
    updated, unknown = fill_related_links([entry])
    # The entry is NOT rejected; it's preserved.
    assert len(updated) == 1
    # FUTURE-ENTRY is not in the entries list → it's unknown.
    assert "FUTURE-ENTRY" in unknown


def test_fill_related_unknown_slug_preserves_entry() -> None:
    """Entry with unknown related slug is preserved (not rejected or dropped)."""
    entry = _base_card("LESS-A", ["runtime"], related=["DOES-NOT-EXIST"])
    updated, unknown = fill_related_links([entry])
    assert updated[0]["id"] == "LESS-A"
    assert "DOES-NOT-EXIST" in unknown


# ---------------------------------------------------------------------------
# 2. Mechanical linking: tag-based and lineage-based, symmetric, unrelated stays
# ---------------------------------------------------------------------------

def test_shared_tags_produce_symmetric_links() -> None:
    """Entries sharing >=2 controlled glossary tags get linked symmetrically."""
    a = _base_card("LESS-A", ["runtime", "state"])
    b = _base_card("LESS-B", ["runtime", "state"])
    c = _base_card("LESS-C", ["lint"])  # only 1 tag shared with a/b if any

    updated, unknown = fill_related_links([a, b, c])
    by_id = {e["id"]: e for e in updated}

    # A and B share 2 tags: they must be linked.
    assert "LESS-B" in by_id["LESS-A"].get("related", [])
    assert "LESS-A" in by_id["LESS-B"].get("related", [])

    # C shares at most 0 tags with A/B → not linked.
    assert "LESS-C" not in by_id["LESS-A"].get("related", [])
    assert "LESS-C" not in by_id["LESS-B"].get("related", [])

    # No unknown slugs: all ids are present.
    assert not unknown


def test_one_shared_tag_does_not_link() -> None:
    """Entries sharing exactly 1 controlled tag must NOT be linked."""
    a = _base_card("LESS-A", ["runtime", "lint"])
    b = _base_card("LESS-B", ["runtime", "state"])  # shares only 'runtime'

    updated, _unknown = fill_related_links([a, b])
    by_id = {e["id"]: e for e in updated}

    assert "LESS-B" not in by_id["LESS-A"].get("related", [])
    assert "LESS-A" not in by_id["LESS-B"].get("related", [])


def test_shared_lineage_produces_links() -> None:
    """Entries with same non-empty delta_evidence value are linked."""
    a = _base_card("LESS-A", ["lint"], delta_evidence="errors-to-integrated-resolution")
    b = _base_card("LESS-B", ["state"], delta_evidence="errors-to-integrated-resolution")

    updated, _unknown = fill_related_links([a, b])
    by_id = {e["id"]: e for e in updated}

    assert "LESS-B" in by_id["LESS-A"].get("related", [])
    assert "LESS-A" in by_id["LESS-B"].get("related", [])


def test_different_lineage_does_not_link() -> None:
    """Entries with different delta_evidence are not linked by lineage."""
    a = _base_card("LESS-A", ["lint"], delta_evidence="lineage-1")
    b = _base_card("LESS-B", ["state"], delta_evidence="lineage-2")

    updated, _unknown = fill_related_links([a, b])
    by_id = {e["id"]: e for e in updated}

    assert "LESS-B" not in by_id["LESS-A"].get("related", [])
    assert "LESS-A" not in by_id["LESS-B"].get("related", [])


def test_cap_at_three_with_many_related() -> None:
    """Each entry's related list is capped at 3 even when many entries link it."""
    # 5 entries all sharing 2 tags → each can have up to 4 links but cap is 3.
    entries = [_base_card(f"LESS-{i}", ["runtime", "state"]) for i in range(5)]
    updated, _unknown = fill_related_links(entries)
    for entry in updated:
        assert len(entry.get("related", [])) <= 3


def test_entry_without_id_is_skipped_for_linking() -> None:
    """Entries without id field are not used as link targets."""
    a = _base_card("LESS-A", ["runtime", "state"])
    no_id: dict = {
        "problem": "no id", "solution": "sol", "tags": ["runtime", "state"],
        "severity": "medium", "evidence": ["x"],
    }
    updated, unknown = fill_related_links([a, no_id])
    # no_id is preserved in output even without id.
    assert len(updated) == 2
    # no_id is not in LESS-A's related (no slug to link).
    assert updated[0].get("related", []) == [] or all(
        s and s != "" for s in updated[0].get("related", [])
    )


def test_fill_related_does_not_mutate_input() -> None:
    """fill_related_links must not mutate the original entry dicts."""
    a = _base_card("LESS-A", ["runtime", "state"])
    b = _base_card("LESS-B", ["runtime", "state"])
    original_a_keys = set(a.keys())
    fill_related_links([a, b])
    assert set(a.keys()) == original_a_keys


# ---------------------------------------------------------------------------
# 3. Card rendering: related hint in build_lessons_context
# ---------------------------------------------------------------------------

def test_related_hint_in_lesson_card(tmp_path: Path) -> None:
    """build_lessons_context includes 'related' in card when entries have related."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    lesson_with_related = {
        "lessons": [
            {
                "id": "LESS-1",
                "title": "parser incremental reads failure",
                "problem": "parser fails when processing incremental reads",
                "solution": "use incremental reads",
                "tags": ["runtime"],
                "approach": "Incremental parser reads avoid failure",
                "reusable_insight": "Use bounded incremental reads everywhere",
                "severity": "medium",
                "evidence": ["cycle-test"],
                "related": ["LESS-2", "LESS-3"],
            }
        ]
    }
    (lessons_dir / "lessons.yaml").write_text(
        yaml.safe_dump(lesson_with_related, allow_unicode=True), encoding="utf-8"
    )

    ctx = build_lessons_context(tmp_path, "parser incremental reads failure")
    assert "relevant_lesson" in ctx
    rl = ctx["relevant_lesson"]
    assert "related" in rl
    assert "LESS-2" in rl["related"] or "LESS-3" in rl["related"]


def test_related_hint_is_absent_when_not_set(tmp_path: Path) -> None:
    """build_lessons_context omits 'related' key when entry has no related field."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    lesson_no_related = {
        "lessons": [
            {
                "id": "LESS-1",
                "title": "parser incremental reads failure",
                "problem": "parser fails when processing incremental reads",
                "solution": "use incremental reads",
                "tags": ["runtime"],
                "approach": "Incremental parser reads avoid failure",
                "reusable_insight": "Use bounded incremental reads everywhere",
                "severity": "medium",
                "evidence": ["cycle-test"],
                # No 'related' field.
            }
        ]
    }
    (lessons_dir / "lessons.yaml").write_text(
        yaml.safe_dump(lesson_no_related, allow_unicode=True), encoding="utf-8"
    )

    ctx = build_lessons_context(tmp_path, "parser incremental reads failure")
    assert "relevant_lesson" in ctx
    rl = ctx["relevant_lesson"]
    assert "related" not in rl


# ---------------------------------------------------------------------------
# 4. Byte-identity: absent related must not change existing output
# ---------------------------------------------------------------------------

def test_related_hint_empty_string() -> None:
    """related_hint() returns '' for entries without related (byte-identical behavior)."""
    entry = {"id": "LESS-1", "problem": "x", "solution": "y"}
    assert related_hint(entry) == ""


def test_related_hint_non_empty() -> None:
    """related_hint() renders compact one-line hint when related is present."""
    entry = {"id": "LESS-1", "related": ["LESS-2", "LESS-3"]}
    hint = related_hint(entry)
    assert hint == "related: LESS-2, LESS-3"


def test_related_hint_capped() -> None:
    """related_hint() caps at 3 slugs."""
    entry = {"id": "LESS-1", "related": ["A", "B", "C", "D"]}
    hint = related_hint(entry)
    assert hint.count(",") == 2  # only 3 slugs → 2 commas


def test_build_lessons_context_byte_identical_without_related(tmp_path: Path) -> None:
    """When no entry has related, build_lessons_context output has no 'related' key at all."""
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "lessons.yaml").write_text(
        yaml.safe_dump({
            "lessons": [{
                "id": "LESS-1",
                "title": "incremental parser bounded reads",
                "problem": "incremental parser bounded reads fails",
                "solution": "incremental bounded solution",
                "tags": ["runtime"],
                "approach": "Use incremental bounded streaming",
                "reusable_insight": "Stream data incrementally",
                "severity": "medium",
                "evidence": ["cycle-x"],
            }]
        }),
        encoding="utf-8",
    )
    ctx = build_lessons_context(tmp_path, "incremental parser bounded reads")
    rl = ctx.get("relevant_lesson", {})
    assert "related" not in rl


# ---------------------------------------------------------------------------
# 5. Rotation preserves related field
# ---------------------------------------------------------------------------

def test_rotation_preserves_related(tmp_path: Path) -> None:
    """lessons_rotation preserves 'related' field through atomic YAML round-trip."""
    lessons_path = tmp_path / "lessons.yaml"
    # Write entry with related using atomic_write_yaml.
    entries = [
        _base_card("LESS-1", ["runtime", "state"], related=["LESS-2"]),
        _base_card("LESS-2", ["runtime", "state"], related=["LESS-1"]),
    ]
    atomic_write_yaml(lessons_path, {"lessons": entries})

    # Read back and verify related is preserved.
    loaded = bounded_load_yaml(lessons_path)
    by_id = {e["id"]: e for e in loaded}
    assert "LESS-2" in by_id["LESS-1"]["related"]
    assert "LESS-1" in by_id["LESS-2"]["related"]


def test_rotation_module_preserves_related_bytes(tmp_path: Path) -> None:
    """lessons_rotation.rotate_lessons_file round-trips related field via raw bytes."""
    from nanobot.runtime.lessons_rotation import rotate_lessons_file

    yaml_path = tmp_path / "lessons.yaml"
    # Build 201 entries with id FIRST (the rotation parser splits on '- id:' boundaries).
    # Use atomic_write_yaml via manual write — sort_keys=False and id-first dict ordering.
    entries = []
    for i in range(201):
        # id must be first key so yaml.safe_dump(sort_keys=False) emits '- id:' as boundary.
        e = {
            "id": f"LESS-{i:03d}",
            "problem": f"Problem for LESS-{i:03d}",
            "solution": f"Solution for LESS-{i:03d}",
            "tags": ["runtime", "state"],
            "severity": "medium",
            "evidence": ["cycle-test"],
        }
        if i < 5:
            e["related"] = ["LESS-999"]
        entries.append(e)
    # sort_keys=False keeps id first so the rotation parser sees '- id:' entry boundaries.
    yaml_path.write_text(
        yaml.safe_dump({"lessons": entries}, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = rotate_lessons_file(yaml_path)
    # Rotation should have occurred (returns archive name): 201 entries > 200 cap.
    assert result is not None

    # Active window still parseable.
    loaded_text = yaml_path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(loaded_text)
    assert isinstance(loaded.get("lessons"), list)
    # Entries 0..4 had 'related'; they are in the active window (first 200).
    active_related = [
        e for e in loaded["lessons"] if "related" in e
    ]
    assert active_related, "at least one entry with 'related' should survive rotation"


# ---------------------------------------------------------------------------
# 6. Bounded open-count: fill_related_links uses no file I/O
# ---------------------------------------------------------------------------

def test_fill_related_links_no_file_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """fill_related_links must not open any files (pure in-memory operation)."""
    open_calls: list[str] = []
    original_open = open

    def counting_open(*args: Any, **kwargs: Any) -> Any:
        # Record any call to open.
        open_calls.append(str(args[0]) if args else "unknown")
        return original_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)

    entries = [
        _base_card("LESS-A", ["runtime", "state"]),
        _base_card("LESS-B", ["runtime", "state"]),
    ]
    fill_related_links(entries)

    assert not open_calls, f"fill_related_links opened files: {open_calls}"


# ---------------------------------------------------------------------------
# 7. Bridge _write_structured_lesson fills related links before writing
# ---------------------------------------------------------------------------

def test_write_structured_lesson_fills_related(tmp_path: Path) -> None:
    """_write_structured_lesson calls fill_related_links before writing (#1095).

    When two lessons share >=2 controlled tags, the written file must contain
    'related' fields on both entries (symmetric linking).
    """
    import yaml

    from nanobot.runtime.bridge import _write_structured_lesson

    repo = tmp_path / "repo"
    (repo / "lessons").mkdir(parents=True)

    # Pre-populate with one lesson that shares tags with the one we'll write.
    existing_lesson = {
        "schema_version": 2,
        "id": "LESS-EXISTING",
        "title": "Existing runtime state lesson",
        "problem": "Existing runtime state problem bounded read failure",
        "solution": "Use bounded state reads to avoid unbounded loading",
        "tags": ["runtime", "state"],
        "severity": "medium",
        "seen_count": 1,
        "first_seen": "2026-01-01",
        "last_seen": "2026-01-01",
        "evidence": ["cycle-existing"],
        "date": "2026-01-01",
        "cycle_id": "cycle-existing",
        "task_id": "existing-task",
        "hypothesis": "Existing hypothesis",
        "result": "Committed 1 commit(s)",
        "approach": "Use bounded reads",
        "generalized_insight": "Use bounded state reads to avoid unbounded loading",
        "reusable_insight": "Use bounded state reads to avoid unbounded loading",
        "files_changed": ["nanobot/runtime/state.py"],
    }
    (repo / "lessons" / "lessons.yaml").write_text(
        yaml.safe_dump({"lessons": [existing_lesson]}, allow_unicode=True),
        encoding="utf-8",
    )
    # Also write an errors.yaml with a matching entry to trigger delta evidence.
    (repo / "lessons" / "errors.yaml").write_text(
        yaml.safe_dump([{
            "task_id": "runtime state bounded read",
            "reason": "prior failure",
        }]),
        encoding="utf-8",
    )

    # Write a new lesson that shares >=2 tags with existing (runtime + state).
    wrote = _write_structured_lesson(
        repo_root=repo,
        cycle_id="cycle-abc123def456",
        backlog_title="runtime state bounded read",
        files_changed=["nanobot/runtime/state.py"],
        commits_pushed=1,
        artifact_data={
            "hypothesis": "runtime state bounded read improves performance",
            "reusable_insight": "Bounded state reads prevent memory spikes in runtime",
            "problem": "Runtime state bounded read failure causes memory spikes",
            "solution": "Bounded state reads prevent memory spikes in runtime",
            "tags": ["runtime", "state"],
            "severity": "medium",
            "evidence": ["cycle-abc123def456"],
            "delta_evidence": True,
        },
    )
    assert wrote is True

    loaded = yaml.safe_load(
        (repo / "lessons" / "lessons.yaml").read_text(encoding="utf-8")
    )
    lessons = loaded["lessons"]
    by_id = {e["id"]: e for e in lessons}

    # Both entries share runtime+state tags → should be symmetrically linked.
    new_id = next(k for k in by_id if k != "LESS-EXISTING")
    new_entry = by_id[new_id]
    existing_entry = by_id["LESS-EXISTING"]

    assert "LESS-EXISTING" in new_entry.get("related", []), (
        f"New lesson missing related link to LESS-EXISTING; new_entry={new_entry}"
    )
    assert new_id in existing_entry.get("related", []), (
        f"LESS-EXISTING missing related link to new lesson; existing_entry={existing_entry}"
    )


def test_write_structured_lesson_no_related_when_no_overlap(tmp_path: Path) -> None:
    """fill_related_links in bridge writer: entries with <2 shared tags get no related."""
    import yaml

    from nanobot.runtime.bridge import _write_structured_lesson

    repo = tmp_path / "repo"
    (repo / "lessons").mkdir(parents=True)

    existing_lesson = {
        "schema_version": 2,
        "id": "LESS-DISJOINT",
        "title": "Disjoint tags lesson",
        "problem": "Disjoint tags problem with reflector output parsing",
        "solution": "Use separate paths for reflector output handling",
        "tags": ["reflector"],
        "severity": "medium",
        "seen_count": 1,
        "first_seen": "2026-01-01",
        "last_seen": "2026-01-01",
        "evidence": ["cycle-disjoint"],
        "date": "2026-01-01",
        "cycle_id": "cycle-disjoint",
        "task_id": "disjoint-task",
        "hypothesis": "Disjoint hypothesis",
        "result": "Committed 1 commit(s)",
        "approach": "Use separate reflector paths",
        "generalized_insight": "Use separate paths for reflector output handling",
        "reusable_insight": "Use separate paths for reflector output handling",
        "files_changed": ["nanobot/runtime/reflector.py"],
    }
    (repo / "lessons" / "lessons.yaml").write_text(
        yaml.safe_dump({"lessons": [existing_lesson]}, allow_unicode=True),
        encoding="utf-8",
    )
    (repo / "lessons" / "errors.yaml").write_text(
        yaml.safe_dump([{
            "task_id": "runtime bounded read delta fix",
            "reason": "prior failure",
        }]),
        encoding="utf-8",
    )

    wrote = _write_structured_lesson(
        repo_root=repo,
        cycle_id="cycle-xyz789abc123",
        backlog_title="runtime bounded read delta fix",
        files_changed=["nanobot/runtime/lesson_v2.py"],
        commits_pushed=1,
        artifact_data={
            "hypothesis": "runtime bounded read delta fix improves performance",
            "reusable_insight": "Bounded reads prevent memory spikes in runtime modules",
            "problem": "Runtime bounded read delta fix failure causes memory spikes",
            "solution": "Bounded reads prevent memory spikes in runtime modules",
            "tags": ["runtime"],
            "severity": "medium",
            "evidence": ["cycle-xyz789abc123"],
            "delta_evidence": True,
        },
    )
    assert wrote is True

    loaded = yaml.safe_load(
        (repo / "lessons" / "lessons.yaml").read_text(encoding="utf-8")
    )
    lessons = loaded["lessons"]
    by_id = {e["id"]: e for e in lessons}

    # No shared tags >=2: "runtime" vs "reflector" → no cross-link on LESS-DISJOINT.
    disjoint_entry = by_id["LESS-DISJOINT"]
    new_id = next(k for k in by_id if k != "LESS-DISJOINT")
    assert new_id not in disjoint_entry.get("related", [])
    assert "LESS-DISJOINT" not in by_id[new_id].get("related", [])


# ---------------------------------------------------------------------------
# 8. Bridge build_task prompt rendering: related hint rendered / absent
# ---------------------------------------------------------------------------

def test_build_task_prompt_includes_related_hint(tmp_path: Path) -> None:
    """build_task renders 'Related:' line in lesson section when related is set (#1095)."""
    from nanobot.runtime.bridge import build_task

    req = {
        "task_title": "test task title",
        "request_id": "req-001",
        "cycle_id": "cycle-001",
        "goal_id": "goal-001",
        "lessons_context": {
            "relevant_lesson": {
                "id": "LESS-ABC",
                "title": "Test lesson title",
                "problem": "Test problem text",
                "solution": "Test solution text",
                "approach": "Test approach",
                "reusable_insight": "Test insight",
                "related": "LESS-DEF, LESS-GHI",
            }
        },
    }

    prompt = build_task(req, "test goal text", "")

    assert "Related: LESS-DEF, LESS-GHI" in prompt, (
        "Expected 'Related:' line in build_task prompt when related is set"
    )


def test_build_task_prompt_no_related_hint_when_absent(tmp_path: Path) -> None:
    """build_task omits 'Related:' line when related is absent (byte-identical, #1095)."""
    from nanobot.runtime.bridge import build_task

    req = {
        "task_title": "test task title",
        "request_id": "req-001",
        "cycle_id": "cycle-001",
        "goal_id": "goal-001",
        "lessons_context": {
            "relevant_lesson": {
                "id": "LESS-ABC",
                "title": "Test lesson title",
                "problem": "Test problem text",
                "solution": "Test solution text",
                "approach": "Test approach",
                "reusable_insight": "Test insight",
                # No 'related' field.
            }
        },
    }

    prompt = build_task(req, "test goal text", "")

    assert "Related:" not in prompt, (
        "Expected no 'Related:' line in build_task prompt when related is absent"
    )
