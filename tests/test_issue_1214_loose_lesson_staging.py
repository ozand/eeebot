"""#1214: loose lesson migration uses the durable #1209 staging path."""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime.bridge import _pickup_staged_promotions
from nanobot.runtime.demand import classify_change_tier
from nanobot.runtime.knowledge_curator import load_staged_manifest, migrate_loose_lessons


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _repo_with_loose_notes(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "Test")
    (repo / "lessons").mkdir()
    (repo / "memory").mkdir()
    (repo / "memory" / "index.md").write_text("# Index\n", encoding="utf-8")
    (repo / "lessons" / "lessons.yaml").write_text("lessons: []\n", encoding="utf-8")
    (repo / "lessons" / "alpha.md").write_text("A durable insight\n", encoding="utf-8")
    (repo / "lessons" / "beta.md").write_text("A durable insight\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed loose lessons")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    return repo, origin


def _cycle_start_reset(repo: Path) -> None:
    _git(repo, "reset", "--hard")
    _git(repo, "clean", "-fd")
    _git(repo, "checkout", "main")


def test_staged_loose_migration_survives_cycle_reset_and_is_idempotent(tmp_path: Path) -> None:
    repo, _origin = _repo_with_loose_notes(tmp_path)
    state = tmp_path / "state"

    result = migrate_loose_lessons(repo, state)

    assert result["ok"] is True
    assert result["migrated"] == 2
    assert result["facts_created"] == 1
    manifest = load_staged_manifest(state)
    assert len(manifest) == 3
    assert sum(1 for entry in manifest if entry.get("kind") == "loose_lesson") == 2
    assert sum(1 for entry in manifest if entry.get("kind") != "loose_lesson") == 1
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "lessons" / "alpha.md").exists()

    _cycle_start_reset(repo)
    assert len(load_staged_manifest(state)) == 3
    assert _pickup_staged_promotions(repo, state) == 3

    assert _git(repo, "show", "origin/main:lessons/archive/loose/alpha.md") == "A durable insight"
    assert _git(repo, "show", "origin/main:lessons/archive/loose/beta.md") == "A durable insight"
    assert _git(repo, "show", "origin/main:memory/facts/alpha.md").startswith("# alpha")
    assert load_staged_manifest(state) == []

    second = migrate_loose_lessons(repo, state)
    assert second == {"ok": True, "migrated": 0, "facts_created": 0, "staged": []}
    assert load_staged_manifest(state) == []


def test_loose_lesson_batch_is_doc_only_but_does_not_change_budget_classifier() -> None:
    paths = [f"lessons/archive/loose/note-{index}.md" for index in range(37)]
    assert classify_change_tier(paths) == "doc-only"
