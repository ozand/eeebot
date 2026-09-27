"""Replay the actual deploy heredoc locally to assert verify-only safety."""
from __future__ import annotations

import os
import shutil
import subprocess
import pytest
from pathlib import Path

from tests.test_deploy_release import DEPLOY_SCRIPT, _write_mock


@pytest.mark.parametrize(("candidate_source", "expected_rc", "expected_output"), [
    ("print('CANDIDATE_GATE_EXECUTED')\n", 0, "CANDIDATE_GATE_EXECUTED"),
    ("raise SystemExit(23)\n", 1, "VERIFY_ONLY HEALTH_FETCH_FAILED"),
])
def test_remote_verify_runs_candidate_and_never_mutates_units_or_current(
    tmp_path: Path, candidate_source: str, expected_rc: int, expected_output: str
) -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    remote = script.split("<<'REMOTE'", 1)[1].split("\nREMOTE", 1)[0]
    roots = {
        "/opt/eeepc-agent": str(tmp_path / "opt/eeepc-agent").replace("\\", "/"),
        "/var/lib/eeepc-agent": str(tmp_path / "var/lib/eeepc-agent").replace("\\", "/"),
        "/etc/systemd": str(tmp_path / "etc/systemd").replace("\\", "/"),
    }
    for old, new in roots.items():
        remote = remote.replace(old, new)
    live = Path(roots["/opt/eeepc-agent"]) / "runtimes/self-evolving-agent/current"
    live.mkdir(parents=True)
    gate = tmp_path / "candidate"
    gate.mkdir()
    (gate / "scripts").mkdir()
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    source_gate = source_repo / "scripts/verify_release_health.py"
    source_gate.parent.mkdir()
    source_gate.write_text("print('HEAD_GATE_EXECUTED')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source_repo)], check=True)
    subprocess.run(["git", "-C", str(source_repo), "add", "."], check=True)
    env_git = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
    subprocess.run(["git", "-C", str(source_repo), "commit", "-qm", "head gate"], check=True, env=env_git)
    head = subprocess.check_output(["git", "-C", str(source_repo), "rev-parse", "HEAD"], text=True).strip()
    source_gate.write_text(candidate_source, encoding="utf-8")
    subprocess.run(["git", "-C", str(source_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source_repo), "commit", "-qm", "candidate gate"], check=True, env=env_git)
    candidate = subprocess.check_output(["git", "-C", str(source_repo), "rev-parse", "HEAD"], text=True).strip()
    assert candidate != head
    archive = subprocess.Popen(["git", "-C", str(source_repo), "archive", "--format=tar", candidate, "scripts"], stdout=subprocess.PIPE)
    extract = subprocess.run(["tar", "-x", "-C", str(gate)], stdin=archive.stdout, check=False)
    archive.stdout.close()
    assert archive.wait() == 0 and extract.returncode == 0
    gate_script = gate / "scripts/verify_release_health.py"
    marker = tmp_path / "gate-ran"
    calls = tmp_path / "calls.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = str(calls).replace("\\", "/")
    _write_mock(bindir / "sudo", f'''echo "sudo $*" >> {log}
if [[ "$1" == "-u" ]]; then shift 2; fi
case "$1" in
  env) shift; while [[ "$1" == *=* ]]; do export "$1"; shift; done; exec "$@" ;;
  chown|chmod|mkdir|cp|install|tee|rmdir|ln|tar) exit 0 ;;
  systemctl) echo "sudo-systemctl $*" >> {log}; exit 0 ;;
  rm) shift; if [[ "$1" == "-n" ]]; then shift; fi; command rm "$@" ;;
  stat) echo 0:0 ;;
  *) echo "unexpected sudo command: $*" >&2; exit 97 ;;
esac
''')
    _write_mock(bindir / "systemctl", f'''echo "systemctl $*" >> {log}
case "$*" in
  *eeepc-network-fallback*"LoadState"*) echo not-found ;;
  *"-p LoadState"*) echo loaded ;;
  *"-p UnitFileState"*) echo enabled ;;
  *"is-enabled"*) echo enabled ;;
  *"-p MainPID"*) echo 0 ;;
  *"-p ExecMainStartTimestamp"*) echo now ;;
  *"is-active"*eeepc-network-fallback*) exit 1 ;;
  *"is-active"*eeebot-dashboard.service*) exit 1 ;;
  *"is-active"*) exit 0 ;;
  *) exit 0 ;;
esac
''')
    _write_mock(bindir / "python3", f'''echo "python3 $*" >> {log}
if [[ "$*" == *verify_release_health.py* ]]; then
  cat "$GATE_TMP/scripts/verify_release_health.py"
  python -c 'import runpy,sys; runpy.run_path(sys.argv[1], run_name="__main__")' "$GATE_TMP/scripts/verify_release_health.py"
else
  exit 0
fi
''')
    _write_mock(bindir / "stat", "echo 0:0")
    remote_script = tmp_path / "remote.sh"
    remote_script.write_text(remote, encoding="utf-8")
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], VERIFY_ONLY="1",
               FULL_COMMIT="candidate-sha", PREV_RELEASE_PATH=str(live),
               GATE_TMP=str(gate).replace("\\", "/"), RELEASE_DIR=str(live),
               HEALTH_GATE_PYTHON=str(bindir / "python3"))
    result = subprocess.run(["bash", str(remote_script)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == expected_rc, result.stdout + result.stderr
    assert expected_output in result.stdout + result.stderr
    assert not marker.exists()
    assert not gate.exists(), "the remote EXIT trap must remove the candidate gate tree"
    logged = calls.read_text(encoding="utf-8")
    assert "python3 " in logged and "verify_release_health.py" in logged
    assert not any(token in logged for token in (
        "systemctl restart", "systemctl stop", "systemctl start",
        "sudo-systemctl restart", "sudo-systemctl stop", "sudo-systemctl start",
        "ln -sfn", "sudo ln -sfn",
    )), logged
    assert not (live / "SOURCE_COMMIT").exists()
    units = Path(roots["/etc/systemd"]) / "system"
    assert not units.exists() or not any(units.iterdir())
