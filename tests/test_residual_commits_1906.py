"""Tests for issue #1906: residual auto-commit metadata and exclusion from git log readers."""
import subprocess
from pathlib import Path

from nanobot.runtime import bridge, llm_proposer


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "Test Agent"], cwd=repo)
    subprocess.check_call(["git", "config", "user.email", "agent@eeepc.local"], cwd=repo)
    subprocess.check_call(["git", "config", "commit.gpgsign", "false"], cwd=repo)
    init_file = repo / "README.md"
    init_file.write_text("init\n")
    subprocess.check_call(["git", "add", "README.md"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=repo)
    return repo


def test_auto_commit_subject_names_paths_and_body_has_attempted_outcome(tmp_path: Path):
    """Subject names residual paths, body names attempted task, outcome, and trailer."""
    repo = _init_repo(tmp_path)
    branch = "selfevo/cycle-test-1"
    subprocess.check_call(["git", "checkout", "-b", branch], cwd=repo)

    # 1. Single file residual commit
    mem = repo / "memory"
    mem.mkdir()
    (mem / "confirmation_status.json").write_text('{"status": "ok"}\n')

    res = bridge._auto_commit_uncommitted_work(
        repo,
        branch,
        backlog_title="Add deduplication cooldown filter to scripts/filter_futile_fallbacks.py",
    )
    assert res["committed"] is True
    assert res["files_committed"] == 1

    subject = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=repo, text=True).strip()
    assert subject == "selfevo: auto-commit residual state — memory/confirmation_status.json"
    assert "filter_futile_fallbacks" not in subject

    body = subprocess.check_output(["git", "log", "-1", "--format=%b"], cwd=repo, text=True)
    assert "attempted: Add deduplication cooldown filter to scripts/filter_futile_fallbacks.py" in body
    assert "outcome: incomplete" in body
    assert "Selfevo-Residual: true" in body

    # Verify git trailer parsing
    trailer = subprocess.check_output(
        ["git", "log", "-1", "--format=%(trailers:key=Selfevo-Residual,valueonly=true)"],
        cwd=repo,
        text=True,
    ).strip()
    assert trailer == "true"


def test_auto_commit_subject_formatting_multiple_and_many_paths(tmp_path: Path):
    """Subject lists multiple paths when short, or counts paths when exceeding limit."""
    repo = _init_repo(tmp_path)
    branch = "selfevo/cycle-test-2"
    subprocess.check_call(["git", "checkout", "-b", branch], cwd=repo)

    # 2 files: comma separated
    (repo / "f1.txt").write_text("1\n")
    (repo / "f2.txt").write_text("2\n")
    res = bridge._auto_commit_uncommitted_work(repo, branch, task_snippet="task foo")
    assert res["committed"] is True
    subj = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=repo, text=True).strip()
    assert subj == "selfevo: auto-commit residual state — f1.txt, f2.txt"

    # > 3 files: formatted as N paths
    for i in range(5):
        (repo / f"extra_{i}.txt").write_text(f"{i}\n")
    res2 = bridge._auto_commit_uncommitted_work(repo, branch, task_snippet="task bar")
    assert res2["committed"] is True
    subj2 = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=repo, text=True).strip()
    assert subj2 == "selfevo: auto-commit residual state — 5 paths"


def test_self_dedup_ignores_legacy_uncommitted_auto_commits_exact_b7e76119(tmp_path: Path):
    """Case b7e76119: legacy auto-commits with 'selfevo: auto-commit uncommitted subagent work'
    and NO trailer must be ignored by self_dedup and recent-activity so they do not poison the pool."""
    repo = _init_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()

    # Exact legacy shape from production (b7e76119): NO trailer, old subject prefix
    mem = repo / "memory"
    mem.mkdir()
    (mem / "confirmation_status.json").write_text("{}\n")
    subprocess.check_call(["git", "add", "."], cwd=repo)
    commit_msg = (
        "selfevo: auto-commit uncommitted subagent work — Add deduplication cooldown filter to scripts/filter_futile_fallbacks.py\n\n"
        "Subagent finished on selfevo/cycle-cycle-b7e76119 without running git commit; the bridge\n"
        "committed its working-tree changes so the smoke gate can evaluate them (#666).\n"
    )
    subprocess.check_call(["git", "commit", "-m", commit_msg], cwd=repo)

    # 2. A new proposal targeting scripts/filter_futile_fallbacks.py must NOT be rejected by self-dedup
    proposal = {
        "task_title": "Add deduplication cooldown filter to scripts/filter_futile_fallbacks.py",
        "target_path": "scripts/filter_futile_fallbacks.py",
    }
    dup, reason, detail = llm_proposer._is_duplicate_proposal(state, repo, proposal)
    assert dup is False
    assert "filter_futile_fallbacks" not in detail

    # 3. Recent activity context must not show this legacy commit
    ctx = bridge._recent_activity_context(state_dir=None, selfevo_repo_root=repo)
    assert "filter_futile_fallbacks" not in ctx
    assert "auto-commit uncommitted subagent work" not in ctx


def test_self_dedup_ignores_new_residual_commits_with_trailer(tmp_path: Path):
    """New residual format with trailer 'Selfevo-Residual: true' is ignored by self_dedup and recent-activity."""
    repo = _init_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()

    # New residual commit with trailer
    mem = repo / "memory"
    mem.mkdir()
    (mem / "confirmation_status.json").write_text("{}\n")
    subprocess.check_call(["git", "add", "."], cwd=repo)
    commit_msg = (
        "selfevo: auto-commit residual state — memory/confirmation_status.json\n\n"
        "Subagent finished on selfevo/cycle-cycle-test without running git commit; the bridge\n"
        "committed its working-tree changes so the smoke gate can evaluate them (#666, #1906).\n\n"
        "attempted: Add deduplication cooldown filter to scripts/filter_futile_fallbacks.py\n"
        "outcome: incomplete\n\n"
        "Selfevo-Residual: true\n"
    )
    subprocess.check_call(["git", "commit", "-m", commit_msg], cwd=repo)

    # 2. Proposal is not rejected by self-dedup
    proposal = {
        "task_title": "Add deduplication cooldown filter to scripts/filter_futile_fallbacks.py",
        "target_path": "scripts/filter_futile_fallbacks.py",
    }
    dup, reason, detail = llm_proposer._is_duplicate_proposal(state, repo, proposal)
    assert dup is False
    assert "filter_futile_fallbacks" not in detail

    # 3. Recent activity context must not show this residual commit
    ctx = bridge._recent_activity_context(state_dir=None, selfevo_repo_root=repo)
    assert "filter_futile_fallbacks" not in ctx
    assert "auto-commit residual state" not in ctx


def test_recent_activity_context_excludes_residual_commits(tmp_path: Path):
    """Recent activity (do not repeat) context excludes residual commits."""
    repo = _init_repo(tmp_path)

    # Real work commit
    work_file = repo / "scripts" / "tool.py"
    work_file.parent.mkdir()
    work_file.write_text("# tool\n")
    subprocess.check_call(["git", "add", "."], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "feat: implement tool.py"], cwd=repo)

    # Residual commit with trailer
    res_file = repo / "memory" / "status.json"
    res_file.parent.mkdir()
    res_file.write_text("{}\n")
    subprocess.check_call(["git", "add", "."], cwd=repo)
    residual_msg = (
        "selfevo: auto-commit residual state — memory/status.json\n\n"
        "attempted: Implement some helper\n"
        "outcome: incomplete\n\n"
        "Selfevo-Residual: true\n"
    )
    subprocess.check_call(["git", "commit", "-m", residual_msg], cwd=repo)

    ctx = bridge._recent_activity_context(state_dir=None, selfevo_repo_root=repo)
    assert "feat: implement tool.py" in ctx
    assert "selfevo: auto-commit residual state" not in ctx
    assert "Implement some helper" not in ctx
