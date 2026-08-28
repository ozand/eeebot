"""Unit tests for cycle stop-guards (R12 revision outcome).

Pure-function tests — no coordinator import, so they run under the CI matrix
regardless of the heavy runtime dependencies.
"""
from nanobot.runtime.stop_guards import (
    REVISION_CAP_DEFAULT,
    revision_outcome,
)

# ── R12: bounded revisions ───────────────────────────────────────────────────

def test_revision_passed():
    rec = revision_outcome(revisions=1, smoke_passed=True, cap=3)
    assert rec["outcome"] == "passed"
    assert rec["capped"] is False
    assert rec["count"] == 1


def test_revision_cap_reached_blocks():
    rec = revision_outcome(revisions=3, smoke_passed=False, cap=3)
    assert rec["outcome"] == "blocked"
    assert rec["capped"] is True
    assert rec["max"] == 3
    assert rec["gate"] == "smoke"


def test_revision_under_cap_is_unresolved_not_blocked():
    rec = revision_outcome(revisions=1, smoke_passed=False, cap=3)
    assert rec["outcome"] == "unresolved"
    assert rec["capped"] is False


def test_default_constants():
    assert REVISION_CAP_DEFAULT == 3


def test_revision_outcome_omits_last_smoke_output_when_not_given():
    rec = revision_outcome(revisions=1, smoke_passed=True, cap=3)
    assert "last_smoke_output" not in rec


def test_revision_outcome_persists_last_smoke_output():
    """#668: the failed-gate output must land in the record for forensics."""
    rec = revision_outcome(
        revisions=3, smoke_passed=False, cap=3, last_smoke_output="FAILED tests/test_x.py",
    )
    assert rec["last_smoke_output"] == "FAILED tests/test_x.py"


def test_revision_outcome_truncates_last_smoke_output_to_2000_chars():
    long_output = 'x' * 3000 + 'TAIL'
    rec = revision_outcome(
        revisions=1, smoke_passed=False, cap=3, last_smoke_output=long_output,
    )
    assert len(rec["last_smoke_output"]) == 2000
    assert rec["last_smoke_output"].endswith('TAIL')
