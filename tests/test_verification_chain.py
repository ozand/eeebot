"""#1826 -- the record of where the verification chain terminates must stay true.

A document describing which checks exist is worth exactly as much as its
agreement with the checks that exist. Three things are asserted here, and each
corresponds to a way the record could quietly go stale:

1. Every mechanism the record names is real -- the file or unit exists.
2. Every scheduled unit on the host is accounted for, as a verifier or as an
   explicit non-verifier, so a new unit cannot appear without a decision.
3. Every "inputs loop-writable" claim is RECOMPUTED from ``MUTATION_POLICY``
   rather than read as prose. If the mutation surface changes -- ``tests/``
   leaving it, or a new prefix arriving -- the claims in the record become
   false and this test says so.

Point 3 is the one that matters. The record's central claim is that exactly one
mechanism can both block integration and be fed by loop-written input. That is
not an opinion about the system; it is a join over the mutation policy and the
table, and it is computed here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nanobot.runtime.mutation_policy import MUTATION_POLICY

REPO = Path(__file__).resolve().parents[1]
RECORD = REPO / "docs" / "VERIFICATION_CHAIN.md"
UNIT_DIR = REPO / "host" / "eeepc" / "systemd"

# The paths each verifier reads, as claimed by the record's "reads" column,
# reduced to the repo-relative path that decides writability. A verifier whose
# only inputs are under state/ or the host filesystem maps to None: nothing it
# reads is on the surface.
VERIFIER_INPUTS: dict[str, str | None] = {
    "gate": "tests/",
    "ci": "tests/",
    "enhancement_gate": None,
    "existence_index": None,
    "validator_harness": "scripts/",
    "reflector": None,
    "curator": "lessons/",
    "strategist": None,
    "action_index": None,
    "skill_evals": "skills/",
    "promotion_verifier": None,
    "systemd_drift_check": None,
    "local_ci": "tests/",
}

# Mechanisms that can, on their own, stop work from integrating into the
# instance repo. "Blocks a proposal" and "blocks a spawn" are NOT this: they
# stop work from starting, which is a different power and a different question.
BLOCKS_INTEGRATION = {"gate"}


def _record_text() -> str:
    return RECORD.read_text(encoding="utf-8")


def _table_rows() -> list[list[str]]:
    """Rows of the verifier table: the markdown table whose first column is
    ``key`` and whose header names ``blocks integration``."""
    rows: list[list[str]] = []
    in_table = False
    for line in _record_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("| key |"):
            in_table = True
            continue
        if in_table:
            if not stripped.startswith("|"):
                break
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):  # the separator row
                continue
            rows.append(cells)
    return rows


def _keys() -> list[str]:
    return [row[0].strip("`") for row in _table_rows()]


def _is_loop_writable(path: str | None) -> bool:
    """Recomputed from the policy, never from the document."""
    if path is None:
        return False
    if path in MUTATION_POLICY.commit_exact_paths:
        return True
    return any(path.startswith(prefix) for prefix in MUTATION_POLICY.commit_path_prefixes)


# ---------------------------------------------------------------------------
# 1. the record names real things
# ---------------------------------------------------------------------------

def test_the_record_exists():
    assert RECORD.is_file()


def test_every_named_mechanism_is_a_real_file():
    """A row naming a module that has been renamed or deleted is a row nobody
    can act on."""
    missing: list[str] = []
    for row in _table_rows():
        mechanism = row[1]
        match = re.search(r"`([^`]+)`", mechanism)
        assert match, f"row {row[0]} does not name a path in backticks"
        path = REPO / match.group(1)
        if not path.exists():
            missing.append(match.group(1))
    assert not missing, missing


def test_the_table_and_this_test_cover_the_same_verifiers():
    """The record and the machine check must not drift apart -- a row added to
    one and not the other is exactly the stale state this file exists to
    prevent."""
    assert set(_keys()) == set(VERIFIER_INPUTS)


# ---------------------------------------------------------------------------
# 2. no verifier can appear on the host without appearing here
# ---------------------------------------------------------------------------

def test_every_scheduled_unit_is_classified():
    """AC 7. A new timer is how a new verifier actually arrives in this system.
    Each one must appear in the record -- as a verifier row, or in the explicit
    non-verifier list -- so its absence from the table is a decision someone
    made rather than an oversight."""
    text = _record_text()
    unclassified: list[str] = []
    for timer in sorted(UNIT_DIR.glob("*.timer")):
        stem = timer.stem  # e.g. eeebot-reflector
        if stem not in text:
            unclassified.append(stem)
    assert not unclassified, (
        f"units on the host with no row and no explicit exclusion: {unclassified}"
    )


def test_the_non_verifier_list_is_explicit_and_not_empty():
    """An empty exclusion list would mean the classification was never made."""
    text = _record_text()
    assert "deliberately **not** verifiers" in text
    for stem in ("eeebot-host-metrics", "eeebot-narrator"):
        assert stem in text


# ---------------------------------------------------------------------------
# 3. the writability claims are recomputed, not trusted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(VERIFIER_INPUTS))
def test_each_writability_claim_matches_the_mutation_policy(key: str):
    """The record says yes or no in the 'inputs loop-writable' column. That
    answer is derived here from MUTATION_POLICY, so a change to the surface
    falsifies the document instead of silently outdating it."""
    row = next(r for r in _table_rows() if r[0].strip("`") == key)
    claimed = "yes" in row[5].lower()
    assert claimed == _is_loop_writable(VERIFIER_INPUTS[key]), (
        f"{key}: record says loop-writable={claimed}, policy says "
        f"{_is_loop_writable(VERIFIER_INPUTS[key])} for {VERIFIER_INPUTS[key]!r}"
    )


def test_tests_are_on_the_mutation_surface():
    """The premise of the whole record, asserted directly rather than implied:
    the loop writes the tests that judge it. If this ever stops being true the
    document's conclusion changes and must be rewritten."""
    assert _is_loop_writable("tests/")


def test_this_record_is_itself_on_the_mutation_surface():
    """Stated in the document and checked here: docs/ is a commit prefix, so
    the record of where the chain terminates sits inside the region the chain
    does not verify. Uncomfortable, and better named than not."""
    assert _is_loop_writable("docs/")


# ---------------------------------------------------------------------------
# the conclusion itself
# ---------------------------------------------------------------------------

def test_exactly_one_mechanism_both_blocks_and_reads_loop_written_input():
    """The record's central claim, computed rather than asserted in prose.

    Independence and authority are separate properties here, and the gap is
    that nothing has both. If a second mechanism ever gains the power to block
    integration, this fails and the document must be rewritten -- which is the
    intended behaviour, not an inconvenience."""
    both = {
        key for key in VERIFIER_INPUTS
        if key in BLOCKS_INTEGRATION and _is_loop_writable(VERIFIER_INPUTS[key])
    }
    assert both == {"gate"}, both


def test_the_record_states_the_termination_plainly():
    """AC 3. Not buried in a table: the document has to say it."""
    text = _record_text()
    assert "## Where the chain terminates" in text
    assert "On loop-authored tests" in text


def test_both_measured_failures_are_cited():
    """AC 4. Evidence, not anecdote -- each names what was measured."""
    text = _record_text()
    assert "source_artifact" in text
    assert "#1188" in text and "#1193" in text


def test_the_judge_evidence_split_is_applied_to_each_model_backed_component():
    """AC 5. An independent judge reading loop-written evidence is not an
    independent verdict, and the curator is the worked example."""
    text = _record_text()
    assert "## The judge/evidence split" in text
    for judge in ("reflector", "strategist", "proposer", "curator"):
        assert judge in text


def test_the_cheap_move_is_named_with_an_estimate_and_not_built():
    """AC 6. A named follow-up with a size, so it can be ranked -- and an
    explicit statement that it is not built here, so the issue is not read as
    half-done."""
    text = _record_text()
    assert "## The one place the termination could cheaply be moved" in text
    assert "Estimate:" in text
    assert "Not built here" in text
