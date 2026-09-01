import os
import subprocess
from pathlib import Path
import pytest
import shutil

@pytest.fixture
def repo(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    
    script_dir = repo_root / "host" / "eeepc" / "scripts"
    script_dir.mkdir(parents=True)
    real_script = Path("T:/Code/eeebot-wt-1146/host/eeepc/scripts/deploy_release.sh")
    test_script = script_dir / "deploy_release.sh"
    shutil.copy(real_script, test_script)
    
    subprocess.run(["git", "init"], cwd=repo_root, check=True)
    (repo_root / "README").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_root, check=True)
    
    return repo_root

@pytest.fixture
def mock_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "scp").write_text("#!/usr/bin/env bash\nexit 0\n", newline="\n")
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", newline="\n")
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
    p = mock_bin / "ssh"
    p.write_text(f"#!/usr/bin/env bash\n{script_content}\n", newline="\n")
    p.chmod(0o755)

def test_a_local_head_not_origin_main(repo, tmp_path, mock_bin):
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    subprocess.run(["git", "clone", "--bare", str(repo), str(tmp_path / "origin.git")], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "origin.git")], cwd=repo, check=True)
    subprocess.run(["git", "fetch", "origin"], cwd=repo, check=True)
    
    (repo / "file").write_text("diverge")
    subprocess.run(["git", "add", "file"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "diverge"], cwd=repo, check=True)
    
    res = run_deploy(repo, mock_bin, [])
    assert res.returncode != 0
    assert "refuse" in (res.stdout + res.stderr).lower()

def test_b_ref_deploys_exact_sha(repo, tmp_path, mock_bin):
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    subprocess.run(["git", "clone", "--bare", str(repo), str(tmp_path / "origin.git")], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "origin.git")], cwd=repo, check=True)
    subprocess.run(["git", "fetch", "origin"], cwd=repo, check=True)
    
    (repo / "file").write_text("diverge")
    subprocess.run(["git", "add", "file"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "diverge"], cwd=repo, check=True)
    commit_sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    
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
            echo '{"cycle_id": "c1", "type": "outcome", "timestamp": "2099-01-01T00:00:00Z"}'
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
