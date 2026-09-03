import os
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
    # The deploy captures the current release before running the remote
    # heredoc. Give every scenario a rollback target unless it explicitly
    # overrides this preflight response.
    wrapper = f'''
    if [[ "$*" == *"readlink /opt/eeepc-agent/runtimes/self-evolving-agent/current"* ]]; then
        echo /opt/eeepc-agent/runtimes/self-evolving-agent/releases/previous-good
        exit 0
    fi
    {script_content}
    '''
    _write_mock(mock_bin / "ssh", wrapper)


def _journal_replay_mock(*journal_lines):
    r"""An ssh stub that runs the remote command's own filter over fixture text.

    A stub that just echoes a canned answer proves nothing about the script's
    grep pattern -- it replaces the pipeline the pattern lives in. This one
    feeds the fixture journal lines into the `| grep ...` half of whatever
    command was asked for, so the pattern under test is the thing being
    exercised.
    """
    payload = "\n".join(journal_lines)
    return f"""
    cmd="$*"
    if [[ "$cmd" == *journalctl* && "$cmd" == *"| grep"* ]]; then
        printf '%s\\n' {shlex.quote(payload)} | eval "${{cmd#*| }}"
        exit 0
    fi
    exit 0
    """


# The authority host runs MSK. journalctl reads a bare `--since` timestamp in
# that zone, so the same wall-clock string denotes an instant this many seconds
# EARLIER in UTC than it looks.
HOST_UTC_OFFSET_SECONDS = 3 * 60 * 60


def _journal_since_mock(*entries):
    r"""An ssh stub that also honours `--since` the way a MSK host's journalctl does.

    `_journal_replay_mock` replaces journalctl outright, so it cannot say
    anything about the `--since` boundary -- every fixture line always reaches
    the grep. This stub keeps that replay behaviour and adds the one thing
    #1162 is about: it resolves the `--since` value through `date` under the
    HOST's timezone, so a bare `YYYY-MM-DD HH:MM:SS` is read as local time and
    a value ending in ` UTC` is read as UTC, exactly as journalctl does. Lines
    older than the resolved boundary are dropped before the grep runs.

    `entries` are `(iso_utc, text)` pairs; the boundary comparison uses the
    timestamp, the grep sees the text.
    """
    rendered = []
    for iso_utc, text in entries:
        stamp = datetime.strptime(iso_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        rendered.append(f"{int(stamp.timestamp())}\t{text}")
    payload = "\n".join(rendered)
    return f"""
    cmd="$*"
    if [[ "$cmd" == *journalctl* && "$cmd" == *"| grep"* ]]; then
        since=$(sed -n "s/.*--since \\"\\([^\\"]*\\)\\".*/\\1/p" <<<"$cmd")
        [ -z "$since" ] && since=$(sed -n "s/.*--since '\\([^']*\\)'.*/\\1/p" <<<"$cmd")
        # journalctl's rule, applied explicitly: an explicit ` UTC` suffix is
        # honoured, a bare timestamp is read in the HOST's zone. Deriving this
        # from the runner's `date` does not work -- git-bash's date silently
        # ignores TZ, so both forms resolve identically there and the test
        # passes against the very bug it is meant to catch.
        case "$since" in
            *" UTC") base="${{since% UTC}}"; shift_s=0 ;;
            *)       base="$since";          shift_s={HOST_UTC_OFFSET_SECONDS} ;;
        esac
        parsed=$(date -u -d "$base UTC" +%s 2>/dev/null || echo 0)
        boundary=$(( parsed - shift_s ))
        printf '%s\\n' {shlex.quote(payload)} \\
            | awk -F'\\t' -v b="$boundary" 'NF==2 && $1 >= b {{ print $2 }}' \\
            | eval "${{cmd#*| }}"
        exit 0
    fi
    exit 0
    """

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

def test_dashboard_activation_and_rollback_are_in_deploy_script(repo, mock_bin):
    content = (repo / "host" / "eeepc" / "scripts" / "deploy_release.sh").read_text()
    assert 'DASHBOARD_UNIT=eeebot-dashboard.service' in content
    assert 'sudo systemctl restart "$DASHBOARD_UNIT"' in content
    assert 'systemctl show "$DASHBOARD_UNIT" -p MainPID --value' in content
    assert 'readlink -v "/proc/$DASHBOARD_PID/cwd"' in content
    assert 'sudo systemctl restart eeebot-dashboard.service && sudo systemctl restart eeepc-self-evolving-subagent-bridge.service' in content
    assert content.index("updating current symlink") < content.index('sudo systemctl restart "$DASHBOARD_UNIT"')
    assert content.index('sudo systemctl restart "$DASHBOARD_UNIT"') < content.index("Ensure bridge service is restarted correctly")


def test_dashboard_activation_fails_when_enabled_unit_restart_fails(repo, mock_bin):
    content = (repo / "host" / "eeepc" / "scripts" / "deploy_release.sh").read_text()
    assert 'if ! systemctl is-active --quiet "$DASHBOARD_UNIT"' in content
    # #1259: CRITICALs go through `die` (prints "CRITICAL: ..." and returns 1 so
    # the ERR trap rolls back); the emitted text is unchanged.
    assert 'die "$DASHBOARD_UNIT is not active after restart"' in content
    assert 'curl --fail --silent --show-error http://127.0.0.1:8080/api/health' in content
    assert 'curl --fail --silent --show-error http://127.0.0.1:8080/api/metrics' in content
    assert 'dashboard listener :8080 is not active' in content


def test_dashboard_activation_verifies_pid_release_identity(repo, mock_bin):
    content = (repo / "host" / "eeepc" / "scripts" / "deploy_release.sh").read_text()
    assert 'DASHBOARD_PID=' in content
    assert 'DASHBOARD_PREV_PID=' in content
    assert 'DASHBOARD_START=' in content
    assert 'DASHBOARD_PREV_START=' in content
    assert 'DASHBOARD_CWD=' in content
    assert 'DASHBOARD_CMDLINE=' in content
    assert 'cwd is' in content
    assert 'activated release SOURCE_COMMIT' in content
    assert 'cmdline' in content
    assert 'DASHBOARD_SOCKET_PIDS=' in content
    assert 'DASHBOARD_SOCKET_PID_COUNT=' in content
    assert 'exactly one owner, dashboard PID' in content
    assert 'dashboard endpoint missing bounded source metadata' in content


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
        if [[ "$*" == *"--since"* && "$*" == *"--utc"* && "$*" != *Z* ]]; then
            if [[ "$*" == *Traceback* ]]; then
                # Shaped like a real journal line: journalctl prefixes every
                # message with `host process[pid]:`, and the gate anchors on it.
                echo "Sep 01 20:15:31 eeepc python[22826]: Traceback (most recent call last):"
                echo "Sep 01 20:15:31 eeepc python[22826]: Exception: crashed"
            fi
        else
            echo "Failed to parse timestamp" >&2
            exit 1
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

def test_h_edited_source_in_debug_log_does_not_trigger_rollback(repo, tmp_path, mock_bin):
    """Source code the agent is editing is not a failure signal.

    The bridge logs the full arguments of an `edit_file` tool call at DEBUG
    level, so the contents of whatever file a cycle is editing reach the
    journal. `grep -iE 'error:'` then matches any ordinary CLI error message in
    that source and rolls back a healthy release — observed live on
    2026-09-01, deploy of 611e44d5 at 20:13 UTC.
    """
    set_ssh_mock(mock_bin, _journal_replay_mock(
        'Sep 01 20:15:31 eeepc python[22826]: 2026-09-01 20:15:31.559 | DEBUG    | '
        'nanobot.agent.subagent:_run_subagent:336 - Subagent [b071997a] executing: '
        'edit_file with arguments: {"path": "scripts/summarize_failure_outcomes.py", '
        '"new_text": "    print(f\\"error: file not found: {target_file}\\", file=sys.stderr)"}'
    ))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert "rolling back" not in combined, combined
    assert res.returncode == 0


def test_i_service_traceback_still_triggers_rollback(repo, tmp_path, mock_bin):
    """Narrowing the pattern must not disarm real traceback detection."""
    set_ssh_mock(mock_bin, _journal_replay_mock(
        "Sep 01 20:15:31 eeepc python[22826]: Traceback (most recent call last):",
        'Sep 01 20:15:31 eeepc python[22826]:   File "/opt/.../bridge.py", line 12, in <module>',
        "Sep 01 20:15:31 eeepc python[22826]: NameError: name '_parse_explore_mode' is not defined",
    ))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    assert res.returncode != 0
    assert "rolling back" in (res.stdout + res.stderr).lower()


def test_d_terminal_outcome_row_no_longer_passes(repo, tmp_path, mock_bin):
    """#1163: a terminal ledger row is not a health signal any more.

    The old PASS branch accepted any `"phase": "outcome"` row after the flip —
    on 2026-09-02 a deploy passed on `outcome: skipped-duplicate, verdict:
    reject`. The mock answers the old query the way the ledger would; the gate
    must neither ask nor say PASS.
    """
    ssh_mock = """
    if [[ "$*" == *"| grep"* ]]; then
        if [[ "$*" == *"phase"* && "$*" == *"outcome"* ]]; then
            echo '{"cycle_id": "c1", "phase": "outcome", "outcome": "skipped-duplicate", "verdict": "reject", "ts": "2099-01-01T00:00:00Z"}'
        fi
        exit 0
    else
        exit 0
    fi
    """
    set_ssh_mock(mock_bin, ssh_mock)
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode == 0
    assert "health gate: pass" not in combined, combined
    assert "health gate: unknown" in combined, combined

def test_f_routine_sigterm_does_not_trigger_rollback(repo, tmp_path, mock_bin):
    """A clean SIGTERM stop is not a crash.

    The bridge is timer-driven: systemd stops each run with SIGTERM and logs
    `Main process exited, code=killed, status=15/TERM` every few minutes, and
    the deploy's own `systemctl restart` produces the same line. `status=[1-9]`
    matches the "15" in 15/TERM, so once #1155 made the journal queries
    readable this pattern matched routine operation and rolled back every
    deploy — observed live on 2026-09-01 at 18:48 UTC.
    """
    set_ssh_mock(mock_bin, _journal_replay_mock(
        "Sep 01 21:52:10 eeepc systemd[1]: eeepc-self-evolving-subagent-bridge.service: "
        "Main process exited, code=killed, status=15/TERM"
    ))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert "rolling back" not in combined, combined
    assert res.returncode == 0


def test_g_real_crash_still_triggers_rollback(repo, tmp_path, mock_bin):
    """Narrowing the SIGTERM false positive must not disarm real crashes."""
    set_ssh_mock(mock_bin, _journal_replay_mock(
        "Sep 01 21:52:10 eeepc systemd[1]: eeepc-self-evolving-subagent-bridge.service: "
        "Main process exited, code=exited, status=1/FAILURE"
    ))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    assert res.returncode != 0
    assert "rolling back" in (res.stdout + res.stderr).lower()


def _utc(offset_minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


TRACEBACK_TEXT = "eeepc python[22826]: Traceback (most recent call last):"


def test_j_pre_deploy_traceback_does_not_trigger_rollback(repo, tmp_path, mock_bin):
    """A crash that predates the deploy is not this release's crash.

    `FLIP_JOURNAL_TS` is built with `date -u`, but journalctl parses a bare
    timestamp in the host's local time, and the host is MSK (UTC+3) -- `--utc`
    only changes output formatting. The window therefore opened three hours
    early, and both FAIL branches scanned pre-deploy history: a traceback the
    operator had already fixed by deploying would roll the fix straight back.
    """
    set_ssh_mock(mock_bin, _journal_since_mock((_utc(-120), TRACEBACK_TEXT)))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert "rolling back" not in combined, combined
    assert res.returncode == 0


def test_k_post_deploy_traceback_still_triggers_rollback(repo, tmp_path, mock_bin):
    """Narrowing the window must not disarm the gate for real post-deploy crashes."""
    set_ssh_mock(mock_bin, _journal_since_mock((_utc(1), TRACEBACK_TEXT)))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    assert res.returncode != 0
    assert "rolling back" in (res.stdout + res.stderr).lower()


def test_e_timeout_returns_unknown(repo, tmp_path, mock_bin):
    ssh_mock = """
    exit 0
    """
    set_ssh_mock(mock_bin, ssh_mock)
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    assert res.returncode == 0
    assert "unknown" in (res.stdout + res.stderr).lower()


# ─── #1163: positive verdicts that do not wait for a terminal ledger row ─────

BRIDGE_UNIT = "eeepc-self-evolving-subagent-bridge.service"
STARTING_TEXT = f"eeepc systemd[1]: Starting {BRIDGE_UNIT} - Run eeepc self-evolving subagent bridge..."
FINISHED_TEXT = f"eeepc systemd[1]: Finished {BRIDGE_UNIT} - Run eeepc self-evolving subagent bridge."


def _streak_mock(streak_json: str, *journal_lines):
    """ssh stub: canned exit_streak.json for the recorder read, journal replay otherwise."""
    replay = _journal_replay_mock(*journal_lines)
    return f"""
    if [[ "$*" == *exit_streak.json* ]]; then
        printf '%s\n' {shlex.quote(streak_json)}
        exit 0
    fi
    if [[ "$*" == *"sudo ln -sfn"* ]]; then
        echo "Rolling back"
        exit 0
    fi
    {replay}
    """


def test_l_post_flip_finished_line_is_clean_exit_never_pass(repo, tmp_path, mock_bin):
    """Pre-#1163: a `Finished <unit>` line was not read at all, so the gate ended UNKNOWN.

    systemd writes `Finished <unit>` only when a oneshot run exits 0 (host
    journal 2026-09-02: 164 Finished / 164 Deactivated successfully against 100
    `Failed with result 'exit-code'`). A pre-flip Finished must not count.
    """
    set_ssh_mock(mock_bin, _journal_since_mock((_utc(-120), FINISHED_TEXT)))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert "clean-exit" not in combined and "health gate: unknown" in combined, combined

    set_ssh_mock(mock_bin, _journal_since_mock((_utc(1), STARTING_TEXT), (_utc(2), FINISHED_TEXT)))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode == 0
    assert "health gate: clean-exit" in combined, combined
    assert "health gate: pass" not in combined and "rolling back" not in combined


def test_m_invocation_without_finish_is_no_crash_after_the_hold(repo, tmp_path, mock_bin):
    """Pre-#1163: `--no-crash-hold` did not exist and a Starting line meant nothing.

    2026-09-01 crash loop: the bridge wrote `started`/`dedup` ledger rows 140
    times before dying ~30 s in, so an invocation alone proves nothing; the
    verdict needs the hold to pass with neither FAIL branch firing, and it must
    carry its own label — weaker than CLEAN-EXIT, never PASS.
    """
    set_ssh_mock(mock_bin, _journal_replay_mock(STARTING_TEXT))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "1", "--no-crash-hold", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode == 0, combined
    assert "health gate: no-crash" in combined, combined
    assert "weaker than clean-exit" in combined
    assert "health gate: pass" not in combined and "health gate: clean-exit" not in combined


def test_n_invocation_then_crash_within_the_hold_rolls_back(repo, tmp_path, mock_bin):
    """The FAIL branches keep their authority over the hold: Starting + a non-zero exit -> rollback."""
    set_ssh_mock(mock_bin, _journal_replay_mock(
        STARTING_TEXT,
        f"eeepc systemd[1]: {BRIDGE_UNIT}: Main process exited, code=exited, status=1/FAILURE",
    ))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "1", "--no-crash-hold", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode != 0
    assert "rolling back" in combined and "no-crash" not in combined, combined


def test_o_exit_streak_failure_after_flip_rolls_back(repo, tmp_path, mock_bin):
    """Pre-#1163: state/bridge/exit_streak.json (#1200) was not consulted."""
    streak = ('{"schema_version": "bridge-exit-streak-v1", "consecutive_failures": 3, '
              '"last_failure_ts": "2099-01-01T00:00:00Z", "last_exit_status": 1, '
              '"last_error": "NameError: name \'_parse_explore_mode\' is not defined", "last_where": "bridge.py:4987"}')
    set_ssh_mock(mock_bin, _streak_mock(streak))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode != 0
    assert "rolling back" in combined and "exit recorder shows a failure" in combined, combined
    assert "_parse_explore_mode" in combined


def test_p_exit_streak_success_after_flip_is_clean_exit(repo, tmp_path, mock_bin):
    """Pre-#1163: the recorder's post-flip success was invisible to the gate."""
    stale = '{"schema_version": "bridge-exit-streak-v1", "consecutive_failures": 0, "last_success_ts": "2000-01-01T00:00:00Z"}'
    set_ssh_mock(mock_bin, _streak_mock(stale))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert "clean-exit" not in combined and "health gate: unknown" in combined, "a pre-flip success is the old release's"

    fresh = '{"schema_version": "bridge-exit-streak-v1", "consecutive_failures": 0, "last_success_ts": "2099-01-01T00:00:00Z"}'
    set_ssh_mock(mock_bin, _streak_mock(fresh))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode == 0
    assert "health gate: clean-exit" in combined and "exit_streak.json" in combined, combined
    assert "health gate: pass" not in combined


def test_q_gate_script_never_reports_pass_and_documents_the_measurement():
    """Pre-#1163: the PASS label and the 15-minute default with no measurement next to it."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "Health gate: PASS" not in text
    assert "Health gate: CLEAN-EXIT" in text and "Health gate: NO-CRASH" in text and "Health gate: UNKNOWN" in text
    assert "HEALTH_TIMEOUT=10" in text and "median wall time is 1.6 min" in text
    assert '"phase": "outcome"' not in text.split("# 4. Post-deploy health gate")[1], "the terminal-row signal is gone"


def test_r_finished_line_never_outranks_a_crash_in_the_same_window(repo, tmp_path, mock_bin):
    """Guard on statement order, not a repro: during the 2026-09-01 crash loop the
    journal carried 6 `Finished` lines against 139 `Failed with result 'exit-code'`,
    so a post-flip Finished line and a non-zero exit in the same window must roll
    back — the FAIL branches are evaluated before any positive verdict."""
    set_ssh_mock(mock_bin, _journal_replay_mock(
        STARTING_TEXT,
        FINISHED_TEXT,
        f"eeepc systemd[1]: {BRIDGE_UNIT}: Main process exited, code=exited, status=1/FAILURE",
        f"eeepc systemd[1]: {BRIDGE_UNIT}: Failed with result 'exit-code'.",
    ))
    res = run_deploy(repo, mock_bin, ["--health-timeout", "0", "--ref", "HEAD"])
    combined = (res.stdout + res.stderr).lower()
    assert res.returncode != 0
    assert "rolling back" in combined and "clean-exit" not in combined, combined


def test_proc_reads_for_the_dashboard_use_sudo() -> None:
    """`/proc/<pid>/cwd` belongs to the unit's user, not the deploying account.

    Release 20260903T141455Z aborted with

        CRITICAL: eeebot-dashboard.service PID 11213 cwd is '', expected '.../releases/...'

    while the dashboard was in fact running from the new release. `readlink`
    without sudo returns empty for another user's process, and the check read
    that emptiness as a mismatch — failing a healthy deploy (#1245).

    A redirect cannot carry the privilege either: in `sudo tr ... < /proc/x`
    the shell opens the file as the deploying user before sudo runs, so the
    read itself has to be the sudo'd command.
    """
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    # #1259: both reads go through gate_read, which runs the sudo'd command as
    # its argv — the privilege is still on the read itself.
    assert 'gate_read "/proc/$DASHBOARD_PID/cwd" sudo readlink -v "/proc/$DASHBOARD_PID/cwd"' in script, (
        "the dashboard cwd read must be sudo'd or it returns empty for another user's process")
    # gate_read streams the sudo'd cat through a file so the NUL separators
    # reach `tr` — a bash command substitution drops NUL bytes, and capturing
    # first yielded `python3/opt/…--serve--port8080` on the healthy host
    # (found by --verify-only, #1259).
    assert 'gate_read "/proc/$DASHBOARD_PID/cmdline" sudo cat "/proc/$DASHBOARD_PID/cmdline" | tr "\\0" " "' in script, (
        "the cmdline read must be sudo'd as the command, not behind a shell redirect")
    assert '$(sudo tr' not in script, (
        'a sudo tr behind a shell redirect opens the file as the unprivileged user')


def test_dashboard_cmdline_check_matches_the_current_symlink_not_a_release_dir() -> None:
    """Verify-only pins the exact dashboard script and serving arguments."""
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'case "$DASHBOARD_CMDLINE" in' in script
    assert '*"/current/scripts/eeebot_dashboard.py --serve --port 8080 --host 0.0.0.0"*)' in script, (
        "the command-line check must match the current symlink and exact args")
    assert 'DASHBOARD_CMDLINE" != *"$RELEASE_DIR"*' not in script, (
        "a release-dir substring must not satisfy verify-only")
    assert 'DASHBOARD_CMDLINE" != *"/current"*' not in script, (
        "a bare current substring must not satisfy verify-only")


def test_verify_only_uses_privileged_current_reads_and_skips_mutations() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert 'RELEASE_DIR="$(gate_read "$CURRENT_SYMLINK" sudo readlink -v "$CURRENT_SYMLINK")"' in script
    assert 'FULL_COMMIT="$(sudo cat "$RELEASE_DIR/SOURCE_COMMIT")"' in script
    assert 'if [ "$VERIFY_ONLY" -eq 0 ]; then' in script
    assert 'VERIFY_ONLY" -eq 1' in script
    # Message reworded with the check: the bare host-path prefixes are gone,
    # so "or host path" no longer describes what it rejects. The tokens it
    # still rejects are asserted individually below.
    assert 'dashboard endpoint contains a stale cycle id or retired artifact value' in script
    assert '"/var/lib/eeepc-agent/"' not in script, (
        "a host path appearing in the payload is not evidence of staleness — "
        "operator_attention legitimately reports one, and listing it here "
        "rejected every healthy deploy")
    assert 'expected = "OK" if source_status[source_key] == "fresh" else "WARN"' in script
    assert '0.88 avg over 5 sample(s)' in script
    assert 'health JSON missing bounded fields' in script


def test_verify_only_semantic_gate_rejects_stale_raw_dashboard(monkeypatch) -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    semantic = script.split('DASHBOARD_HEALTH="$DASHBOARD_HEALTH"', 1)[1]
    assert 'dashboard reward status does not match source state' not in semantic
    assert 'dashboard reward' in semantic
    assert 'status does not match source state' in semantic
    assert '0.88 avg over 5 sample(s)' in semantic
    assert 'materialize_synthesized_improvement' in semantic


def test_verify_only_has_no_mutation_or_health_wait_path() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert 'VERIFY-ONLY mode active. Skipping archive and upload.' in script
    assert 'if [ "$VERIFY_ONLY" -eq 0 ]; then' in script
    assert 'if [ "$NO_HEALTH_GATE" -eq 1 ] || [ "$DRY_RUN" -eq 1 ] || [ "$VERIFY_ONLY" -eq 1 ]' in script
    remote = script.split("<<'REMOTE'", 1)[1].split("\nREMOTE", 1)[0]
    assert 'if [ "$VERIFY_ONLY" -eq 0 ]; then' in remote
    assert 'if [ "$VERIFY_ONLY" -eq 1 ]; then' in remote
    assert 'VERIFY_ONLY HEALTH_FETCH_FAILED' in remote


def test_verify_only_no_mutation_end_to_end_sandbox(tmp_path):
    """Execute the remote heredoc in a sandbox and record all mock commands."""
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    remote = script.split("<<'REMOTE'", 1)[1].split("\nREMOTE", 1)[0]
    root = tmp_path / "sandbox"
    release = root / "opt/eeepc-agent/runtimes/self-evolving-agent/current"
    release.mkdir(parents=True)
    sha = "a" * 40
    (release / "SOURCE_COMMIT").write_text(sha + "\n", encoding="utf-8")
    commands = root / "commands.log"
    bindir = root / "bin"
    bindir.mkdir()

    def mock(name: str, body: str) -> None:
        _write_mock(bindir / name, body)

    log = shlex.quote(str(commands))
    mock("systemctl", f'''echo "systemctl $*" >> {log}
    case "$*" in *eeepc-network-fallback.*"-p LoadState"*) echo not-found;; *"-p LoadState"*) echo loaded;; *"-p MainPID"*) echo 4242;; *"-p ExecMainStartTimestamp"*) echo now;; *"is-enabled"*) echo enabled;; *"is-active"*eeepc-network-fallback.*) exit 1;; *"is-active"*) exit 0;; *) exit 97;; esac''')
    mock("sudo", f'''echo "sudo $*" >> {log}
    case "$1" in readlink) echo {shlex.quote(str(release))};; cat) if [[ "$2" == *SOURCE_COMMIT ]]; then echo {sha}; else printf "python3 /opt/eeepc-agent/runtimes/self-evolving-agent/current/scripts/eeebot_dashboard.py --serve --port 8080 --host 0.0.0.0\\0"; fi;; ss) echo 'LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("python3",pid=4242,fd=3))';; *) exit 97;; esac''')
    mock("ss", f'''echo "ss $*" >> {log}; echo 'LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("python3",pid=4242,fd=3))' ''')
    mock("curl", f'''echo "curl $*" >> {log}; case "$*" in *health*) echo '{{"overall":"WARN","dimensions":{{"reward":{{"status":"WARN","detail":"source=stale"}},"gate":{{"status":"WARN","detail":"source=stale"}}}},"goal":"stale","active_task":"stale","reward_average":"stale; age=100.0h (context-only artifact)"}}';; *) echo '{{"goal":"stale; age=100.0h (context-only artifact)","active_task":"stale; age=100.0h (context-only artifact)","approval_gate_state":"stale; age=100.0h (context-only artifact)","reward_average":"stale; age=100.0h (context-only artifact)","reward_source":{{"status":"stale","age_hours":100.0,"authoritative":false,"context_only":true}},"goal_source":{{"status":"stale","age_hours":100.0,"authoritative":false,"context_only":true}},"active_task_source":{{"status":"stale","age_hours":100.0,"authoritative":false,"context_only":true}},"approval_gate_source":{{"status":"stale","age_hours":100.0,"authoritative":false,"context_only":true}},"latest_report_path":null,"materialized_path":null}}';; esac''')
    mock("sleep", f'''echo "sleep $*" >> {log}; exit 97''')
    mock("stat", f'''echo "stat $*" >> {log}; echo 0:0''')

    remote_path = root / "remote.sh"
    remote_path.write_text(remote.replace("/opt/eeepc-agent", str(root / "opt/eeepc-agent")), encoding="utf-8")
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], VERIFY_ONLY="1", CURRENT_SYMLINK=str(release), PREV_RELEASE_PATH=str(release), FULL_COMMIT=sha, RELEASE_DIR=str(release))
    result = subprocess.run(["bash", str(remote_path)], cwd=root, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = commands.read_text(encoding="utf-8")
    for token in ("tar xzf", "scp ", "ln -sfn", "chown", "chmod", "daemon-reload", "restart", "stop ", "disable ", "sleep "):
        assert token not in seen, seen
    assert "curl" in seen and "api/health" in seen and "api/metrics" in seen


def test_socket_owner_check_reads_ss_with_sudo() -> None:
    """`ss -ltnp` prints the owning process only to root.

    Without sudo the listener row appears but its
    `users:(("python3",pid=...))` field does not, so the owner count reads 0
    and the gate fails a healthy deploy. Release 20260903T143835Z aborted on

        CRITICAL: :8080 must have exactly one owner, dashboard PID 16981

    while `sudo ss -ltnpH` on the same host showed exactly that pid and no
    other. Third read in this block to need a privilege it was missing, after
    the cwd and cmdline reads (#1246) — the class, not three coincidences.
    """
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "sudo ss -ltnpH" in script, (
        "the socket owner read must be sudo'd or it sees no owners at all")
    assert '"$(ss -ltnpH' not in script, "an unprivileged ss -p read cannot see owners"
    # The plain listener probe: `ss` failing is `unreadable`, an empty row set
    # from a successful `ss` is "listener not active" (#1259).
    assert 'DASHBOARD_PLAIN_ROWS="$(gate_read "ss -ltnH" sudo ss -ltnH |' in script


def test_socket_check_waits_for_the_listener_instead_of_sampling_once() -> None:
    """`systemctl restart` returns before the process has bound the port.

    Measured on the host: immediately after restart `:8080` has no listener,
    and it appears within about a second (4 polls at 0.25s in one run, under
    0.4s in another). Sampling once raced the bind and reported

        CRITICAL: :8080 must have exactly one owner, dashboard PID 20144;
                  saw listeners=0 pids=''

    on a healthy deploy (#1246). `listeners=0` is the tell: an unprivileged
    read still shows the listener row and only hides the owner, so zero rows
    means the socket genuinely was not bound yet — a different defect from the
    missing sudo, stacked behind it in the same check.
    """
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    socket_block = script[script.index("DASHBOARD_SOCKET_ROWS=\"\""):]
    socket_block = socket_block[: socket_block.index("DASHBOARD_LISTENER_COUNT=")]

    assert "for _ in $(seq 1 40)" in socket_block, "the listener read must be retried, not sampled once"
    assert "sleep 0.25" in socket_block, "the retry needs a bounded pause between polls"
    assert '[ -n "$DASHBOARD_SOCKET_ROWS" ] && break' in socket_block, (
        "the loop must stop as soon as a listener appears rather than always sleeping 10s")
    assert "sudo ss -ltnpH" in socket_block, "the retried read still needs the owner field"


def _remote_block(script: str) -> str:
    start = script.index("<<'REMOTE'")
    end = script.index("\nREMOTE\n", start)
    return script[start:end]


def _remote_helpers(script: str) -> str:
    """The `die` / `gate_read` definitions, cut from the remote block so a test
    can run them under a real bash with the same `set -eEuo pipefail`."""
    block = _remote_block(script)
    start = block.index("die() {")
    end = block.index('ARCHIVE="${REMOTE_ARCHIVE:-}"')
    return block[start:end]


def test_gate_reads_distinguish_unreadable_from_empty() -> None:
    """#1259: `cmd 2>/dev/null || true` collapsed a failed read into an empty
    value, and the gate then described the emptiness — `cwd is ''` meant either
    "wrong release" or "the read failed", and four deploys died on the second
    while the message said the first (#1246). Every gate read now goes through
    gate_read, and none of the four named reads keeps the collapsing shape."""
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    block = _remote_block(script)

    for what in ('"/proc/$DASHBOARD_PID/cwd"', '"/proc/$DASHBOARD_PID/cmdline"', '"ss -ltnpH"', '"ss -ltnH"', '"$CURRENT_SYMLINK"'):
        assert f"gate_read {what} sudo " in block, f"gate read missing for {what}"
    assert 'readlink "/proc/$DASHBOARD_PID/cwd" 2>/dev/null || true' not in block
    assert 'cmdline" 2>/dev/null | tr' not in block
    assert 'sudo ss -ltnpH 2>/dev/null' not in block
    assert 'sudo ss -ltnH 2>/dev/null' not in block
    assert 'readlink "$CURRENT_SYMLINK" || true' not in block
    assert 'unreadable: $what (exit $rc:' in block
    # The health gate's recorder read says "unreadable" once instead of polling
    # silently to a timeout; the decision it feeds is unchanged.
    assert 'sudo cat $EXIT_STREAK_PATH 2>/dev/null || true' not in script
    assert 'Health gate: unreadable: $EXIT_STREAK_PATH' in script


def test_every_remote_critical_goes_through_die_so_the_err_trap_fires() -> None:
    """`set -E` was already there and did not cover a single CRITICAL: the ERR
    trap fires on a FAILING command, and `exit 1` is not one, so every
    `echo CRITICAL; exit 1` skipped rollback_remote (#1246 left four releases
    half-activated). `die` returns 1 instead."""
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    block = _remote_block(script)
    code_lines = [line for line in block.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code_lines if "exit 1" in line], "a CRITICAL that exits bypasses the ERR trap; use die"
    assert "set -eEuo pipefail" in block
    assert "trap rollback_remote ERR" in block
    assert 'echo "CRITICAL: $*" >&2' in block and "return 1" in block


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_die_and_gate_read_semantics_under_the_remote_shell_options(tmp_path) -> None:
    """Run the real helper definitions under `set -eEuo pipefail` with an ERR
    trap, exactly as the remote block does, and show: (1) a failing gate read
    reports `unreadable` with the exit status and stderr, fires the trap and
    aborts — it does not hand the gate an empty value; (2) an EMPTY successful
    read is returned as-is (the gate decides what empty means); (3) `die`
    fires the trap; (4) the shape it replaces, `exit 1`, does not."""
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    helpers = _remote_helpers(script)

    def run(body: str) -> subprocess.CompletedProcess:
        test_script = tmp_path / "t.sh"
        test_script.write_text("set -eEuo pipefail\ntrap 'echo TRAP-FIRED >&2' ERR\n" + helpers + "\n" + body + "\n", newline="\n")
        return subprocess.run(["bash", str(test_script)], capture_output=True, text=True)

    # (1) failed read: unreadable, trap, abort, and the gate line never runs.
    res = run('V="$(gate_read "/proc/999999/cwd" bash -c \'echo boom >&2; exit 3\')"\necho "cwd is \'$V\'"')
    assert res.returncode == 3, res
    assert "CRITICAL: unreadable: /proc/999999/cwd (exit 3: boom" in res.stderr, res.stderr
    assert "TRAP-FIRED" in res.stderr
    assert "cwd is" not in res.stdout

    # (2) empty successful read: no CRITICAL, value handed on untouched.
    res = run('V="$(gate_read "x" true)"\necho "got [$V]"')
    assert res.returncode == 0, res
    assert res.stdout.strip() == "got []"
    assert "CRITICAL" not in res.stderr

    # (2b) NUL bytes survive to a downstream `tr` — /proc/<pid>/cmdline is
    # NUL-separated, and a first draft that captured into a variable before
    # `tr` produced `python3/opt/…--serve--port8080` on the healthy host.
    res = run('V="$(gate_read "cmdline" printf \'python3\\0--serve\\0\' | tr "\\0" " ")"\necho "got [$V]"')
    assert res.returncode == 0, res
    assert res.stdout.strip() == "got [python3 --serve ]"  # trailing NUL → trailing space

    # (3) die fires the trap and aborts with 1; (4) exit 1 inside a function does not.
    res = run('f() { die "no good"; }\nf\necho unreachable')
    assert res.returncode == 1 and "CRITICAL: no good" in res.stderr and "TRAP-FIRED" in res.stderr and "unreachable" not in res.stdout
    res = run('g() { echo "CRITICAL: old shape" >&2; exit 1; }\ng')
    assert res.returncode == 1 and "TRAP-FIRED" not in res.stderr
