"""Replay candidate staging and remote verify-only behavior in a sandbox."""
from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from tests.test_deploy_release import DEPLOY_SCRIPT, _git, _write_mock, run_deploy

pytest_plugins = ("tests.test_deploy_release",)


def _extract_remote(script: str) -> str:
    return script.split("<<'REMOTE'", 1)[1].split("\nREMOTE", 1)[0]



def _run_remote_gate(tmp_path: Path, gate_source: str) -> tuple[subprocess.CompletedProcess[str], list, tuple[Path, dict[str, str]]]:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    remote = _extract_remote(script)
    roots = {"/opt/eeepc-agent": str(tmp_path / "opt/eeepc-agent").replace("\\", "/"),
             "/var/lib/eeepc-agent": str(tmp_path / "var/lib/eeepc-agent").replace("\\", "/"),
             "/etc/systemd": str(tmp_path / "etc/systemd").replace("\\", "/")}
    for old, new in roots.items():
        remote = remote.replace(old, new)
    live = Path(roots["/opt/eeepc-agent"]) / "runtimes/self-evolving-agent/current"
    live.mkdir(parents=True)
    (live / "scripts").mkdir()
    (live / "scripts/verify_release_health.py").write_text(
        "import json; verify_release_health = lambda: {'health': {'dimensions': {}}}\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    gate_path = source / "scripts/verify_release_health.py"
    gate_path.parent.mkdir()
    gate_path.write_text("def verify_release_health(): return {'health': {'dimensions': {}}}\\n", encoding="utf-8")
    _git("init", cwd=source, check=True)
    hooks = source / ".test-hooks"
    hooks.mkdir()
    _git("config", "core.hooksPath", str(hooks), cwd=source, check=True)
    _git("add", "scripts/verify_release_health.py", cwd=source, check=True)
    identity = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                    GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
    _git("commit", "-qm", "head gate", cwd=source, check=True, env=identity)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    gate_path.write_text(
        "def verify_release_health(): return {'health': {'dimensions': {}}}\\n" + gate_source,
        encoding="utf-8",
    )
    _git("add", "scripts/verify_release_health.py", cwd=source, check=True)
    _git("commit", "-qm", "candidate gate", cwd=source, check=True, env=identity)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    gate_path.write_text("print('advanced HEAD gate')\n", encoding="utf-8")
    _git("add", "scripts/verify_release_health.py", cwd=source, check=True)
    _git("commit", "-qm", "advance checkout after candidate", cwd=source, check=True, env=identity)
    actual_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    assert candidate != head and candidate != actual_head
    assert "CANDIDATE_GATE_EXECUTED" in gate_source or "SystemExit(23)" in gate_source
    assert subprocess.check_output(
        ["git", "-C", str(source), "show", f"{candidate}:scripts/verify_release_health.py"], text=True
    ) != subprocess.check_output(
        ["git", "-C", str(source), "show", f"{actual_head}:scripts/verify_release_health.py"], text=True
    )
    gate = tmp_path / "candidate"
    gate.mkdir()
    archive = subprocess.Popen(["git", "-C", str(source), "archive", "--format=tar", candidate, "scripts"], stdout=subprocess.PIPE)
    extract = subprocess.run(["tar", "-x", "-C", str(gate)], stdin=archive.stdout, check=False)
    archive.stdout.close()
    assert archive.wait() == 0 and extract.returncode == 0
    systemd_sandbox = Path(roots["/etc/systemd"])
    systemd_sandbox.mkdir(parents=True)
    before_systemd = sorted((p.relative_to(systemd_sandbox).as_posix(), p.is_dir(), p.read_bytes() if p.is_file() else b"") for p in systemd_sandbox.rglob("*"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    log_path = str(calls).replace("\\", "/")
    _write_mock(bindir / "sudo", f'''echo "sudo $*" >> {log_path}
while [[ "$1" == -* ]]; do
  case "$1" in
    -n) shift ;;
    -u) shift 2 ;;
    *) echo "unexpected sudo option: $1" >&2; exit 97 ;;
  esac
done
case "$1" in
  env) shift; while [[ "$1" == *=* ]]; do export "$1"; shift; done; exec "$@" ;;
  chown|chmod|mkdir|cp|install|tee|rmdir|ln|tar) exit 0 ;;
  systemctl) echo "sudo-systemctl $*" >> {log_path}; exit 0 ;;
  rm) command rm "$@" ;;
  stat) echo 0:0 ;;
  *) echo "unexpected sudo command: $*" >&2; exit 97 ;;
esac
''')
    _write_mock(bindir / "systemctl", f'''echo "systemctl $*" >> {log_path}
case "$*" in
  *eeepc-network-fallback.timer*"-p LoadState"*|*eeepc-network-fallback.service*"-p LoadState"*) echo not-found ;;
  *eeebot-network-fallback*"-p LoadState"*) echo not-found ;;
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
exec {shlex.quote(sys.executable)} "$@"
''')
    _write_mock(bindir / "stat", "echo 0:0")
    remote_path = tmp_path / "remote.sh"
    remote_path.write_text(remote, encoding="utf-8")
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], VERIFY_ONLY="1",
               FULL_COMMIT="candidate", PREV_RELEASE_PATH=str(live), GATE_TMP=str(gate).replace("\\", "/"),
               RELEASE_DIR=str(live), HEALTH_GATE_PYTHON=str(bindir / "python3"))
    result = subprocess.run(["bash", str(remote_path)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=480)
    return result, before_systemd, (systemd_sandbox, roots)


@pytest.mark.parametrize(("gate_source", "expected_rc", "marker"), [
    ("print('CANDIDATE_GATE_EXECUTED')\n", 0, "CANDIDATE_GATE_EXECUTED"),
    ("raise SystemExit(23)\n", 1, "VERIFY_ONLY HEALTH_FETCH_FAILED"),
])
def test_remote_verify_runs_candidate_and_never_mutates_units_or_current(tmp_path: Path, gate_source: str, expected_rc: int, marker: str) -> None:
    result, before_systemd, sandbox = _run_remote_gate(tmp_path, gate_source)
    output = result.stdout + result.stderr
    assert result.returncode == expected_rc, output
    assert marker in output
    assert "ADVANCED_HEAD_ONLY" not in output, "candidate gate must not come from advanced checkout HEAD"
    assert not (tmp_path / "candidate").exists()
    assert not (tmp_path / "opt/eeepc-agent/runtimes/self-evolving-agent/current/SOURCE_COMMIT").exists()
    systemd_sandbox, roots = sandbox
    after_systemd = sorted((p.relative_to(systemd_sandbox).as_posix(), p.is_dir(), p.read_bytes() if p.is_file() else b"") for p in systemd_sandbox.rglob("*"))
    assert after_systemd == before_systemd, "verify-only modified the sandbox systemd tree"
    logged = (tmp_path / "calls.log").read_text()
    assert "python3 " in logged and "verify_release_health.py" in logged
    assert not any(x in logged for x in ("systemctl restart", "systemctl stop", "systemctl start", "ln -sfn"))


def test_advanced_head_mutation_fails_on_explicit_marker(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    gate = source / "scripts/verify_release_health.py"
    gate.parent.mkdir()
    gate.write_text("print('CANDIDATE_GATE_ONLY')\n", encoding="utf-8")
    (source / "nanobot").mkdir()
    (source / "nanobot/__init__.py").write_text("# candidate\n", encoding="utf-8")
    (source / "host/eeepc/etc").mkdir(parents=True)
    (source / "host/eeepc/etc/preset.env").write_text("FIXTURE=1\n", encoding="utf-8")
    _git("init", cwd=source, check=True)
    _git("add", "scripts", "nanobot", "host/eeepc/etc", cwd=source, check=True)
    _git("commit", "-m", "candidate", cwd=source, check=True)
    gate.write_text("print('ADVANCED_HEAD_ONLY')\n", encoding="utf-8")
    _git("add", "scripts/verify_release_health.py", cwd=source, check=True)
    _git("commit", "-m", "advance", cwd=source, check=True)
    candidate = _git("rev-parse", "HEAD~", cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    archive_command = next(line.strip().removesuffix("\\") for line in deploy.splitlines()
                           if 'GATE_TMP="$(git -C "$REPO_ROOT" archive --format=tar' in line)
    pipeline = archive_command.removeprefix('GATE_TMP="$(').removesuffix('")').replace(" | ", " ")
    pipeline = pipeline.replace('"$COMMIT"', 'HEAD', 1)
    archive = subprocess.run(["bash", "-c", pipeline], env=dict(os.environ, REPO_ROOT=str(source), COMMIT=candidate),
                             capture_output=True, timeout=30)
    assert archive.returncode == 0, archive.stderr.decode(errors="replace")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        marker = bundle.extractfile("scripts/verify_release_health.py").read().decode()
    expected_members = subprocess.check_output(
        ["git", "-C", str(source), "ls-tree", "-r", "--name-only", candidate, "--", "scripts", "nanobot", "host/eeepc/etc"], text=True
    ).splitlines()
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        archive_members = sorted(member.name for member in bundle.getmembers() if member.isfile())
    assert archive_members == sorted(expected_members)
    with pytest.raises(AssertionError, match="ADVANCED_HEAD_ONLY"):
        assert "ADVANCED_HEAD_ONLY" not in marker, "candidate archive selected advanced checkout HEAD"


def test_production_archive_stream_matches_candidate_tree_and_not_advanced_head(tmp_path: Path):
    """The actual deploy archive must be a tree-exact archive of COMMIT, not checkout HEAD."""
    source = tmp_path / "source"
    source.mkdir()
    gate = source / "scripts/verify_release_health.py"
    gate.parent.mkdir()
    gate.write_text("print('CANDIDATE_GATE_ONLY')\n", encoding="utf-8")
    (source / "nanobot").mkdir()
    (source / "nanobot/__init__.py").write_text("# candidate\n", encoding="utf-8")
    (source / "host/eeepc/etc").mkdir(parents=True)
    (source / "host/eeepc/etc/preset.env").write_text("FIXTURE=1\n", encoding="utf-8")
    _git("init", cwd=source, check=True)
    _git("add", "scripts", "nanobot", "host/eeepc/etc", cwd=source, check=True)
    _git("commit", "-m", "candidate", cwd=source, check=True)
    candidate = _git("rev-parse", "HEAD", cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    gate.write_text("print('ADVANCED_HEAD_ONLY')\n", encoding="utf-8")
    _git("add", "scripts/verify_release_health.py", cwd=source, check=True)
    _git("commit", "-m", "advance HEAD", cwd=source, check=True)

    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    archive_prefix = '  GATE_TMP="$(git -C "$REPO_ROOT" archive --format=tar '
    archive_lines = deploy.splitlines()
    archive_start = next(i for i, line in enumerate(archive_lines) if line.startswith(archive_prefix))
    archive_command = "\n".join(
        line.strip().removesuffix("\\")
        for line in archive_lines[archive_start:]
        if line.strip()
    ).split(" |", 1)[0]
    archive_pipeline = archive_command.removeprefix('GATE_TMP="$(')
    mutated_pipeline = archive_pipeline.replace('"$COMMIT"', 'HEAD', 1)
    assert mutated_pipeline != archive_pipeline, "production archive-ref mutation did not apply"
    archive_args = archive_pipeline.removeprefix('git -C "$REPO_ROOT" archive --format=tar ').split()
    archive_args = [candidate if arg == '"$COMMIT"' else arg.strip('"') for arg in archive_args]
    if os.environ.get("MUTATE_ARCHIVE_REF"):
        archive_args[archive_args.index(candidate)] = "HEAD"
    archive = subprocess.run(
        ["git", "-C", str(source), "archive", "--format=tar", *archive_args], capture_output=True, check=True,
    )
    expected = subprocess.check_output(
        ["git", "-C", str(source), "ls-tree", "-r", "--name-only", candidate, "--", "scripts", "nanobot", "host/eeepc/etc"], text=True
    ).splitlines()
    stage = tmp_path / "candidate-stage"
    stage.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        archive_members = sorted(member.name for member in bundle.getmembers() if member.isfile())
        bundle.extractall(stage, filter="data")
    assert archive_members == sorted(expected)
    assert "ADVANCED_HEAD_ONLY" not in archive_members
    actual = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
    assert actual == sorted(expected)
    result = subprocess.run([sys.executable, str(stage / "scripts/verify_release_health.py")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ADVANCED_HEAD_ONLY" not in result.stdout, "ADVANCED_HEAD_ONLY: archive selected checkout HEAD instead of candidate"
    assert "CANDIDATE_GATE_ONLY" in result.stdout


def test_staging_failure_after_mktemp_cleans_remote_directory(tmp_path: Path, repo: Path, mock_bin: Path, monkeypatch) -> None:
    # Exercise the exact staging command embedded in production deploy_release.sh.
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    staging_source = deploy.split('  GATE_TMP="$(git -C "$REPO_ROOT" archive', 1)[1].split("\n  cleanup_candidate_gate", 1)[0]
    production_command = textwrap.dedent(deploy.split('run ssh "ozand@${HOST}" "$REMOTE_ENV bash -s" <<\'REMOTE\'', 1)[1].split("\nREMOTE", 1)[0])

    # Create a second commit with the candidate gate. It differs from the
    # initial HEAD and gives the real archive pipeline a requested path, while
    # the mocked remote tar fails after mktemp has created its directory.
    (repo / "scripts").mkdir()
    (repo / "scripts/verify_release_health.py").write_text("print('candidate')\\n", encoding="utf-8")
    (repo / "nanobot").mkdir()
    (repo / "nanobot/__init__.py").write_text("# candidate package\\n", encoding="utf-8")
    (repo / "host/eeepc/etc/presets").mkdir(parents=True)
    (repo / "host/eeepc/etc/presets/test.env").write_text("FIXTURE=1\\n", encoding="utf-8")
    _git("add", "scripts", "nanobot", "host/eeepc/etc/presets", cwd=repo, check=True)
    _git("commit", "-m", "candidate gate", cwd=repo, check=True)
    candidate = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo, text=True).strip()
    (repo / "later-head.txt").write_text("checkout HEAD differs from requested candidate\\n", encoding="utf-8")
    _git("add", "later-head.txt", cwd=repo, check=True)
    _git("commit", "-m", "advance checkout after candidate", cwd=repo, check=True)
    head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo, text=True).strip()
    assert candidate != head

    staging_dir = tmp_path / "eeebot-verify-gate.A1B2C3"
    tar_log = tmp_path / "tar.log"
    _write_mock(mock_bin / "tar", f'echo "$*" >> {str(tar_log).replace(chr(92), "/")}; exit 23')
    _write_mock(mock_bin / "sudo", '[[ "$1" == -n ]] && shift; exec rm "$@"')
    staging_script = tmp_path / "staging-command.sh"
    staging_command = "\n".join(
        line.strip().removesuffix("\\")
        for line in deploy.splitlines()
        if 'GATE_TMP="$(git -C "$REPO_ROOT" archive' in line
        or (line.startswith("    ssh ") and "eeebot-verify-gate.XXXXXX" in line)
    ) + "\n"
    staging_script.write_text('#!/usr/bin/env bash\nset -euo pipefail\n' + staging_command, encoding="utf-8")
    syntax_check = subprocess.run(["bash", "-n", str(staging_script)], capture_output=True, text=True)
    assert syntax_check.returncode == 0, syntax_check.stderr
    staging_dir_arg = shlex.quote(str(staging_dir).replace("\\", "/"))
    staging_script_arg = shlex.quote(str(staging_script).replace("\\", "/"))
    tar_input = tmp_path / "empty.tar"
    tar_input.write_bytes(b"")
    remote_commands = tmp_path / "remote-commands.log"
    remote_mock_log = str(remote_commands).replace("\\", "/")
    _write_mock(mock_bin / "ssh", f'''echo "$*" >> {remote_mock_log}
if [[ "$*" == *"readlink /opt/eeepc-agent/runtimes/self-evolving-agent/current"* ]]; then
  echo /opt/eeepc-agent/runtimes/self-evolving-agent/releases/previous
  exit 0
fi
if [[ "$*" == *"mktemp -d /tmp/eeebot-verify-gate."* ]]; then
  export STAGING_DIR={staging_dir_arg}
  bash {staging_script_arg}
  exit $?
fi
exit 0
''')
    # Run the production script so its COMMIT selection and staging pipeline
    # execute unchanged; requested candidate is a commit distinct from checkout HEAD.
    monkeypatch.setenv("REPO_ROOT", str(repo))
    result = run_deploy(repo, mock_bin, ["--verify-only", "--ref", candidate])
    assert result.returncode != 0
    assert not staging_dir.exists()
    commands = remote_commands.read_text(encoding="utf-8")
    assert "mktemp -d /tmp/eeebot-verify-gate.XXXXXX" in commands
    assert "tar -x -C" in commands
    assert "mktemp -d /tmp/eeebot-verify-gate.XXXXXX" in staging_source
    assert "trap " in staging_source and "trap - EXIT" in staging_source
    assert "tar -x -C \"$d\"" in staging_source
    assert 'sudo chown -R eeepc-agent:eeepc-agent "$GATE_TMP"' in production_command
    assert "[remote] candidate $FULL_COMMIT gate temp=$GATE_TMP" in production_command



def test_sudo_n_rm_stub_parses_options_before_dispatch(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log_path = str(tmp_path / "sudo.log").replace("\\", "/")
    _write_mock(bindir / "sudo", f'''echo "sudo $*" >> {log_path}
while [[ "$1" == -* ]]; do
  case "$1" in -n) shift ;; -u) shift 2 ;; *) exit 97 ;; esac
done
case "$1" in rm) command rm "$@" ;; *) exit 97 ;; esac
''')
    gate = tmp_path / "eeebot-verify-gate.A1B2C3"
    gate.mkdir()
    runner = tmp_path / "sudo-call.sh"
    runner.write_text(f'"{bindir / "sudo"}" -n rm -rf -- "{gate}"\n', encoding="utf-8")
    result = subprocess.run(["bash", str(runner)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not gate.exists()
    assert "sudo -n rm -rf --" in (tmp_path / "sudo.log").read_text()


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
