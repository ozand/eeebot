"""#1219: a state path with readers and no writer must fail a test.

``state/research/`` had five readers for twelve days after its only writer was
deleted (#924). :mod:`nanobot.runtime.state_paths` declares the writer(s) of
every ``<state_dir>/<segment>`` the runtime reads; this module checks that the
declaration covers every read the scan can see and that every declared writer
still exists. The second check is the one that fires when a writer module is
deleted — it does not depend on the scan finding every reader.
"""
from __future__ import annotations

import importlib
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from nanobot.runtime import state_path_invocations, state_paths

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "nanobot"

# The greppable read forms. A floor, not a census: ``usage``, ``host_metrics``
# and ``validator_harness_parent`` reach state through other spellings and are
# not caught here — the registry may (and does) list more than this finds.
_READ_RE = re.compile(r"""\b(?:state_dir|state_root|STATE_DIR|state)\s*/\s*['"]([A-Za-z_]+)['"]""")


def scan_read_segments(package: Path = PACKAGE) -> dict[str, set[str]]:
    """First path segment -> files (relative to the package's parent) that read under it."""
    root = package.parent
    readers: dict[str, set[str]] = defaultdict(set)
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _READ_RE.finditer(text):
            readers[match.group(1)].add(path.relative_to(root).as_posix())
    return dict(readers)


def resolve_writer(ref: str, *, root: Path = ROOT, orphan_issues: dict[str, str] | None = None) -> str | None:
    """Return None when ``ref`` names something that exists, else the reason it does not."""
    orphan_issues = state_paths.ORPHAN_ISSUES if orphan_issues is None else orphan_issues
    if ref.startswith("repo:"):
        rel = ref[len("repo:"):]
        return None if (root / rel).exists() else f"{ref}: no such file in the repository"
    if ref.startswith("orphan:"):
        issue = ref[len("orphan:"):]
        return None if issue in orphan_issues else f"{ref}: issue not listed in ORPHAN_ISSUES"
    module_name, sep, attr = ref.partition(":")
    if not sep or not attr:
        return f"{ref!r}: expected '<module>:<attribute>', 'repo:<path>' or 'orphan:#<issue>'"
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # ImportError, or a module that fails at import
        return f"{ref}: module does not import ({exc.__class__.__name__})"
    if not hasattr(module, attr):
        return f"{ref}: module has no attribute {attr!r}"
    return None


def unwritten_segments(readers: dict[str, set[str]], registry: dict[str, tuple[str, ...]]) -> list[str]:
    """Human-readable line per read segment that declares no writer."""
    problems = []
    for segment in sorted(readers):
        writers = registry.get(segment)
        if not writers:
            files = ", ".join(sorted(readers[segment]))
            problems.append(f"state/{segment} is read by {files} and has no declared writer")
    return problems


# ─── the live registry ───────────────────────────────────────────────────────


def test_every_read_segment_declares_a_writer() -> None:
    readers = scan_read_segments()
    assert readers, "the scan found no state reads at all — the regex is broken, not the tree"
    assert unwritten_segments(readers, state_paths.STATE_PATH_WRITERS) == []


def test_every_declared_writer_still_exists() -> None:
    """The load-bearing half: a deleted writer module fails here even if the
    reader scan never saw its readers."""
    failures = [
        f"state/{segment}: {reason}"
        for segment, writers in sorted(state_paths.STATE_PATH_WRITERS.items())
        for ref in writers
        if (reason := resolve_writer(ref)) is not None
    ]
    assert failures == []


def test_registry_entries_are_non_empty_and_orphan_issues_are_explained() -> None:
    for segment, writers in state_paths.STATE_PATH_WRITERS.items():
        assert writers, f"state/{segment}: empty writer tuple is a silent exemption"
    for issue, reason in state_paths.ORPHAN_ISSUES.items():
        assert issue.startswith("#") and issue[1:].isdigit(), issue
        assert len(reason.split()) >= 4, f"{issue}: give the reason, not a label"
    used = {ref[len("orphan:"):] for w in state_paths.STATE_PATH_WRITERS.values() for ref in w if ref.startswith("orphan:")}
    assert used <= set(state_paths.ORPHAN_ISSUES)
    assert set(state_paths.ORPHAN_ISSUES) <= used, "an ORPHAN_ISSUES entry nothing references is stale"


def test_research_is_no_longer_read_anywhere() -> None:
    """The #1219 case itself: the frozen directory has left the read set."""
    assert "research" not in scan_read_segments()


# ─── the checker, against the failure it exists for ─────────────────────────


def test_the_deleted_planner_writer_would_fail_resolution() -> None:
    """#924's case: the module that wrote state/research/ no longer exists."""
    reason = resolve_writer("nanobot.runtime.cycle_planning:_write_research_feed")
    assert reason is not None and "does not import" in reason


def test_a_stale_attribute_on_a_live_module_fails_resolution() -> None:
    reason = resolve_writer("nanobot.runtime.existence_index:_reindex_hypotheses")
    assert reason is not None and "no attribute" in reason
    assert resolve_writer("nanobot.runtime.existence_index:reindex") is None


def test_unknown_reference_forms_and_untracked_orphans_are_rejected() -> None:
    assert "expected" in (resolve_writer("just a string") or "")
    assert "not listed" in (resolve_writer("orphan:#99999") or "")
    assert "no such file" in (resolve_writer("repo:docs/does-not-exist.md") or "")


def test_a_reader_with_no_declared_writer_is_named_with_its_files(tmp_path: Path) -> None:
    """A synthetic tree in the shape #1219 found: readers survive, the writer
    entry is gone. The failure message names the segment and every reader."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text('feed = state_dir / "research" / "feed.json"\n', encoding="utf-8")
    (pkg / "b.py").write_text("hyps = state_root / 'research' / 'hypotheses.json'\n", encoding="utf-8")
    (pkg / "c.py").write_text('rows = state_dir / "ledger" / "cycles.jsonl"\n', encoding="utf-8")

    readers = {seg: {Path(f).name for f in files} for seg, files in
               ((s, {str(p) for p in fs}) for s, fs in scan_read_segments(pkg).items())}
    assert set(readers) == {"research", "ledger"}
    assert readers["research"] == {"a.py", "b.py"}

    problems = unwritten_segments(scan_read_segments(pkg), {"ledger": ("nanobot.runtime.cycle_ledger:append_event",)})
    assert len(problems) == 1
    assert problems[0].startswith("state/research is read by ")
    assert "a.py" in problems[0] and "b.py" in problems[0] and "no declared writer" in problems[0]


@pytest.mark.parametrize("segment", ["ledger", "reflector", "subagents", "curator"])
def test_hot_directories_are_declared_by_real_writers_not_orphans(segment: str) -> None:
    writers = state_paths.STATE_PATH_WRITERS[segment]
    assert not any(ref.startswith("orphan:") for ref in writers)
    assert all(resolve_writer(ref) is None for ref in writers)


def test_writer_invocation_check_flags_disabled_strategist_timer_and_keeps_direct_ledger() -> None:
    output = """
        eeebot-strategist.timer                disabled enabled
        eeebot-self-evolving-subagent-bridge.timer enabled enabled
    """

    def runner(command: list[str]):
        assert command[0:3] == ["systemctl", "list-unit-files", "--type=timer"]
        return subprocess.CompletedProcess(command, 0, output, "")

    report = state_path_invocations.check_writer_invocations(runner)

    assert report["results"]["hypotheses"]["status"] == "disabled"
    assert report["results"]["ledger"]["status"] == "per_cycle"
    assert report["failures"] == ["hypotheses"]


def test_writer_invocation_check_accepts_enabled_strategist_timer() -> None:
    output = "eeebot-strategist.timer enabled enabled\n"

    def runner(command: list[str]):
        return subprocess.CompletedProcess(command, 0, output, "")

    report = state_path_invocations.check_writer_invocations(
        runner,
        {"hypotheses": state_path_invocations.WRITER_INVOKERS["hypotheses"]},
    )

    assert report["ok"] is True
    assert report["results"]["hypotheses"]["status"] == "scheduled"


def test_writer_invocation_check_distinguishes_absent_timer() -> None:
    def runner(command: list[str]):
        assert "list-timers" not in command
        return subprocess.CompletedProcess(command, 0, "", "")

    report = state_path_invocations.check_writer_invocations(
        runner,
        {"hypotheses": state_path_invocations.WRITER_INVOKERS["hypotheses"]},
    )

    assert report["ok"] is False
    assert report["results"]["hypotheses"]["status"] == "absent"
