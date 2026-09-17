"""#1724 / ADR-022: ``goals.md`` carries only what the executor can act on,
and the injected charter block carries exactly one top-level heading.

The bridge used to prepend ``# Immutable operator charter`` to the charter
text as a post-fit ``system_context`` tail for the executor; #1725 removed
that path — ``ContextBuilder(release_root=RELEASE_ROOT)`` now loads
``goals.md`` itself as an ADR-022 ontology block, inside the fit and its
telemetry. The literal survives in exactly one place, a fallback used only
when dumping the prompt for an old bridge-test double that doesn't
implement ``_build_subagent_prompt`` — see ``_BRIDGE_CONSTRUCTION``. The
file used to carry its own ``# eeebot operator charter`` heading, a preamble
about the sandbox path and issue numbers, and an IMPORTANT paragraph
restating the commit surface. None of that is an instruction the executor
can follow; the sandbox and ownership facts now live in
``docs/agents/charter-ownership.md`` and the commit rule is owned by the
operating instructions.

These tests keep the file in that shape.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nanobot.runtime.goal_review import read_charter_text

_REPO_ROOT = Path(__file__).parent.parent
_CHARTER = _REPO_ROOT / "goals.md"
_BRIDGE_SRC = _REPO_ROOT / "nanobot" / "runtime" / "bridge.py"

_BRIDGE_HEADING = "# Immutable operator charter"
# #1725: bridge.py no longer builds the executor's system_context this way
# at all — ContextBuilder(release_root=RELEASE_ROOT) loads goals.md as an
# ADR-022 ontology block, inside the fit and its telemetry. The ONE
# remaining site is the prompt-dump fallback for old bridge-test doubles
# that don't implement ``_build_subagent_prompt``.
_BRIDGE_CONSTRUCTION = "'# Immutable operator charter\\n\\n' + charter_text"

_LEVEL_ONE = re.compile(r"^# ", re.MULTILINE)
_OPERATOR_EXECUTED = "### Operator-executed"


@pytest.fixture(scope="module")
def charter() -> str:
    return _CHARTER.read_text(encoding="utf-8")


def test_file_has_no_level_one_heading(charter: str) -> None:
    """The bridge supplies the one heading; the file must not add a second."""
    found = _LEVEL_ONE.findall(charter)
    assert not found, (
        f"goals.md carries {len(found)} level-1 heading(s); the bridge "
        f"prepends '{_BRIDGE_HEADING}', so the injected block would show two."
    )


def test_preamble_is_one_sentence(charter: str) -> None:
    lines = charter.splitlines()
    assert lines, "goals.md is empty"
    assert lines[0] == (
        "> Immutable. Ships in the release tree; the gate rejects any cycle "
        "that touches it."
    ), "the preamble is no longer the single agreed sentence"
    assert not lines[1].startswith(">"), (
        "the preamble grew past one line; sandbox and ownership facts belong "
        "in docs/agents/charter-ownership.md"
    )


def test_mutation_surface_is_not_stated_in_the_charter(charter: str) -> None:
    """The commit rule is owned by the operating instructions, not the charter.

    ``state/`` is allowed in exactly one place: Vector 1's operator-executed
    subsection, which names the surfaces the loop cannot commit (ADR-012,
    ``test_charter_mutation_surface_agreement``). Everywhere else it is the
    mutation surface restated, and it must not be there.
    """
    assert "Only commit" not in charter
    assert "git-tracked" not in charter
    assert "state/goals/" not in charter

    lines = charter.splitlines()
    outside: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(_OPERATOR_EXECUTED):
            inside = True
            continue
        if inside and line.startswith("## "):
            inside = False
        if not inside:
            outside.append(line)
    leaked = [ln for ln in outside if "state/" in ln]
    assert not leaked, (
        f"`state/` appears outside the '{_OPERATOR_EXECUTED}' subsection: "
        f"{leaked}"
    )


def test_vectors_and_validity_rules_survive(charter: str) -> None:
    for literal in ("Vector 1", "Vector 2", "Validity rules"):
        assert literal in charter, f"goals.md lost its '{literal}' section"


def test_bridge_block_carries_exactly_one_top_level_heading() -> None:
    """Build the block the way bridge.py's prompt-dump fallback does (#1725:
    the only remaining site — the executor itself no longer gets this as a
    system_context tail, see ``_BRIDGE_CONSTRUCTION``'s docstring) and count
    its ``# `` lines."""
    src = _BRIDGE_SRC.read_text(encoding="utf-8")
    assert _BRIDGE_CONSTRUCTION in src, (
        "bridge.py no longer builds the charter block as "
        f"{_BRIDGE_CONSTRUCTION}; update this test to mirror the new shape"
    )

    charter_text = read_charter_text(_REPO_ROOT)
    assert charter_text, "read_charter_text returned '' for the repo root"
    block = _BRIDGE_HEADING + "\n\n" + charter_text

    assert block.startswith(_BRIDGE_HEADING + "\n\n")
    assert len(_LEVEL_ONE.findall(block)) == 1, (
        "the injected charter block carries more than one top-level heading"
    )
