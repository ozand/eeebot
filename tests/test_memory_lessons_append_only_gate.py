"""#1768 Part 1: the executor may add to ``memory/``/``lessons/``, never
delete, rename, or shrink ``memory/HISTORY.md``.

The executor is not the curator (#1188/#1193 precedent: the loop already
edited its own standing context in its own favour once). These tests drive
:func:`gate._memory_lessons_append_only_violations` against real git repos —
a base commit, then a cycle commit — with the changed-file list taken from
``git diff --name-only``, exactly what the bridge feeds it (same pattern as
``tests/test_skill_hygiene_gate.py``).

Part 2 (the curator's own path is not blocked) is proven here against the
REAL function the curator's commit path already uses in production
(``gate._validate_mutation_surfaces``, called by
``bridge._pickup_staged_promotions`` — never
``_changed_files_and_violations``/this new check) rather than a fabricated
role flag: the new append-only rule is wired only into the executor's
cycle-branch gate (see the PR body for the exact one-line bridge.py call
site, skipped here per the #1765 file boundary), so the curator's existing,
unchanged commit path is the honest proof of "not blocked".
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime import gate


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo_with(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    """Init a repo whose base commit holds *files* (path -> text)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "gate@test.local")
    _git(repo, "config", "user.name", "gate-test")
    _write(repo, "README.md", "base\n")
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _cycle_commit(repo: Path, base_sha: str, writes: dict[str, str | None]) -> list[str]:
    """Apply *writes* (path -> text, None = delete) as one cycle commit.

    Returns what the bridge feeds the gate: ``git diff --name-only base HEAD``
    (a rename shows only its destination — the production shape).
    """
    for rel, text in writes.items():
        if text is None:
            (repo / rel).unlink()
        else:
            _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "cycle")
    out = _git(repo, "diff", "--name-only", base_sha, "HEAD").stdout
    return [line for line in out.splitlines() if line.strip()]


def _violations(repo: Path, base_sha: str, changed: list[str]) -> list[str]:
    return gate._memory_lessons_append_only_violations(repo, base_sha, changed)


# ─── delete / rename: structural, git status only ────────────────────────────


def test_deleting_a_memory_fact_is_rejected(tmp_path):
    repo, base = _repo_with(tmp_path, {"memory/facts/x.md": "a fact\n"})
    changed = _cycle_commit(repo, base, {"memory/facts/x.md": None})
    out = _violations(repo, base, changed)
    assert out == ["memory_lessons_append_only: delete blocked under append-only surface: memory/facts/x.md"]


def test_deleting_a_lesson_card_is_rejected(tmp_path):
    repo, base = _repo_with(tmp_path, {"lessons/some-card.md": "card\n"})
    changed = _cycle_commit(repo, base, {"lessons/some-card.md": None})
    out = _violations(repo, base, changed)
    assert out == ["memory_lessons_append_only: delete blocked under append-only surface: lessons/some-card.md"]


def test_renaming_a_memory_fact_is_rejected(tmp_path):
    repo, base = _repo_with(tmp_path, {"memory/facts/old.md": "same content padded out\nfor rename detection\n"})
    (repo / "memory/facts/old.md").unlink()
    _write(repo, "memory/facts/new.md", "same content padded out\nfor rename detection\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename")
    changed = [
        line for line in _git(repo, "diff", "--name-only", base, "HEAD").stdout.splitlines() if line.strip()
    ]
    out = _violations(repo, base, changed)
    assert out == [
        "memory_lessons_append_only: rename blocked under append-only surface: "
        "memory/facts/old.md -> memory/facts/new.md"
    ]


def test_rename_into_memory_is_caught_even_though_name_only_shows_only_the_destination(tmp_path):
    """``git diff --name-only`` (what ``changed_files`` is) lists only a
    rename's destination — the unscoped full-diff-status read this function
    does must still catch a rename whose SOURCE was outside memory/lessons."""
    repo, base = _repo_with(tmp_path, {"docs/old.md": "same content padded out\nfor rename detection\n"})
    (repo / "docs/old.md").unlink()
    _write(repo, "memory/facts/new.md", "same content padded out\nfor rename detection\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename in")
    changed = [
        line for line in _git(repo, "diff", "--name-only", base, "HEAD").stdout.splitlines() if line.strip()
    ]
    assert changed == ["memory/facts/new.md"]  # production shape: destination only
    out = _violations(repo, base, changed)
    assert out == [
        "memory_lessons_append_only: rename blocked under append-only surface: "
        "docs/old.md -> memory/facts/new.md"
    ]


def test_deleting_outside_memory_or_lessons_is_not_this_checks_concern(tmp_path):
    repo, base = _repo_with(tmp_path, {"docs/x.md": "doc\n"})
    changed = _cycle_commit(repo, base, {"docs/x.md": None})
    assert _violations(repo, base, changed) == []


# ─── additions: the append-only surface still accepts new work ──────────────


def test_adding_a_new_memory_fact_passes(tmp_path):
    repo, base = _repo_with(tmp_path, {})
    changed = _cycle_commit(repo, base, {"memory/facts/new.md": "a new fact\n"})
    assert _violations(repo, base, changed) == []


def test_adding_a_new_lesson_card_passes(tmp_path):
    repo, base = _repo_with(tmp_path, {})
    changed = _cycle_commit(repo, base, {"lessons/new-card.md": "card body\n"})
    assert _violations(repo, base, changed) == []


def test_editing_an_existing_fact_without_deleting_or_renaming_passes(tmp_path):
    repo, base = _repo_with(tmp_path, {"memory/facts/x.md": "line one\n"})
    changed = _cycle_commit(repo, base, {"memory/facts/x.md": "line one\nline two\n"})
    assert _violations(repo, base, changed) == []


# ─── memory/HISTORY.md: no existing line may vanish ──────────────────────────


def test_history_md_line_removal_is_rejected(tmp_path):
    repo, base = _repo_with(tmp_path, {"memory/HISTORY.md": "[2026-09-01 00:00] first entry\n"})
    changed = _cycle_commit(repo, base, {"memory/HISTORY.md": ""})
    out = _violations(repo, base, changed)
    assert out == ["memory_lessons_append_only: 1 existing line(s) removed from memory/HISTORY.md"]


def test_history_md_line_edited_in_place_counts_as_removal(tmp_path):
    """Rewriting a line is a removal (of the old text) plus an add — the
    multiset check catches an edit the same way it catches a deletion; a
    line, once written, is immutable, not merely present-by-count."""
    repo, base = _repo_with(tmp_path, {"memory/HISTORY.md": "[2026-09-01 00:00] first entry\n"})
    changed = _cycle_commit(repo, base, {"memory/HISTORY.md": "[2026-09-01 00:00] EDITED entry\n"})
    out = _violations(repo, base, changed)
    assert out == ["memory_lessons_append_only: 1 existing line(s) removed from memory/HISTORY.md"]


def test_history_md_append_at_end_passes(tmp_path):
    repo, base = _repo_with(tmp_path, {"memory/HISTORY.md": "[2026-09-01 00:00] first entry\n"})
    changed = _cycle_commit(repo, base, {
        "memory/HISTORY.md": "[2026-09-01 00:00] first entry\n[2026-09-02 00:00] second entry\n",
    })
    assert _violations(repo, base, changed) == []


def test_history_md_insertion_that_preserves_every_existing_line_passes(tmp_path):
    """The rule is "no existing line disappears", not "only ever append at
    the end" — a new line inserted between two existing ones still keeps
    every original line present, so it passes."""
    repo, base = _repo_with(tmp_path, {
        "memory/HISTORY.md": "[2026-09-01 00:00] first entry\n[2026-09-03 00:00] third entry\n",
    })
    changed = _cycle_commit(repo, base, {
        "memory/HISTORY.md": (
            "[2026-09-01 00:00] first entry\n"
            "[2026-09-02 00:00] second entry\n"
            "[2026-09-03 00:00] third entry\n"
        ),
    })
    assert _violations(repo, base, changed) == []


def test_history_md_new_file_has_nothing_to_have_removed(tmp_path):
    repo, base = _repo_with(tmp_path, {})
    changed = _cycle_commit(repo, base, {"memory/HISTORY.md": "[2026-09-01 00:00] first entry\n"})
    assert _violations(repo, base, changed) == []


def test_history_md_untouched_this_cycle_is_not_checked(tmp_path):
    repo, base = _repo_with(tmp_path, {"memory/HISTORY.md": "[2026-09-01 00:00] first entry\n"})
    changed = _cycle_commit(repo, base, {"memory/facts/x.md": "unrelated add\n"})
    assert _violations(repo, base, changed) == []


# ─── fail-closed on an unreadable diff ────────────────────────────────────────


def test_unreadable_base_sha_fails_closed(tmp_path):
    repo, base = _repo_with(tmp_path, {"memory/facts/x.md": "a fact\n"})
    _cycle_commit(repo, base, {"memory/facts/x.md": None})
    out = gate._memory_lessons_append_only_violations(repo, "0" * 40, ["memory/facts/x.md"])
    assert len(out) == 1
    assert out[0].startswith("memory_lessons_append_only: cannot read diff status")


# ─── Part 2: the curator's real commit path is not blocked ──────────────────


def test_curator_path_validate_mutation_surfaces_does_not_block_a_delete():
    """``bridge._pickup_staged_promotions`` (the curator's own commit path)
    validates only via ``_validate_mutation_surfaces`` — never this new
    check, never ``_classify_mutation_surface``. Proven directly against
    that real function: a delete under memory/lessons, which the new
    executor-only check above rejects, passes it unchanged."""
    changed = ["memory/facts/old-superseded.md", "lessons/duplicate-card.md"]
    assert gate._validate_mutation_surfaces(changed) == []


def test_executor_path_blocks_what_curator_path_allows(tmp_path):
    """Both directions in one test: the same delete is rejected by the new
    executor-only check and accepted by the curator's real, unchanged
    commit-surface check."""
    repo, base = _repo_with(tmp_path, {"memory/facts/x.md": "a fact\n"})
    changed = _cycle_commit(repo, base, {"memory/facts/x.md": None})

    executor_violations = gate._memory_lessons_append_only_violations(repo, base, changed)
    curator_violations = gate._validate_mutation_surfaces(changed)

    assert executor_violations != []
    assert curator_violations == []
