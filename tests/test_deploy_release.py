import os
import subprocess
from pathlib import Path
import pytest
import shutil

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "host" / "eeepc" / "scripts" / "deploy_release.sh"


def _git(*args, cwd=None, **kwargs):
    """Run git with an identity supplied by the test.

    CI runners have no global user.name / user.email, so `git commit` exits 128
    there while passing on any developer machine that has them configured. The
    fixtures below commit into throwaway repositories, so carry the identity in
    the environment rather than depending on the host's git configuration.
    """
    env = dict(kwargs.pop("env", None) or os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "eeebot tests")
    env.setdefault("GIT_AUTHOR_EMAIL", "tests@eeebot.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "eeebot tests")
    env.setdefault("GIT_COMMITTER_EMAIL", "tests@eeebot.invalid")
    return subprocess.run(["git", *args], cwd=cwd, env=env, **kwargs)


@pytest.fixture
def repo(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    script_dir = repo_root / "host" / "eeepc" / "scripts"
    script_dir.mkdir(parents=True)
    # Copy the script from the repository under test. An absolute path to one
    # author's worktree passed only on that machine and turned CI red on main.
    test_script = script_dir / "deploy_release.sh"
    shutil.copy(DEPLOY_SCRIPT, test_script)
    
    _git("init", cwd=repo_root, check=True)
    (repo_root / "README").write_text("hello")
    _git("add", ".", cwd=repo_root, check=True)
    _git("commit", "-m", "init", cwd=repo_root, check=True)
    
    return repo_root

def _write_mock(path, body):
    """Write an executable stub onto the mock PATH.

    The execute bit is not optional. Without it a POSIX shell skips the stub
    and falls through to the real binary on PATH, so the tests reached the
    actual ssh/scp on CI ("could not resolve hostname eeepc") while passing on
    Windows, where executability is not consulted the same way.
    """
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def mock_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_mock(bin_dir / "scp", "exit 0")
    _write_mock(bin_dir / "sleep", "exit 0")
    # Default ssh stub, so a test that never calls set_ssh_mock still cannot
    # reach a real host. Tests that need behaviour overwrite it.
    _write_mock(bin_dir / "ssh", "exit 0")
    return bin_dir

def run_deploy(repo_root, mock_bin, args):
    env = os.environ.copy()
    env["PATH"] = str(mock_bin).replace('\\', '/') + ":" + env["PATH"]
    script = repo_root / "host" / "eeepc" / "scripts" / "deploy_release.sh"
    res = subprocess.run(
        ["bash", str(script)] + args,
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True
    )
    return res

def set_ssh_mock(mock_bin, script_content):
    _write_mock(mock_bin / "ssh", script_content)

def test_a_local_head_not_origin_main(repo, tmp_path, mock_bin):
    _git("branch", "-M", "main", cwd=repo, check=True)
    _git("clone", "--bare", str(repo), str(tmp_path / "origin.git"), check=True)
    _git("remote", "add", "origin", str(tmp_path / "origin.git"), cwd=repo, check=True)
    _git("fetch", "origin", cwd=repo, check=True)
    
    (repo / "file").write_text("diverge")
    _git("add", "file", cwd=repo, check=True)
    _git("commit", "-m", "diverge", cwd=repo, check=True)
    
    res = run_deploy(repo, mock_bin, [])
    assert res.returncode != 0
    assert "refuse" in (res.stdout + res.stderr).lower()

def test_b_ref_deploys_exact_sha(repo, tmp_path, mock_bin):
    _git("branch", "-M", "main", cwd=repo, check=True)
    _git("clone", "--bare", str(repo), str(tmp_path / "origin.git"), check=True)
    _git("remote", "add", "origin", str(tmp_path / "origin.git"), cwd=repo, check=True)
    _git("fetch", "origin", cwd=repo, check=True)
    
    (repo / "file").write_text("diverge")
    _git("add", "file", cwd=repo, check=True)
    _git("commit", "-m", "diverge", cwd=repo, check=True)
    commit_sha = _git("rev-parse", "--short", "HEAD", cwd=repo, capture_output=True, text=True).stdout.strip()
    
    set_ssh_mock(mock_bin, "exit 0")
    res = run_deploy(repo, mock_bin, ["--ref", commit_sha, "--no-health-gate"])
    assert res.returncode == 0
    assert commit_sha in res.stdout

def test_c_traceback_triggers_rollback(repo, tmp_path, mock_bin):
    ssh_mock = """
    if [[ "$*" == *"| grep"* ]]; then
        if [[ "$*" == *"traceback|exception:|error:"* ]]; then
            echo "Traceback (most recent call last):"
            echo "Exception: crashed"
        fi
        exit 0
    elif [[ "$*" == *"sudo ln -sfn"* ]]; then
        echo "Rolling back"
        exit 0
    else
        exit 0
    fi
    """
    set_ssh_mock(mock_bin, ssh_mock)
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    assert res.returncode != 0
    assert "rollback" in res.stdout.lower() or "rollback" in res.stderr.lower()

def test_d_terminal_outcome_row_triggers_pass(repo, tmp_path, mock_bin):
    ssh_mock = """
    if [[ "$*" == *"| grep"* ]]; then
        if [[ "$*" == *"'\\"type\\": \\"outcome\\"'"* ]]; then
            echo '{"cycle_id": "c1", "phase": "outcome", "ts": "2099-01-01T00:00:00Z"}'
        fi
        exit 0
    else
        exit 0
    fi
    """
    set_ssh_mock(mock_bin, ssh_mock)
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    assert res.returncode == 0
    assert "health gate: pass" in (res.stdout + res.stderr).lower()

def test_e_timeout_returns_unknown(repo, tmp_path, mock_bin):
    ssh_mock = """
    exit 0
    """
    set_ssh_mock(mock_bin, ssh_mock)
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    assert res.returncode == 0
    assert "unknown" in (res.stdout + res.stderr).lower()
