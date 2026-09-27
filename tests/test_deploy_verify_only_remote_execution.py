"""Replay candidate staging and remote verify-only behavior in a sandbox."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_deploy_release import DEPLOY_SCRIPT, _write_mock


def _extract_remote(script: str) -> str:
    return script.split("<<'REMOTE'", 1)[1].split("\nREMOTE", 1)[0]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True, env=env)
    return result.stdout.strip()


def _run_remote_gate(tmp_path: Path, gate_source: str) -> subprocess.CompletedProcess[str]:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    remote = _extract_remote(script)
    roots = {"/opt/eeepc-agent": str(tmp_path / "opt/eeepc-agent").replace("\\", "/"),
             "/var/lib/eeepc-agent": str(tmp_path / "var/lib/eeepc-agent").replace("\\", "/"),
             "/etc/systemd": str(tmp_path / "etc/systemd").replace("\\", "/")}
    for old, new in roots.items():
        remote = remote.replace(old, new)
    live = Path(roots["/opt/eeepc-agent"]) / "runtimes/self-evolving-agent/current"
    live.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    gate_path = source / "scripts/verify_release_health.py"
    gate_path.parent.mkdir()
    gate_path.write_text(gate_source, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    identity = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                    GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "candidate"], check=True, env=identity)
    gate = tmp_path / "candidate"
    gate.mkdir()
    archive = subprocess.Popen(["git", "-C", str(source), "archive", "--format=tar", "HEAD", "scripts"], stdout=subprocess.PIPE)
    extract = subprocess.run(["tar", "-x", "-C", str(gate)], stdin=archive.stdout, check=False)
    archive.stdout.close()
    assert archive.wait() == 0 and extract.returncode == 0
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    log_path = str(calls).replace("\\", "/")
    _write_mock(bindir / "sudo", f'''echo "sudo $*" >> {log_path}
if [[ "$1" == "-u" ]]; then shift 2; fi
case "$1" in
  env) shift; while [[ "$1" == *=* ]]; do export "$1"; shift; done; exec "$@" ;;
  chown|chmod|mkdir|cp|install|tee|rmdir|ln|tar) exit 0 ;;
  systemctl) echo "sudo-systemctl $*" >> {log_path}; exit 0 ;;
  rm) shift; if [[ "$1" == "-n" ]]; then shift; fi; command rm "$@" ;;
  stat) echo 0:0 ;;
  *) echo "unexpected sudo command: $*" >&2; exit 97 ;;
esac
''')
    _write_mock(bindir / "systemctl", f'''echo "systemctl $*" >> {log_path}
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
    _write_mock(bindir / "python3", f'''echo "python3 $*" >> {log_path}
if [[ "$*" == *verify_release_health.py* ]]; then
  python -c 'import runpy,sys; runpy.run_path(sys.argv[1], run_name="__main__")' "$GATE_TMP/scripts/verify_release_health.py"
else exit 0; fi
''')
    _write_mock(bindir / "stat", "echo 0:0")
    remote_path = tmp_path / "remote.sh"
    remote_path.write_text(remote, encoding="utf-8")
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], VERIFY_ONLY="1",
               FULL_COMMIT="candidate", PREV_RELEASE_PATH=str(live), GATE_TMP=str(gate).replace("\\", "/"),
               RELEASE_DIR=str(live), HEALTH_GATE_PYTHON=str(bindir / "python3"))
    return subprocess.run(["bash", str(remote_path)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=90)


@pytest.mark.parametrize(("gate_source", "expected_rc", "marker"), [
    ("print('CANDIDATE_GATE_EXECUTED')\n", 0, "CANDIDATE_GATE_EXECUTED"),
    ("raise SystemExit(23)\n", 1, "VERIFY_ONLY HEALTH_FETCH_FAILED"),
])
def test_remote_verify_runs_candidate_and_never_mutates_units_or_current(tmp_path: Path, gate_source: str, expected_rc: int, marker: str) -> None:
    result = _run_remote_gate(tmp_path, gate_source)
    assert result.returncode == expected_rc, result.stdout + result.stderr
    assert marker in result.stdout + result.stderr
    assert not (tmp_path / "candidate").exists()
    assert not (tmp_path / "opt/eeepc-agent/runtimes/self-evolving-agent/current/SOURCE_COMMIT").exists()
    logged = (tmp_path / "calls.log").read_text()
    assert "python3 " in logged and "verify_release_health.py" in logged
    assert not any(x in logged for x in ("systemctl restart", "systemctl stop", "systemctl start", "ln -sfn"))


def test_staging_failure_after_mktemp_cleans_remote_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    tar_bin = tmp_path / "bin"
    tar_bin.mkdir()
    tar_log = tmp_path / "tar.log"
    _write_mock(tar_bin / "tar", f'echo "$*" >> {tar_log}; exit 23')
    ssh_log = tmp_path / "ssh.log"
    ssh_path = tar_bin / "ssh"
    _write_mock(ssh_path, f'''echo "$*" >> {ssh_log}
if [[ "$*" == *mktemp* ]]; then
  d="{tmp_path}/eeebot-verify-gate.A1B2C3"
  mkdir -p "$d"
  trap 'rm -rf -- "$d"' EXIT
  tar -x -C "$d" < /dev/null || exit $?
fi
''')
    (tmp_path / "eeebot-verify-gate.A1B2C3").mkdir()
    stage = tmp_path / "stage.sh"
    stage.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
GATE_TMP="$(printf archive | ssh staging 'set -e; d=$(mktemp -d /tmp/eeebot-verify-gate.XXXXXX); trap cleanup EXIT; tar -x -C "$d"; printf %s "$d"; trap - EXIT')"
''', encoding="utf-8")
    result = subprocess.run(["bash", str(stage)], env=dict(os.environ, PATH=str(tar_bin) + os.pathsep + os.environ["PATH"]), capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "eeebot-verify-gate.A1B2C3").exists()
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    staging = deploy.split("GATE_TMP=\"$(git -C", 1)[1].split("  cleanup_candidate_gate", 1)[0]
    assert "trap" in staging and "trap - EXIT" in staging


def test_fallback_cleanup_after_chown_removes_dir_with_sudo_n_and_rejects_unsafe_path(tmp_path: Path) -> None:
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    cleanup = deploy.split("cleanup_candidate_gate() {", 1)[1].split("\n  }", 1)[0]
    assert "^/tmp/eeebot-verify-gate\\.[A-Za-z0-9]{6}$" in cleanup
    assert "sudo -n rm -rf -- '$GATE_TMP'" in cleanup
    gate = tmp_path / "owned-by-agent"
    gate.mkdir()
    ssh_log = tmp_path / "ssh.log"
    ssh = tmp_path / "bin/ssh"
    ssh.parent.mkdir()
    valid = "/tmp/eeebot-verify-gate.A1B2C3"
    _write_mock(ssh, f'''echo "$*" >> {str(ssh_log).replace(chr(92), "/")}
[[ "$*" == *"sudo -n rm -rf -- '{valid}'"* ]] || exit 9
rm -rf -- '{str(gate).replace(chr(92), "/")}'
''')
    runner = tmp_path / "cleanup.sh"
    runner.write_text(f'''#!/usr/bin/env bash
GATE_TMP='{valid}'
HOST=eeepc
log() {{ echo "$*"; }}
cleanup_candidate_gate() {{
{cleanup}
}}
# simulate chown -R eeepc-agent:eeepc-agent
cleanup_candidate_gate
''', encoding="utf-8")
    result = subprocess.run(["bash", str(runner)], env=dict(os.environ, PATH=str(ssh.parent) + os.pathsep + os.environ["PATH"]), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not gate.exists()
    assert "sudo -n rm -rf" in ssh_log.read_text()
    unsafe_runner = tmp_path / "unsafe.sh"
    unsafe_runner.write_text(runner.read_text().replace(valid, valid + "/../outside"), encoding="utf-8")
    ssh_log.unlink()
    unsafe = subprocess.run(["bash", str(unsafe_runner)], env=dict(os.environ, PATH=str(ssh.parent) + os.pathsep + os.environ["PATH"]), capture_output=True, text=True)
    assert unsafe.returncode == 0
    assert not ssh_log.exists()
