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
    pairs = [lesson_v2.markdown_lesson_pair(tmp_path, entry) for entry in entries]
    reason = lesson_v2.mint_quality_reason(pairs[0], pairs[1:])
    assert reason["reason"] == "duplicate"
    assert reason["duplicate_id"] == entries[1]["id"]
    assert lesson_v2.mint_quality_reason(pairs[0], pairs[1:], extending=True) is None
    good = card("good")
    assert lesson_v2.mint_quality_reason(good, pairs) is None
    other = {**good, "title": pairs[0]["title"]}
    assert lesson_v2.mint_quality_reason(other, pairs) is None


def test_pair_requires_condition_and_action():
    original = card("good")
    same_title = {**original, "problem": "Socket authentication fails when credentials expire",
                  "solution": "Renew the credential and reauthenticate the connection"}
    assert lesson_v2.mint_quality_reason(same_title, [original]) is None
    same_action = {**same_title, "solution": original["solution"]}
    assert lesson_v2.mint_quality_reason(same_action, [original]) is None


def test_corrupt_duplicate_sources_fail_open(tmp_path):
    directory = tmp_path / "lessons"
    directory.mkdir()
    for name in ("lessons.yaml", "errors.yaml", "index.md"):
        (directory / name).write_text("[broken", encoding="utf-8")
    assert lesson_v2.allow_mint(card("good"), [], tmp_path / "state", workspace=tmp_path)
    (directory / "errors.yaml").write_bytes(b"x" * (lesson_v2._MAX_FILE_BYTES + 1))
    assert lesson_v2.allow_mint(card("good"), [], tmp_path / "state", workspace=tmp_path)


def test_real_live_reflector_paraphrased_duplicates():
    # Real incident 2026-09-07 00:09Z: 3 cards minted for the same 404 Gemini error.
    # When evaluated pairwise, the paraphrases share condition and action semantics.
    card_7c7 = {
        "id": "LESS-REF-7c7d36e5d201-5015",
        "title": "error_pattern",
        "problem": "Bridge sequence 1 terminated with 'litellm.NotFoundError: GeminiException - {\"detail\":\"Not Found\"}' for model 'an/gemini-3.7-flash-high' before any prompt was processed.",
        "solution": "Verify model availability and upstream litellm route configuration for 'an/gemini-3.7-flash-high', or fall back to an active default model when encountering 404 NotFound errors.",
    }
    card_e39 = {
        "id": "LESS-REF-e39ffed2e48f-fc53",
        "title": "error_pattern",
        "problem": "Bridge sequence 1 terminated with 'litellm.NotFoundError: GeminiException - {\"detail\":\"Not Found\"}' for model 'an/gemini-3.7-flash-high' before any prompt was processed.",
        "solution": "Verify upstream model endpoint configuration for 'an/gemini-3.7-flash-high' and consider falling back to a default active model when an escalated model identifier returns a 404 NotFoundError.",
    }
    card_dde = {
        "id": "LESS-REF-ddee2f247341-5c01",
        "title": "error_pattern",
        "problem": "Invoking 'an/gemini-3.7-flash-high' returned litellm.NotFoundError (GeminiException: Not Found) on sequence 1, terminating the cycle prematurely with zero files changed.",
        "solution": "Verify endpoint routing and deployment status for 'an/gemini-3.7-flash-high' prior to escalation, and introduce error-handling fallback to default models when an upstream route 404s.",
    }
    reason_e39 = lesson_v2.mint_quality_reason(card_e39, [card_7c7])
    assert reason_e39 is not None
    assert reason_e39["reason"] == "duplicate"
    assert reason_e39["duplicate_id"] == "LESS-REF-7c7d36e5d201-5015"

    reason_dde = lesson_v2.mint_quality_reason(card_dde, [card_e39])
    assert reason_dde is not None
    assert reason_dde["reason"] == "duplicate"
    assert reason_dde["duplicate_id"] == "LESS-REF-e39ffed2e48f-fc53"


def test_rejection_records_existing_decision_surface(tmp_path):
    import json
    assert not lesson_v2.allow_mint(card("tautology"), [], tmp_path)
    row = json.loads((tmp_path / "curator/decisions.jsonl").read_text().splitlines()[0])
    assert row["decision"] == "mint_rejected"
    assert row["reason"].startswith("tautology:")
