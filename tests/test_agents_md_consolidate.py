"""#1313: #1188's unanswered half — something must remove, not only mark.

Marking a section droppable (#1300/#1302) buys reserve once but never bounds
the file; nothing else removes. ``scripts/agents_md_consolidate.py`` is the
minimal, explicit, operator-only removal path: it only removes ``## ``
sections named on the command line, and only if every one of them already
carries the exact ``<!-- prompt-fit: droppable -->`` marker. Dry-run is the
default; ``--apply`` is required to write, and the write is atomic.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder

SCRIPT_DIR = Path("scripts").resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

agents_md_consolidate = importlib.import_module("agents_md_consolidate")

MARK = ContextBuilder.DROPPABLE_MARKER


def _agents_md(tmp_path: Path) -> Path:
    content = (
        "# Instance AGENTS.md\n\n"
        "intro paragraph.\n\n"
        "## Working knowledge\n\n"
        "critical standing guidance.\n\n"
        f"## Big optional appendix\n\n{MARK}\n"
        "loses turns, not correctness, line one.\n"
        "loses turns, not correctness, line two.\n\n"
        f"## Small optional note\n\n{MARK}\n"
        "a short optional note.\n\n"
        "## Standard test runner\n\n"
        "critical, unmarked, sits last.\n"
    )
    path = tmp_path / "AGENTS.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_dry_run_is_the_default_and_leaves_the_file_untouched(tmp_path, capsys):
    path = _agents_md(tmp_path)
    original = path.read_text(encoding="utf-8")

    rc = agents_md_consolidate.main(["--file", str(path), "--section", "Big optional appendix"])

    assert rc == 0
    assert path.read_text(encoding="utf-8") == original, "dry-run must never write"
    out = capsys.readouterr().out
    assert "Would remove" in out and "Big optional appendix" in out
    assert "Dry run" in out and "--apply" in out
    assert not path.with_suffix(".md.tmp").exists()


def test_apply_removes_exactly_the_named_sections_and_round_trips(tmp_path, capsys):
    path = _agents_md(tmp_path)
    original = path.read_text(encoding="utf-8")

    rc = agents_md_consolidate.main([
        "--file", str(path),
        "--section", "## Big optional appendix",  # explicit prefix form also accepted
        "--section", "Small optional note",
        "--apply",
    ])

    assert rc == 0
    new_content = path.read_text(encoding="utf-8")
    assert "## Big optional appendix" not in new_content
    assert "## Small optional note" not in new_content
    assert "## Working knowledge" in new_content and "critical standing guidance" in new_content
    assert "## Standard test runner" in new_content and "critical, unmarked, sits last" in new_content

    # round-trip: every unit of the ORIGINAL is either still in new_content
    # (untouched, in order) or was one of the two named removals — nothing
    # else was touched, and nothing is lost (#1300's split contract: joining
    # every unit's text reconstructs the source exactly).
    original_units = ContextBuilder._split_bootstrap_sections(original)
    kept_units = [text for heading, text in original_units
                  if heading not in ("## Big optional appendix", "## Small optional note")]
    assert new_content == "".join(kept_units)

    out = capsys.readouterr().out
    assert "Removed" in out and "Big optional appendix" in out and "Small optional note" in out
    assert not path.with_suffix(".md.tmp").exists(), "atomic write must not leave a temp file behind"


def test_apply_preserves_a_symlink_and_updates_its_target(tmp_path):
    target = _agents_md(tmp_path)
    link = tmp_path / "linked-AGENTS.md"
    try:
        link.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    rc = agents_md_consolidate.main([
        "--file", str(link), "--section", "Big optional appendix", "--apply",
    ])

    assert rc == 0
    assert link.is_symlink(), "atomic replacement must update the target, not destroy the symlink"
    assert "## Big optional appendix" not in target.read_text(encoding="utf-8")


def test_refuses_a_critical_unmarked_section_and_writes_nothing(tmp_path, capsys):
    path = _agents_md(tmp_path)
    original = path.read_text(encoding="utf-8")

    rc = agents_md_consolidate.main([
        "--file", str(path),
        "--section", "Big optional appendix",
        "--section", "Standard test runner",  # critical, unmarked
        "--apply",
    ])

    assert rc != 0
    assert path.read_text(encoding="utf-8") == original, "an unmarked section in the request refuses the WHOLE run"
    err = capsys.readouterr().err
    assert "critical/unmarked" in err and "Standard test runner" in err


def test_refuses_a_section_that_does_not_exist(tmp_path, capsys):
    path = _agents_md(tmp_path)
    original = path.read_text(encoding="utf-8")

    rc = agents_md_consolidate.main(["--file", str(path), "--section", "Nonexistent heading", "--apply"])

    assert rc != 0
    assert path.read_text(encoding="utf-8") == original
    err = capsys.readouterr().err
    assert "not found" in err and "Nonexistent heading" in err


def test_refuses_with_no_sections_named(tmp_path):
    path = _agents_md(tmp_path)
    rc = agents_md_consolidate.main(["--file", str(path)])
    assert rc != 0


def test_refuses_a_missing_file(tmp_path):
    rc = agents_md_consolidate.main(["--file", str(tmp_path / "nope.md"), "--section", "X"])
    assert rc != 0


def test_droppable_reserve_chars_matches_what_remains(tmp_path):
    path = _agents_md(tmp_path)
    plan = agents_md_consolidate.plan_removal(
        path.read_text(encoding="utf-8"), ["## Big optional appendix"]
    )
    # "Small optional note" is still droppable and still present -> the reserve.
    assert plan["droppable_reserve_chars"] == len(f"## Small optional note\n\n{MARK}\na short optional note.\n\n")


def test_refuses_duplicate_heading_even_when_one_copy_is_marked(tmp_path):
    path = _agents_md(tmp_path)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original + f"\n## Big optional appendix\n\n{MARK}\nduplicate optional copy.\n",
        encoding="utf-8",
    )
    duplicated = path.read_text(encoding="utf-8")

    rc = agents_md_consolidate.main([
        "--file", str(path), "--section", "Big optional appendix", "--apply",
    ])

    assert rc != 0
    assert path.read_text(encoding="utf-8") == duplicated


def test_plan_removal_refuses_are_raised_not_swallowed(tmp_path):
    path = _agents_md(tmp_path)
    text = path.read_text(encoding="utf-8")
    with pytest.raises(agents_md_consolidate.ConsolidationRefusedError):
        agents_md_consolidate.plan_removal(text, ["## Standard test runner"])
    with pytest.raises(agents_md_consolidate.ConsolidationRefusedError):
        agents_md_consolidate.plan_removal(text, ["## Nope"])
