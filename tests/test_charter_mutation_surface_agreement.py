"""#1598 / ADR-012: the charter names only what the loop can commit.

``goals.md`` Vector 1 used to name the harness — "its own code", dedicated
modules in Rust/C++/C — while ``mutation_policy._COMMIT_PATH_PREFIXES`` has
never allowed the loop to touch ``nanobot/``. Nothing compared the two, so
they drifted, and every Vector 1 measurement became a proxy for something
the loop structurally could not do.

These tests compare them. They are the reason the drift cannot recur
silently: a path added to the charter's object list must be a path the
mutation policy allows, and a path the policy forbids must not be presented
as loop work.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nanobot.runtime.mutation_policy import _COMMIT_PATH_PREFIXES

_CHARTER = Path(__file__).parent.parent / "goals.md"

# Paths the loop is forbidden to commit. Kept here as literals rather than
# derived from the policy: the point of the test is to catch a charter that
# starts naming one of them, which a derivation from the same source could
# not do.
_FORBIDDEN_PREFIXES = ("nanobot/", "systemd/", "ops/", "state/")

# The instance repository itself. `_COMMIT_PATH_PREFIXES` are prefixes *within*
# it, so the repo root is a container, not an object path — a different
# vocabulary, and comparing it against the policy would be a category error.
# It is required rather than merely tolerated: without it the charter would
# list seven bare prefixes and never say which repository they are in.
_REPO_ROOT = "eeebot-self-evolving/"

_BACKTICK_PATH = re.compile(r"`([A-Za-z0-9_./-]+/)`")


def _section(title_prefix: str) -> str:
    """The text of the ``## `` section whose title starts with the prefix,
    up to the next ``## `` heading (``### `` subsections are included)."""
    text = _CHARTER.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.startswith("## ") and line[3:].startswith(title_prefix):
            start = idx
            break
    assert start is not None, f"no '## {title_prefix}...' section in goals.md"
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("## "):
            end = idx
            break
    return "\n".join(lines[start:end])


@pytest.fixture(scope="module")
def vector_one() -> str:
    return _section("Vector 1")


def test_vector_one_object_paths_are_all_committable(vector_one: str) -> None:
    """Every path the charter presents as Vector 1's object is a path the
    mutation policy lets the loop commit."""
    quoted = set(_BACKTICK_PATH.findall(vector_one))
    # Non-vacuity: the charter must actually name its object surface. If the
    # extraction finds nothing, the assertion below would pass over an empty
    # set and this test would guard nothing.
    assert quoted, (
        "Vector 1 names no backticked path. The charter has to state which "
        "surfaces it is about, or this comparison is vacuous."
    )
    assert _REPO_ROOT in quoted, (
        f"Vector 1 lists object prefixes without naming `{_REPO_ROOT}`. The "
        "prefixes are meaningless unless the charter says which repository "
        "they are relative to."
    )
    allowed = set(_COMMIT_PATH_PREFIXES)
    named_as_object = {
        p for p in quoted
        if p not in _FORBIDDEN_PREFIXES and p != _REPO_ROOT
    }
    unknown = named_as_object - allowed
    assert not unknown, (
        f"goals.md Vector 1 names {sorted(unknown)} as the loop's object, but "
        f"mutation_policy._COMMIT_PATH_PREFIXES allows only {sorted(allowed)}. "
        "Either the path belongs in the policy or it does not belong in the "
        "charter's object list (ADR-012)."
    )


def test_forbidden_paths_appear_only_as_operator_executed(vector_one: str) -> None:
    """A forbidden path may still be mentioned — the charter has to explain
    that harness work exists — but only inside the subsection that marks it
    as the operator's to implement."""
    quoted = set(_BACKTICK_PATH.findall(vector_one))
    mentioned_forbidden = quoted & set(_FORBIDDEN_PREFIXES)
    if not mentioned_forbidden:
        pytest.skip("Vector 1 mentions no forbidden path")

    marker = "### Operator-executed"
    assert marker in vector_one, (
        f"Vector 1 mentions {sorted(mentioned_forbidden)}, which the loop "
        f"cannot commit, but carries no '{marker}' subsection saying so."
    )
    head, _, tail = vector_one.partition(marker)
    leaked = set(_BACKTICK_PATH.findall(head)) & set(_FORBIDDEN_PREFIXES)
    assert not leaked, (
        f"{sorted(leaked)} appears in Vector 1 before the "
        f"'{marker}' subsection, so it reads as loop work."
    )
    assert tail.strip(), f"'{marker}' subsection is empty"


def test_charter_states_the_measurement_requirement(vector_one: str) -> None:
    """The strictest clause in the charter is the one that survived the
    rewrite: an optimization claim carries a before/after measurement. It is
    also the clause the harness proposal route depends on."""
    # Match the imperative clause, not the phrase. "before/after measurement"
    # also appears in the operator-executed subsection describing what a
    # harness proposal must carry, so a presence check would survive deleting
    # the requirement itself — which is exactly what an earlier version of
    # this test did.
    requirement = re.search(
        r"must come with a before/after measurement", vector_one
    )
    assert requirement, (
        "Vector 1 no longer *requires* a before/after measurement for an "
        "optimization claim. That requirement predates this rewrite and is "
        "what makes a harness proposal countable (ADR-012). A passing "
        "mention of the phrase elsewhere in the section does not satisfy it."
    )
