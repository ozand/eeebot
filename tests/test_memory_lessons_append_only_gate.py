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
role flag: the new append-only rule is wired ONLY into the executor's
cycle-branch gate (``bridge._changed_files_and_violations``, proven below
against that real function, the same way
``test_skill_hygiene_gate.py::test_bridge_changed_files_carries_skill_hygiene_into_mutation_violations``
proves the sibling check's wiring), so the curator's existing, unchanged
commit path is the honest proof of "not blocked".
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("status,paths", [
    ("R100", ("memory/a\t\nb.md", "docs/new.md")),
    ("C075", ("lessons/a\\b.md", "docs/copied.md")),
    ("M", ("memory/space and quote\\\".md",)),
    ("A", ("lessons/факт.md",)),
])
def test_name_status_z_parser_preserves_special_path_bytes(status, paths):
    raw = status.encode("ascii") + b"\0" + b"\0".join(
        path.encode("utf-8") for path in paths
    ) + b"\0"
    parsed = gate._parse_name_status_z(raw)
    assert parsed == [(status, *paths)]


@pytest.mark.parametrize("raw", [
    b"M\0docs/a",  # missing terminal NUL
    b"M\0\0",  # empty pathname
    b"Q\0docs/a\0",  # unsupported status
    b"R100\0docs/a\0",  # truncated rename record
    b"M\xff\0",  # pathname cannot be decoded on this platform
])
def test_name_status_z_parser_rejects_malformed_records(raw):
    assert gate._parse_name_status_z(raw) is None


@pytest.mark.parametrize("record", [
    b"M\0docs/a\0",
    b"R100\0memory/a\0docs/a\0",
    b"C075\0lessons/a\0docs/a\0",
    b"A\0lessons/new.md\0",
])
def test_name_status_z_parser_accepts_complete_record_shapes(record):
    parsed = gate._parse_name_status_z(record)
    assert parsed is not None
    assert len(parsed) == 1


def test_name_status_z_lookup_blocks_on_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gate.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, b"", b"failed"),
    )
    assert gate._git_name_status_z(tmp_path, "base") is None


def test_name_status_z_lookup_blocks_on_timeout(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(gate.subprocess, "run", timeout)
    assert gate._git_name_status_z(tmp_path, "base") is None


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


@pytest.mark.parametrize("source", [
    "memory/факт.md",
    "memory/space name.md",
    "memory/-leading.md",
    'memory/quote"name.md',
    "memory/tab\tname.md",
    "memory/newline\nname.md",
])
def test_special_name_rename_out_of_memory_is_rejected(tmp_path, source):
    if os.name == "nt" and any(char in source for char in "\\t\\n"):
        pytest.skip(f"Windows does not support pathname {source!r}")
    content = "same content padded out\nfor rename detection\n"
    repo, base = _repo_with(tmp_path, {source: content})
    (repo / source).unlink()
    _write(repo, "docs/new.md", content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unicode rename out")
    changed = _git(repo, "diff", "--name-only", base, "HEAD").stdout.splitlines()
    assert changed == ["docs/new.md"]
    assert _violations(repo, base, changed) == [
        f"memory_lessons_append_only: rename blocked under append-only surface: "
        f"{source} -> docs/new.md"
    ]


def test_rename_out_of_memory_is_rejected_even_though_name_only_shows_only_destination(tmp_path):
    repo, base = _repo_with(tmp_path, {"memory/facts/old.md": "same content padded out\\nfor rename detection\\n"})
    (repo / "memory/facts/old.md").unlink()
    _write(repo, "docs/new.md", "same content padded out\\nfor rename detection\\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename out")
    changed = [
        line for line in _git(repo, "diff", "--name-only", base, "HEAD").stdout.splitlines() if line.strip()
    ]
    assert changed == ["docs/new.md"]  # production shape: destination only
    assert _violations(repo, base, changed) == [
        "memory_lessons_append_only: rename blocked under append-only surface: "
        "memory/facts/old.md -> docs/new.md"
    ]


def test_rename_out_of_lessons_is_rejected_even_though_name_only_shows_only_destination(tmp_path):
    repo, base = _repo_with(tmp_path, {"lessons/old.md": "same content padded out\\nfor rename detection\\n"})
    (repo / "lessons/old.md").unlink()
    _write(repo, "docs/new.md", "same content padded out\\nfor rename detection\\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename out")
    changed = [
        line for line in _git(repo, "diff", "--name-only", base, "HEAD").stdout.splitlines() if line.strip()
    ]
    assert changed == ["docs/new.md"]  # production shape: destination only
    assert _violations(repo, base, changed) == [
        "memory_lessons_append_only: rename blocked under append-only surface: "
        "lessons/old.md -> docs/new.md"
    ]


def test_unavailable_full_status_blocks_destination_only_rename_out(tmp_path, monkeypatch):
    for prefix in ("memory", "lessons"):
        source = f"{prefix}/old.md"
        root = tmp_path / prefix
        root.mkdir()
        repo, base = _repo_with(root, {source: "same content padded out\\nfor rename detection\\n"})
        destination = "docs/new.md"
        (repo / source).unlink()
        _write(repo, destination, "same content padded out\\nfor rename detection\\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "rename out")
        changed = [destination]
        monkeypatch.setattr(gate, "_git_name_status_z", lambda *_: None)
        assert _violations(repo, base, changed) == [
            f"memory_lessons_append_only: cannot read diff status {base[:12]}..HEAD — protected paths cannot be verified"
        ]
        monkeypatch.undo()


def test_unavailable_full_status_blocks_even_when_changed_paths_look_unrelated(tmp_path, monkeypatch):
    repo, base = _repo_with(tmp_path, {})
    changed = ["docs/unrelated.md"]
    monkeypatch.setattr(gate, "_git_name_status_z", lambda *_: None)
    assert _violations(repo, base, changed) == [
        f"memory_lessons_append_only: cannot read diff status {base[:12]}..HEAD — protected paths cannot be verified"
    ]


def test_available_empty_full_status_allows_unrelated_changes(tmp_path, monkeypatch):
    repo, base = _repo_with(tmp_path, {})
    monkeypatch.setattr(gate, "_git_name_status_z", lambda *_: [])
    assert _violations(repo, base, ["docs/unrelated.md"]) == []


def test_available_unrelated_full_status_allows_unrelated_changes(tmp_path, monkeypatch):
    repo, base = _repo_with(tmp_path, {})
    monkeypatch.setattr(gate, "_git_name_status_z", lambda *_: [("A", "docs/unrelated.md")])
    assert _violations(repo, base, ["docs/unrelated.md"]) == []


def test_unavailable_binary_status_blocks_even_when_changed_paths_look_unrelated(tmp_path, monkeypatch):
    repo, base = _repo_with(tmp_path, {})
    monkeypatch.setattr(gate, "_git_name_status_z", lambda *_: None)
    assert _violations(repo, base, ["docs/new.md"]) == [
        f"memory_lessons_append_only: cannot read diff status {base[:12]}..HEAD — protected paths cannot be verified"
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


def test_unreadable_existing_history_baseline_blocks(tmp_path, monkeypatch):
    repo, base = _repo_with(tmp_path, {"memory/HISTORY.md": "first entry\n"})
    changed = _cycle_commit(repo, base, {"memory/HISTORY.md": "first entry\nsecond\n"})
    monkeypatch.setattr(gate, "_git_show_many", lambda *_: {"memory/HISTORY.md": None})
    assert _violations(repo, base, changed) == [
        f"memory_lessons_append_only: baseline unreadable at {base[:12]}: memory/HISTORY.md"
    ]


def test_unavailable_history_baseline_blocks(tmp_path, monkeypatch):
    repo, base = _repo_with(tmp_path, {"memory/HISTORY.md": "first entry\n"})
    changed = _cycle_commit(repo, base, {"memory/HISTORY.md": "first entry\nsecond\n"})
    original_git_lines = gate._git_lines

    def fail_baseline_lookup(root, args):
        if args[:1] == ["ls-tree"]:
            return None
        return original_git_lines(root, args)

    monkeypatch.setattr(gate, "_git_lines", fail_baseline_lookup)
    assert _violations(repo, base, changed) == [
        f"memory_lessons_append_only: baseline unavailable at {base[:12]}: memory/HISTORY.md"
    ]


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


# ─── bridge wiring: production actually reaches the check ───────────────────


def test_bridge_changed_files_carries_append_only_violation_into_mutation(tmp_path):
    """Not just "the function works" (proven above) -- that the executor's
    real cycle-branch gate decision (``bridge._changed_files_and_violations``,
    the function ``_evaluate_candidate`` actually calls to decide whether to
    integrate) carries this violation through. Same shape as
    ``test_skill_hygiene_gate.py``'s equivalent wiring test for the sibling
    check."""
    from nanobot.runtime import bridge

    repo, base = _repo_with(tmp_path, {"memory/facts/x.md": "a fact\n"})
    changed = _cycle_commit(repo, base, {"memory/facts/x.md": None})

    files, blocked, mutation, tier = bridge._changed_files_and_violations(repo, base)
    assert files == changed
    assert blocked == []
    assert tier == "script"
    assert any(v.startswith("memory_lessons_append_only:") for v in mutation), mutation
