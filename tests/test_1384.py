import pytest
from pathlib import Path
from nanobot.runtime import bridge
from tests.test_bridge_cycle_branch import _init_repo, _run

def test_restore_to_main_fails_if_tree_dirty(tmp_path: Path):
    origin, work = _init_repo(tmp_path)
    
    unremovable = work / "unremovable.txt"
    unremovable.write_text("junk")
    
    f = open(unremovable, "w")
    try:
        res = bridge._restore_to_main(work)
        # Verify it returns a falsy value and mentions the file
        assert res is not True
        assert "unremovable.txt" in str(res)
    finally:
        f.close()

def test_restore_to_main_ignores_ignored_files(tmp_path: Path):
    origin, work = _init_repo(tmp_path)
    
    gitignore = work / ".gitignore"
    gitignore.write_text("ignored.txt\n")
    _run(work, "add", ".gitignore")
    _run(work, "commit", "-m", "add gitignore")
    
    ignored = work / "ignored.txt"
    ignored.write_text("junk")
    
    res = bridge._restore_to_main(work)
    assert res is True
