from pathlib import Path

import yaml

from nanobot.runtime import lesson_v2

FIXTURES = Path(__file__).parent / "fixtures/lesson_quality"


def card(name):
    return yaml.safe_load((FIXTURES / f"{name}.yaml").read_text())[0]


def test_real_tautology_and_anecdote_are_separate():
    bad = card("tautology")
    assert lesson_v2.mint_quality_reason(bad)["reason"].startswith("tautology:")
    assert lesson_v2.anecdote_only(bad["problem"])


def test_real_good_lesson_passes():
    assert lesson_v2.mint_quality_reason(card("good")) is None


def test_real_markdown_twins_and_extension(tmp_path):
    from nanobot.runtime.lesson_index import generate_index, read_index
    directory = tmp_path / "lessons"
    directory.mkdir()
    for path in FIXTURES.glob("*.md"):
        (directory / path.name).write_bytes(path.read_bytes())
    generate_index(tmp_path)
    entries = read_index(directory / "index.md")
    assert len(entries) == 2
    reason = lesson_v2.mint_quality_reason(entries[0], entries[1:])
    assert reason["reason"] == "duplicate"
    assert reason["duplicate_id"] == entries[1]["id"]
    assert lesson_v2.mint_quality_reason(entries[0], entries[1:], extending=True) is None


def test_rejection_records_existing_decision_surface(tmp_path):
    import json
    assert not lesson_v2.allow_mint(card("tautology"), [], tmp_path)
    row = json.loads((tmp_path / "curator/decisions.jsonl").read_text().splitlines()[0])
    assert row["decision"] == "mint_rejected"
    assert row["reason"].startswith("tautology:")
