"""#1384: ``_restore_to_main`` must verify the tree, not only the branch.

The outage this covers: ``skills/batch_grep/evals`` in the shared checkout was
owned by ``root`` while the bridge runs as ``eeepc-agent``, so ``git clean -fd``
could not unlink the file inside it and the leftover stayed untracked.
``_restore_to_main`` returned True anyway — it only checked ``HEAD == main`` —
the #680 precondition passed, and every cycle for 6h22m then died three lines
later at ``_setup_cycle_branch``'s own dirty check with a bare ``dirty_tree``
attributed to nothing.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nanobot.runtime import bridge
from tests.test_bridge_cycle_branch import _init_repo, _run


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX directory permissions cannot be arranged on Windows",
)
def test_restore_reports_a_tree_it_could_not_clean(tmp_path: Path):
    origin, work = _init_repo(tmp_path)

    # Reproduce the real mechanism: an untracked file inside a directory the
    # process cannot write into. `clean -fd` must unlink the file to remove the
    # directory, and cannot. Holding an open handle would NOT reproduce it —
    # POSIX unlinks open files happily, so such a test can only pass on Windows,
    # where the outage cannot occur in the first place.
    blocked = work / "blocked"
    blocked.mkdir()
    (blocked / "stuck.txt").write_text("junk")
    blocked.chmod(0o555)
    try:
        if os.access(blocked, os.W_OK):
            # Root ignores the permission bits, so the condition the outage
            # needed does not exist here. Skip rather than assert — a test that
            # cannot reproduce its own premise must say so, not report a verdict.
            pytest.skip("process can write into a 0555 directory (running as root)")
        res = bridge._restore_to_main(work)
        assert res is not True
        # `git status --porcelain` collapses an untracked directory into a
        # single entry — exactly what the live incident showed
        # (`?? skills/batch_grep/`), so assert the directory, not the file.
        assert "blocked/" in str(res)
    finally:
        blocked.chmod(0o755)


def test_restore_accepts_a_clean_tree(tmp_path: Path):
    origin, work = _init_repo(tmp_path)

    assert bridge._restore_to_main(work) is True


def test_restore_does_not_treat_ignored_files_as_dirty(tmp_path: Path):
    # `clean -fd` (no `-x`) keeps ignored files deliberately: #1381's rule
    # protecting the generated `lessons/index.md` depends on it. An ignored
    # file present is not a dirty tree, and reporting it as one would delete
    # that file on every restore all over again.
    origin, work = _init_repo(tmp_path)
    (work / ".gitignore").write_text("ignored.txt\n")
    _run(work, "add", ".gitignore")
    _run(work, "commit", "-m", "add gitignore")
    (work / "ignored.txt").write_text("junk")

    assert bridge._restore_to_main(work) is True
