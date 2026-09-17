"""An `accepted` ADR has a test behind every item of its Test Contract (#1700).

`docs/adr/README.md` ("Acceptance") says a record moves `proposed -> accepted`
only when every item of its `# Test Contract` names a test under `tests/` that
exists and cites the record (`ADR-NNN` appears in the test file), or carries the
marker `deferred (#NNN)`. Eleven records were proposed in three days with no
procedure to accept them and four with no test citing them; this module makes
the procedure a check rather than a convention.

Scope, so the first run is not red on eleven records (the #1593 pattern):

- `proposed` records are never failed. Their coverage is printed, per record.
- A record without a `# Test Contract` section is exempt and skipped by name.
- Records accepted before the procedure existed (`_ACCEPTED_BEFORE_PROCEDURE`)
  are run through the same resolver; an unmapped item there is an expected
  failure, visible in the report, not a red suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ADR_DIR = REPO / "docs" / "adr"
README = ADR_DIR / "README.md"

STATUSES = frozenset({"proposed", "accepted", "superseded"})

# Accepted under the old README, which had no procedure. Listed by name in the
# README's Acceptance section; grow this set only by operator decision.
_ACCEPTED_BEFORE_PROCEDURE = frozenset({"ADR-002", "ADR-004", "ADR-008", "ADR-009"})

_RE_ID = re.compile(r"^(ADR-\d{3})-")
_RE_STATUS = re.compile(r"^status:\s*(\S+)", re.MULTILINE)
_RE_CONTRACT_HEADING = re.compile(r"^(#{1,6})\s+Test Contract\s*$", re.MULTILINE)
_RE_HEADING = re.compile(r"^(#{1,6})\s+\S")
_RE_DEFERRED = re.compile(r"\bdeferred \(#(\d+)\)")
_RE_TEST_REF = re.compile(r"tests/[\w./-]+\.py(?:::\w+)*")
_RE_TABLE_SEP = re.compile(r"^\|\s*:?-{3,}")
# | [ADR-007](ADR-007-....md) | Title | proposed |
_RE_INDEX_ROW = re.compile(r"^\|\s*\[(ADR-\d{3})\]\([^)]+\)\s*\|[^|]*\|\s*(\w+)\s*\|", re.MULTILINE)


@dataclass
class Record:
    adr_id: str
    path: Path
    status: str
    contract: list[str] | None  # None: no `# Test Contract` section


@dataclass
class Resolution:
    item: str
    ok: bool
    why: str
    deferred_issue: int | None = None
    refs: list[str] = field(default_factory=list)


def _frontmatter_status(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    m = _RE_STATUS.search(block)
    return m.group(1) if m else ""


def _contract_items(text: str) -> list[str] | None:
    """Bullets and table body rows under `# Test Contract`, up to the next
    heading of the same or a shallower level. Any heading level is accepted:
    ADR-007 uses `##`, the others `#`."""
    m = _RE_CONTRACT_HEADING.search(text)
    if m is None:
        return None
    level = len(m.group(1))
    items: list[str] = []
    seen_table_header = False
    for line in text[m.end() :].splitlines():
        h = _RE_HEADING.match(line)
        if h and len(h.group(1)) <= level:
            break
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            if _RE_TABLE_SEP.match(stripped):
                continue
            if not seen_table_header:
                seen_table_header = True  # the `| Claim | Test | Status |` row
                continue
            items.append(stripped)
        elif stripped[0] in "-*" or re.match(r"^\d+\.\s", stripped):
            items.append(stripped.lstrip("-* ").strip())
    return items


def _parse(path: Path) -> Record:
    text = path.read_text(encoding="utf-8")
    m = _RE_ID.match(path.name)
    assert m, f"ADR file name does not start with ADR-NNN-: {path.name}"
    return Record(m.group(1), path, _frontmatter_status(text), _contract_items(text))


def _records() -> list[Record]:
    return [_parse(p) for p in sorted(ADR_DIR.glob("ADR-*.md"))]


_SELF = Path(__file__).resolve()


def _cites(test_file: Path, adr_id: str) -> bool:
    """This module names records in its own prose; it is never a citation."""
    if test_file.resolve() == _SELF:
        return False
    return re.search(rf"{adr_id}(?!\d)", test_file.read_text(encoding="utf-8")) is not None


def _names(test_file: Path, symbol: str) -> bool:
    return (
        re.search(
            rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(symbol)}\b",
            test_file.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        is not None
    )


def resolve(item: str, adr_id: str, repo: Path = REPO) -> Resolution:
    """One contract item is mapped when it is deferred to an issue, or when at
    least one `tests/...py[::name]` it names exists, cites `adr_id`, and (if a
    `::name` is given) defines that symbol."""
    d = _RE_DEFERRED.search(item)
    if d:
        return Resolution(item, True, f"deferred (#{d.group(1)})", int(d.group(1)))
    refs = _RE_TEST_REF.findall(item)
    if not refs:
        return Resolution(item, False, "names no tests/ path and is not deferred")
    problems = []
    for ref in refs:
        rel, *symbols = ref.split("::")
        p = repo / rel
        if not p.is_file():
            problems.append(f"{ref}: file missing")
        elif not _cites(p, adr_id):
            problems.append(f"{ref}: file does not cite {adr_id}")
        elif symbols and not _names(p, symbols[-1]):
            problems.append(f"{ref}: `{symbols[-1]}` not defined in file")
        else:
            return Resolution(item, True, f"mapped -> {ref}", refs=refs)
    return Resolution(item, False, "; ".join(problems), refs=refs)


def _coverage(rec: Record) -> list[Resolution]:
    return [resolve(item, rec.adr_id) for item in rec.contract or []]


def _short(s: str, n: int = 72) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


# --- the check ---------------------------------------------------------------

_RECORDS = _records()


def test_adr_directory_parses_at_all() -> None:
    """Guards every check below against a glob or regex that matches nothing."""
    assert len(_RECORDS) >= 21
    assert all(r.status for r in _RECORDS), [r.path.name for r in _RECORDS if not r.status]
    assert sum(1 for r in _RECORDS if r.contract) >= 15


@pytest.mark.parametrize("rec", _RECORDS, ids=lambda r: r.adr_id)
def test_status_is_one_declared_word(rec: Record) -> None:
    assert rec.status in STATUSES, f"{rec.adr_id}: status {rec.status!r} not in {sorted(STATUSES)}"


@pytest.mark.parametrize(
    "rec", [r for r in _RECORDS if r.status == "accepted"], ids=lambda r: r.adr_id
)
def test_accepted_record_has_a_test_behind_every_contract_item(rec: Record) -> None:
    """Rule 1 of the Acceptance section, enforced on the record's own text."""
    if rec.contract is None:
        pytest.skip(f"{rec.adr_id}: no `# Test Contract` section; exempt from rule 1")
    unmapped = [r for r in _coverage(rec) if not r.ok]
    detail = "\n".join(f"  - {_short(r.item)}\n      {r.why}" for r in unmapped)
    if unmapped and rec.adr_id in _ACCEPTED_BEFORE_PROCEDURE:
        pytest.xfail(
            f"{rec.adr_id} accepted before the procedure existed; "
            f"{len(unmapped)}/{len(rec.contract)} contract items unmapped:\n{detail}"
        )
    assert not unmapped, (
        f"{rec.adr_id} is `accepted` but {len(unmapped)}/{len(rec.contract)} "
        f"contract items map to no test citing it:\n{detail}"
    )


def test_proposed_records_report_contract_coverage() -> None:
    """Never fails on a `proposed` record; prints the per-record ratio the issue
    counted by hand, so the repository produces the number (run with -rA/-s)."""
    lines = ["", "ADR contract coverage (proposed records):"]
    for rec in _RECORDS:
        if rec.status != "proposed":
            continue
        if rec.contract is None:
            lines.append(f"  {rec.adr_id}: no Test Contract section")
            continue
        cov = _coverage(rec)
        mapped = sum(1 for r in cov if r.ok and r.deferred_issue is None)
        deferred = sum(1 for r in cov if r.deferred_issue is not None)
        citing = sorted(p.name for p in (REPO / "tests").glob("test_*.py") if _cites(p, rec.adr_id))
        lines.append(
            f"  {rec.adr_id}: {mapped}/{len(cov)} items mapped, {deferred} deferred; "
            f"{len(citing)} test file(s) cite it: {', '.join(citing) or '-'}"
        )
    print("\n".join(lines))
    assert len(lines) > 2


def test_index_status_matches_frontmatter() -> None:
    """The README table cannot say `accepted` while the file says `proposed`."""
    rows = dict(_RE_INDEX_ROW.findall(README.read_text(encoding="utf-8")))
    assert len(rows) >= 21, "index parser matched too few rows"
    by_id = {r.adr_id: r.status for r in _RECORDS}
    drift = [
        f"{adr}: index says {idx!r}, frontmatter says {by_id.get(adr)!r}"
        for adr, idx in rows.items()
        if by_id.get(adr) != idx
    ]
    assert not drift, "\n".join(drift)


def test_readme_states_the_acceptance_procedure() -> None:
    text = README.read_text(encoding="utf-8")
    assert "## Acceptance" in text
    for phrase in (
        "`proposed`",
        "`accepted`",
        "`superseded`",
        "`deferred (#NNN)`",
        "operator",
        "`# Test Contract`",
        "tests/test_adr_acceptance.py",
    ):
        assert phrase in text, f"Acceptance section lacks {phrase!r}"
    for adr in sorted(_ACCEPTED_BEFORE_PROCEDURE):
        assert adr.removeprefix("ADR-") in text.split("## Acceptance", 1)[1], (
            f"{adr} is in _ACCEPTED_BEFORE_PROCEDURE but the README does not name it"
        )


# --- the resolver on a synthetic record, so the real-file run is not its own test


def test_resolver_on_a_fixture_record(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_cites.py").write_text(
        "# ADR-099\nclass TestX:\n    def test_a(self):\n        pass\n", encoding="utf-8"
    )
    (tests / "test_silent.py").write_text("def test_b():\n    pass\n", encoding="utf-8")

    ok = resolve("Covered by `tests/test_cites.py::TestX::test_a`.", "ADR-099", tmp_path)
    assert ok.ok and ok.why.startswith("mapped")
    bare = resolve("| claim | `tests/test_cites.py` | passing |", "ADR-099", tmp_path)
    assert bare.ok
    defer = resolve("A kill mid-segment resumes; deferred (#1234).", "ADR-099", tmp_path)
    assert defer.ok and defer.deferred_issue == 1234
    prose = resolve("A test asserts thresholds are unchanged.", "ADR-099", tmp_path)
    assert not prose.ok and "names no tests/ path" in prose.why
    silent = resolve("see tests/test_silent.py", "ADR-099", tmp_path)
    assert not silent.ok and "does not cite ADR-099" in silent.why
    wrong_symbol = resolve("`tests/test_cites.py::test_zzz`", "ADR-099", tmp_path)
    assert not wrong_symbol.ok and "not defined" in wrong_symbol.why
    # ADR-099 must not match ADR-0990
    (tests / "test_near.py").write_text("# ADR-0990\n", encoding="utf-8")
    assert not resolve("tests/test_near.py", "ADR-099", tmp_path).ok

    text = (
        "---\nstatus: accepted\n---\n# Status\n\n## Test Contract\n\n"
        "| Claim | Test | Status |\n|---|---|---|\n| c1 | `tests/a.py` | passing |\n\n"
        "- bullet one\n- bullet two\n\n## Next\n\n- not a contract item\n"
    )
    assert _frontmatter_status(text) == "accepted"
    assert _contract_items(text) == ["| c1 | `tests/a.py` | passing |", "bullet one", "bullet two"]
    assert _contract_items("---\nstatus: proposed\n---\n# Decision\n") is None
