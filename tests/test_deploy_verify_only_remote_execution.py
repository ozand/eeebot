"""Replay the actual deploy heredoc locally to assert verify-only safety."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tests.test_deploy_release import DEPLOY_SCRIPT, _write_mock


def test_remote_verify_runs_candidate_and_never_mutates_units_or_current(tmp_path: Path) -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    remote = script.split("<<'REMOTE'", 1)[1].split("\nREMOTE", 1)[0]
    roots = {
        "/opt/eeepc-agent": str(tmp_path / "opt/eeepc-agent").replace("\\", "/"),
        "/var/lib/eeepc-agent": str(tmp_path / "var/lib/eeepc-agent").replace("\\", "/"),
        "/etc/systemd": str(tmp_path / "etc/systemd").replace("\\", "/"),
    }
    for old, new in roots.items():
        remote = remote.replace(old, new)
    root = Path(roots["/opt/eeepc-agent"]) / "runtimes/self-evolving-agent"
    live = root / "current"
    live.mkdir(parents=True)
    gate = tmp_path / "candidate"
    (gate / "scripts").mkdir(parents=True)
    (gate / "scripts/verify_release_health.py").write_text("print('CANDIDATE_GATE_EXECUTED')\n", encoding="utf-8")
    marker = tmp_path / "gate-ran"
    calls = tmp_path / "calls.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = str(calls).replace("\\", "/")
    _write_mock(bindir / "sudo", f'''if [[ "$1" == "-u" ]]; then shift 2; fi
case "$1" in
  env) shift; while [[ "$1" == *=* ]]; do export "$1"; shift; done; exec "$@" ;;
  chown|chmod|mkdir|cp|install|tee|rmdir|ln|tar) echo "sudo $*" >> {log}; exit 0 ;;
  rm) echo "sudo $*" >> {log}; shift; command rm "$@" ;;
  stat) echo 0:0 ;;
  *) exit 0 ;;
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
    python = shutil.which("python3")
    assert python, "python3 is required for the remote gate replay"
    marker_path = str(marker).replace(chr(92), "/")
    _write_mock(bindir / "python3", f'''if [[ "$*" == *verify_release_health.py* ]]; then
  test -f "$GATE_TMP/scripts/verify_release_health.py" || exit 9
  echo ran > {marker_path}
  echo "gate $*" >> {log}
  exit 0
fi
exec {python} "$@"
''')
    _write_mock(bindir / "stat", "echo 0:0")
    remote_script = tmp_path / "remote.sh"
    remote_script.write_text(remote, encoding="utf-8")
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
               VERIFY_ONLY="1", FULL_COMMIT="candidate-sha", PREV_RELEASE_PATH=str(live),
               GATE_TMP=str(gate).replace("\\", "/"), RELEASE_DIR=str(live),
               HEALTH_GATE_PYTHON=str(bindir / "python3"))
    result = subprocess.run(["bash", str(remote_script)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists(), result.stdout + result.stderr
    assert not gate.exists(), "the real remote EXIT trap must remove the candidate gate tree"
    logged = calls.read_text(encoding="utf-8")
    assert "gate " in logged
    assert not any(token in logged for token in (
        "systemctl restart", "systemctl stop", "systemctl start", "ln -sfn",
    )), logged
    assert not (live / "SOURCE_COMMIT").exists()
    assert not (tmp_path / "etc/systemd/system").exists() or not any((tmp_path / "etc/systemd/system").iterdir())
