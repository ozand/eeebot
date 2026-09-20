"""ADR-028 rules 2-3 (#1811): the diary's opening entry survives whatever
happens to the cycle branch.

Design note (architect review): this does NOT need a carve-out in the
integration step. #1811 asked for exactly what the curator's staged-pickup
mechanism (#1001, #1209) already solved for facts/lesson cards: write on
main, commit, push immediately, roll back on push failure, all at the safe
cycle-start boundary (bridge lock held, HEAD on clean main) BEFORE the
cycle branch is cut. Rules 2 and 3 then hold by construction rather than by
a carve-out or a measurement: there is no cycle branch, no gate, and no
code commit yet when :func:`bridge._write_diary_open_entry` runs, so there
is no verdict for the diary write to ride alongside or need protecting
from. These tests mirror the exact git sequence
``tests/test_issue_1209_durable_curator_writes.py`` uses to prove the same
property for staged pickup.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nanobot.runtime.bridge import _write_diary_open_entry
from nanobot.runtime.day_diary import DIARY_MARKER, diary_relpath


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _init_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout on ``main`` tracking a bare ``origin`` -- the instance repo's shape."""
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    return repo, origin


def _origin_main_show(repo: Path, rel: str) -> "str | None":
    _git(repo, "fetch", "origin", "main")
    proc = subprocess.run(["git", "-C", str(repo), "show", f"origin/main:{rel}"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def _ledger_rows(state: Path) -> list[dict]:
    path = state / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# happy path: write, commit, push
# ---------------------------------------------------------------------------


def test_diary_open_entry_creates_a_fresh_day_file_and_pushes_it(tmp_path: Path):
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    result = _write_diary_open_entry(repo, state, "cycle-1", "connect the orphaned validator")

    assert result["outcome"] == "integrated"
    relpath = diary_relpath()
    pushed = _origin_main_show(repo, relpath)
    assert pushed is not None
    assert "connect the orphaned validator" in pushed
    assert pushed.count(DIARY_MARKER) == 1
    assert (repo / relpath).read_text(encoding="utf-8") == pushed
    assert _git(repo, "status", "--porcelain") == ""


def test_diary_commit_is_path_scoped_to_diary_only(tmp_path: Path):
    """AC 1: a commit containing only diary/ changes."""
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    result = _write_diary_open_entry(repo, state, "cycle-1", "extend the demand ranker")

    files = _git(repo, "show", "--name-only", "--format=", result["commit_sha"]).splitlines()
    assert [f for f in files if f.strip()] == [diary_relpath()]


def test_second_cycle_same_day_appends_keeping_one_marker(tmp_path: Path):
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _write_diary_open_entry(repo, state, "cycle-1", "first cycle's intent")
    result = _write_diary_open_entry(repo, state, "cycle-2", "second cycle's intent")

    assert result["outcome"] == "integrated"
    content = _origin_main_show(repo, diary_relpath())
    assert content is not None
    assert "first cycle's intent" in content
    assert "second cycle's intent" in content
    assert content.index("first cycle's intent") < content.index("second cycle's intent")
    assert content.count(DIARY_MARKER) == 1


def test_missing_task_title_falls_back_to_a_placeholder(tmp_path: Path):
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    result = _write_diary_open_entry(repo, state, "cycle-1", "")
    assert result["outcome"] == "integrated"
    content = _origin_main_show(repo, diary_relpath())
    assert "(no task title)" in content


# ---------------------------------------------------------------------------
# AC 2/3/5: the diary survives whatever happens to the cycle branch,
# and the code verdict is unaffected by the diary riding "alongside" it.
# ---------------------------------------------------------------------------


def test_diary_survives_a_gate_rejected_cycle(tmp_path: Path):
    """AC 2 + AC 5: the diary entry is already on origin/main before the
    cycle branch is even cut, so a gate rejection of the cycle's own code
    cannot lose it -- and the rejected code never reaches origin/main
    either."""
    from nanobot.runtime.bridge import _restore_to_main, _setup_cycle_branch

    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    diary_result = _write_diary_open_entry(repo, state, "cycle-rejected", "attempting a risky refactor")
    assert diary_result["outcome"] == "integrated"

    setup = _setup_cycle_branch(repo, "cycle-rejected", state_dir=state)
    assert setup["ok"] is True
    (repo / "risky_change.py").write_text("BROKEN\n", encoding="utf-8")
    _git(repo, "add", "risky_change.py")
    _git(repo, "commit", "-m", "cycle: risky change")

    # The gate rejects: the bridge's own recovery on a bad cycle is
    # _restore_to_main (reset --hard + clean -fd + checkout main), never an
    # integration merge.
    restored = _restore_to_main(repo, state_dir=state)
    assert restored is True

    assert _origin_main_show(repo, "risky_change.py") is None, "rejected code must never reach origin/main"
    diary_after = _origin_main_show(repo, diary_relpath())
    assert diary_after is not None
    assert "attempting a risky refactor" in diary_after


def test_diary_survives_a_cycle_that_never_commits_any_code(tmp_path: Path):
    """AC 3: truncated / gateway-failed / dead-on-LLM-call cycles that never
    reach a commit at all still leave the diary entry intact, since the
    write happened before the subagent (and therefore before any of those
    failure modes) could even begin."""
    from nanobot.runtime.bridge import _restore_to_main, _setup_cycle_branch

    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    diary_result = _write_diary_open_entry(repo, state, "cycle-dead", "attempting to fix the flaky test")
    assert diary_result["outcome"] == "integrated"

    setup = _setup_cycle_branch(repo, "cycle-dead", state_dir=state)
    assert setup["ok"] is True
    # Subagent dies before touching the working tree at all -- nothing to
    # commit, nothing for the auto-commit safety net to pick up either.
    restored = _restore_to_main(repo, state_dir=state)
    assert restored is True

    diary_after = _origin_main_show(repo, diary_relpath())
    assert diary_after is not None
    assert "attempting to fix the flaky test" in diary_after


# ---------------------------------------------------------------------------
# AC 4: blocked-pattern refusal
# ---------------------------------------------------------------------------


def test_blocked_filename_refuses_without_writing_or_committing(tmp_path: Path, monkeypatch):
    from nanobot.runtime import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "_is_blocked_filename", lambda f: True)
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    before = _git(repo, "rev-parse", "HEAD")

    result = _write_diary_open_entry(repo, state, "cycle-1", "an entry that should never land")

    assert result["outcome"] == "refused"
    assert _git(repo, "rev-parse", "HEAD") == before
    assert not (repo / diary_relpath()).exists()
    rows = [r for r in _ledger_rows(state) if r["phase"] == "diary_open_entry"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["cycle_id"] == "cycle-1"


# ---------------------------------------------------------------------------
# push failure: rolled back, journalled, retried cleanly next time
# ---------------------------------------------------------------------------


def test_push_failure_rolls_back_and_journals_the_deferral(tmp_path: Path):
    repo, origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    before = _git(repo, "rev-parse", "HEAD")

    result = _write_diary_open_entry(repo, state, "cycle-1", "an entry that cannot reach origin")

    assert result["outcome"] == "push_failed"
    assert _git(repo, "rev-parse", "HEAD") == before, "the undurable commit must be dropped"
    assert _git(repo, "status", "--porcelain") == "", "a failed push must not leave main dirty"
    assert not (repo / diary_relpath()).exists()
    rows = [r for r in _ledger_rows(state) if r["phase"] == "diary_open_entry"]
    assert [r["outcome"] for r in rows] == ["push_failed"]
    assert rows[0]["reason"].startswith("push to origin/main failed:")

    # Restore the remote: the retry on the next cycle boundary succeeds and
    # is not blocked by anything the failed attempt left behind.
    _git(repo, "remote", "set-url", "origin", str(origin))
    result2 = _write_diary_open_entry(repo, state, "cycle-2", "the retried entry")
    assert result2["outcome"] == "integrated"
    content = _origin_main_show(repo, diary_relpath())
    assert "the retried entry" in content


def test_no_origin_remote_is_not_durable(tmp_path: Path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    state = tmp_path / "state"
    before = _git(repo, "rev-parse", "HEAD")

    result = _write_diary_open_entry(repo, state, "cycle-1", "an entry with no remote")

    assert result["outcome"] == "push_failed"
    assert _git(repo, "rev-parse", "HEAD") == before
    rows = [r for r in _ledger_rows(state) if r["phase"] == "diary_open_entry"]
    assert "no origin remote" in rows[0]["reason"]


# ---------------------------------------------------------------------------
# malformed diary file: refused, never a partial write
# ---------------------------------------------------------------------------


def test_malformed_diary_file_is_a_journalled_no_op_not_a_crash(tmp_path: Path):
    repo, _origin = _init_repo_with_origin(tmp_path)
    state = tmp_path / "state"
    relpath = diary_relpath()
    (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
    (repo / relpath).write_text("# Diary\n\nno marker at all here.\n", encoding="utf-8")
    _git(repo, "add", relpath)
    _git(repo, "commit", "-m", "malformed diary seed")
    _git(repo, "push", "origin", "main")
    before = _git(repo, "rev-parse", "HEAD")
    before_content = (repo / relpath).read_text(encoding="utf-8")

    result = _write_diary_open_entry(repo, state, "cycle-1", "an entry that cannot be appended")

    assert result["outcome"] == "malformed"
    assert _git(repo, "rev-parse", "HEAD") == before
    assert (repo / relpath).read_text(encoding="utf-8") == before_content
    rows = [r for r in _ledger_rows(state) if r["phase"] == "diary_open_entry"]
    assert [r["outcome"] for r in rows] == ["malformed"]


# ---------------------------------------------------------------------------
# AC 6: every attempt is journalled, none silently
# ---------------------------------------------------------------------------


def test_every_outcome_kind_writes_exactly_one_journal_row(tmp_path: Path, monkeypatch):
    from nanobot.runtime import bridge as bridge_module
    from nanobot.runtime.cycle_ledger import VALID_DIARY_OPEN_OUTCOMES

    # integrated
    repo1, _o1 = _init_repo_with_origin(tmp_path / "a")
    state1 = tmp_path / "a-state"
    r1 = _write_diary_open_entry(repo1, state1, "c1", "intent 1")
    assert r1["outcome"] == "integrated"

    # refused
    repo2, _o2 = _init_repo_with_origin(tmp_path / "b")
    state2 = tmp_path / "b-state"
    monkeypatch.setattr(bridge_module, "_is_blocked_filename", lambda f: True)
    r2 = _write_diary_open_entry(repo2, state2, "c2", "intent 2")
    assert r2["outcome"] == "refused"
    monkeypatch.undo()

    # push_failed
    repo3, _o3 = _init_repo_with_origin(tmp_path / "c")
    state3 = tmp_path / "c-state"
    _git(repo3, "remote", "set-url", "origin", str(tmp_path / "gone2.git"))
    r3 = _write_diary_open_entry(repo3, state3, "c3", "intent 3")
    assert r3["outcome"] == "push_failed"

    for state, cycle_id, outcome in ((state1, "c1", "integrated"), (state2, "c2", "refused"), (state3, "c3", "push_failed")):
        rows = [r for r in _ledger_rows(state) if r["phase"] == "diary_open_entry"]
        assert len(rows) == 1, f"expected exactly one journal row for {cycle_id}, got {rows}"
        assert rows[0]["cycle_id"] == cycle_id
        assert rows[0]["outcome"] == outcome
        assert rows[0]["outcome"] in VALID_DIARY_OPEN_OUTCOMES


# ---------------------------------------------------------------------------
# AC 7 (replay/measurement): the write happens strictly before any cycle
# branch exists -- a structural pin, not a measured percentage, since the
# new design removes the loss scenario rather than reducing it.
# ---------------------------------------------------------------------------


def test_diary_write_call_site_precedes_cycle_branch_setup_in_source():
    """Pins the ordering the whole design leans on: bridge.py must call
    _write_diary_open_entry() before _setup_cycle_branch(), textually, in
    the main cycle path -- if a future edit reorders these, the diary write
    would start racing the cycle branch cut instead of strictly preceding
    it, and this test catches that before it ships."""
    src = Path(__import__("nanobot.runtime.bridge", fromlist=["__file__"]).__file__).read_text(encoding="utf-8")
    diary_idx = src.index("_write_diary_open_entry(\n")
    setup_idx = src.index("_cycle_setup = _setup_cycle_branch(")
    assert diary_idx < setup_idx, (
        "the diary open-entry write must precede the cycle branch setup call site"
    )
