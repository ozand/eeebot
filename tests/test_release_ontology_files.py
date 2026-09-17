"""Release-root ontology files: SOUL.md, IDENTITY.md, USER.md (#1721, #1722).

These files are operator-owned prompt sources served read-only from the
release root. Each has a prompt budget so none can crowd out the others, and
each answers one question: IDENTITY.md says who the agent is (facts only),
SOUL.md says how it behaves, USER.md says what the operator requires.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

RELEASE_ROOT = Path(__file__).parents[1]

BUDGETS = {
    "SOUL.md": 1_800,
    "IDENTITY.md": 1_500,
    "USER.md": 4_000,
}

# Combined SOUL + IDENTITY may not exceed the pre-split IDENTITY.md (#1721):
# 2 810 chars as measured on 01d57773 (the issue quotes 2 834).
PRE_SPLIT_IDENTITY_CHARS = 2_810

# Words that mark a behavioural directive. IDENTITY.md carries facts about
# the machine only; every directive moved to SOUL.md or USER.md.
DIRECTIVE_WORDS = (" do not ", "never", "must", "prefer")

# SOUL.md holds voice, stance and boundaries; procedures belong to
# OPERATING.md (#1723).
PROCEDURE_WORDS = ("git ", "branch", "pytest", "iteration budget", "staging")

DIRECTIVE_LINE = re.compile(r"^- (Always|Never|Prefer)\b")
DATE = re.compile(r"\b2026-\d{2}-\d{2}\b")
SOURCE_REF = re.compile(r"(#\d+|ADR-\d{3})")


def _read(name: str) -> str:
    return (RELEASE_ROOT / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> list[str]:
    """Return the lines under ``## heading`` up to the next ``## `` heading."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == f"## {heading}")
    except StopIteration:
        pytest.fail(f"missing section '## {heading}'")
    body: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.startswith("## "):
            break
        body.append(ln)
    return body


def _has_directive_word(line: str) -> bool:
    padded = f" {line.lower()} "
    return any(word in padded for word in DIRECTIVE_WORDS)


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_release_file_exists_non_empty_and_within_budget(name: str):
    path = RELEASE_ROOT / name
    assert path.is_file(), f"{name} missing from the release root"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{name} is empty"
    assert len(text) <= BUDGETS[name], f"{name} is {len(text)} chars; budget {BUDGETS[name]}"


def test_soul_plus_identity_not_larger_than_pre_split_identity():
    combined = len(_read("SOUL.md")) + len(_read("IDENTITY.md"))
    assert combined <= PRE_SPLIT_IDENTITY_CHARS, combined


def test_identity_has_one_heading_and_no_directive_words():
    text = _read("IDENTITY.md")
    headings = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert len(headings) == 1, headings
    offenders = [ln for ln in text.splitlines() if _has_directive_word(ln)]
    assert offenders == [], offenders
    assert "This file defines who you are" not in text


def test_directive_word_check_catches_a_directive_line():
    """The matcher is only a guard if it fails on the sentences that moved out."""
    assert _has_directive_word("Do not imitate it.")
    assert _has_directive_word("Act autonomously; do not ask for clarification.")
    assert _has_directive_word("You will never out-compute the phone.")
    assert _has_directive_word("What the screen shows must be true.")
    assert _has_directive_word("prefer composing and extending them")
    assert not _has_directive_word("one slow core, two gigabytes, a 1024x600 screen")


def test_soul_has_the_five_sections_and_no_procedures():
    text = _read("SOUL.md")
    for heading in ("Stance", "Autonomy", "Ambition and scope", "Voice", "Boundaries"):
        assert _section(text, heading), f"SOUL.md section '{heading}' is empty"
    lowered = text.lower()
    offenders = [w for w in PROCEDURE_WORDS if w in lowered]
    assert offenders == [], offenders


def test_user_has_who_directives_superseded_and_priority_pointer():
    text = _read("USER.md")
    for heading in ("Who", "Directives", "Superseded"):
        _section(text, heading)
    assert "state/goals/goal_text.json" in text
    assert "## Directives" in text and text.index("## Who") < text.index("## Directives")


def test_user_directives_are_imperative_dated_and_sourced():
    items = [ln for ln in _section(_read("USER.md"), "Directives") if ln.startswith("- ")]
    assert items, "no directives listed"
    for item in items:
        assert DIRECTIVE_LINE.match(item), f"does not start with Always/Never/Prefer: {item}"
        assert DATE.search(item), f"no 2026- date: {item}"
        assert SOURCE_REF.search(item), f"no #issue or ADR- reference: {item}"
        assert item.count(". ") <= 1, f"more than one sentence: {item}"


def test_user_directive_shape_check_rejects_malformed_lines():
    assert not DIRECTIVE_LINE.match("- Do not read comments. (2026-09-15; ADR-015)")
    assert not DIRECTIVE_LINE.match("- Neverending story (2026-09-15; ADR-015)")
    assert DIRECTIVE_LINE.match("- Never read comments. (2026-09-15; ADR-015)")
    assert not SOURCE_REF.search("- Never read comments. (2026-09-15)")
    assert not DATE.search("- Never read comments. (ADR-015)")


def test_user_has_no_private_hostnames_or_credentials():
    lowered = _read("USER.md").lower()
    for token in ("192.168.", "100.", ".local", "ssh ", "token=", "api_key", "password"):
        assert token not in lowered, token
