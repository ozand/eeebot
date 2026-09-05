#!/usr/bin/env bash
# =============================================================================
# eeepc deploy: push a new code release to the running host and activate it
#
# Usage (from dev machine, not on eeepc):
#   bash host/eeepc/scripts/deploy_release.sh [--host eeepc] [--dry-run] [--ref <sha>] [--health-timeout <min>] [--no-crash-hold <sec>] [--no-health-gate] [--allow-dirty]
#
# What it does:
#   1. Bundles current repo HEAD into a timestamped release archive
#   2. Sends it to eeepc via scp
#   3. On eeepc: extracts, creates venv, symlinks current, restarts agent
#   4. Post-deploy health gate check
#
# Prerequisites on dev machine:
#   - ssh access to eeepc (key-based)
#   - gh CLI authenticated
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
# #1303: the bridge's exit-status classes, shared with the remote activation
# check (which sources the same file from the extracted release).
. "$(dirname "${BASH_SOURCE[0]}")/lib_bridge_exit.sh"
HOST="eeepc"
DRY_RUN=0
REF=""
# #1163: the gate's positive signals no longer wait for a terminal ledger row
# (P90 167 min, so 24% of gated deploys ended UNKNOWN at 25 min). Measured on
# the host 2026-09-02: `systemctl restart` in step 3 blocks until the new
# release's first run ends, the timer (OnUnitInactiveSec=3m) fires the next run
# ~3 min after that, and a normal run's median wall time is 1.6 min — so a
# post-flip `Finished` line lands ~4.6 min after the gate arms, and the weaker
# no-crash verdict at first-invocation + NO_CRASH_HOLD (~8 min). 10 min covers
# both with margin; a run longer than that is TimeoutStartSec=3300 territory.
HEALTH_TIMEOUT=10
# Seconds a post-flip invocation must run without a crash before the weaker
# NO-CRASH verdict is reported (the 2026-09-01 crash loop died ~30 s in).
NO_CRASH_HOLD=300
NO_HEALTH_GATE=0
ALLOW_DIRTY=0

VERIFY_ONLY=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --host)           HOST="$2"; shift 2 ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --verify-only)    VERIFY_ONLY=1; shift ;;
    --ref)            REF="$2"; shift 2 ;;
    --health-timeout) HEALTH_TIMEOUT="$2"; shift 2 ;;
    --no-crash-hold) NO_CRASH_HOLD="$2"; shift 2 ;;
    --no-health-gate) NO_HEALTH_GATE=1; shift ;;
    --allow-dirty)    ALLOW_DIRTY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

log()  { echo "[deploy] $*"; }
run()  { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY] $*"; else "$@"; fi; }

if [ "$VERIFY_ONLY" -eq 0 ]; then
  # Ref verification
  if [ -z "$REF" ]; then
    COMMIT=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
    # Verify against origin/main
    ORIGIN_COMMIT=$(git -C "$REPO_ROOT" rev-parse --short origin/main 2>/dev/null || true)
    if [ "$COMMIT" != "$ORIGIN_COMMIT" ]; then
      echo "CRITICAL: local HEAD ($COMMIT) != origin/main ($ORIGIN_COMMIT)." >&2
      echo "Refuse to deploy unless the archived ref matches origin/main, or --ref is explicitly passed." >&2
      exit 1
    fi
  else
    COMMIT=$(git -C "$REPO_ROOT" rev-parse --short "$REF")
  fi

  if [ "$ALLOW_DIRTY" -eq 0 ]; then
    if ! git -C "$REPO_ROOT" diff-index --quiet HEAD --; then
      echo "CRITICAL: Working tree is dirty. Refuse to deploy unless --allow-dirty is passed." >&2
      exit 1
    fi
  fi

  BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref "$COMMIT" 2>/dev/null || echo "detached")
  SUBJECT=$(git -C "$REPO_ROOT" log -1 --format="%s" "$COMMIT")
  TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
  RELEASE_NAME="${TIMESTAMP}-canonical-${COMMIT}"
  FULL_COMMIT="$(git -C "$REPO_ROOT" rev-parse "$COMMIT^{commit}")"
  ARCHIVE="/tmp/eeebot-release-${RELEASE_NAME}.tar.gz"

  log "repo:    $REPO_ROOT"
  log "commit:  $COMMIT ($SUBJECT) ($BRANCH)"
  log "full commit: $FULL_COMMIT"
  log "release: $RELEASE_NAME"
  log "host:    $HOST"

  # 1. Create archive from git COMMIT (excludes .git, .venv, __pycache__)
  log "creating archive..."
  run git -C "$REPO_ROOT" archive --format=tar.gz --prefix="${RELEASE_NAME}/" "$COMMIT" \
    -o "$ARCHIVE"
  log "archive: $ARCHIVE ($(du -sh "$ARCHIVE" | cut -f1))"

  # 2. Copy to host
  REMOTE_ARCHIVE="/tmp/$(basename "$ARCHIVE")"
  log "uploading to $HOST:$REMOTE_ARCHIVE..."
  run scp "$ARCHIVE" "ozand@${HOST}:${REMOTE_ARCHIVE}"
else
  log "host:    $HOST"
  log "VERIFY-ONLY mode active. Skipping archive and upload."
  REMOTE_ARCHIVE=""
  RELEASE_NAME=""
  FULL_COMMIT=""
fi

# Resolve PREV_RELEASE so we can roll back if needed
PREV_RELEASE_PATH=$(ssh "ozand@${HOST}" "readlink /opt/eeepc-agent/runtimes/self-evolving-agent/current || true")
PREV_RELEASE_PATH="${PREV_RELEASE_PATH//$'\r'/}"
if [ -z "$PREV_RELEASE_PATH" ]; then
  echo "CRITICAL: could not resolve previous current release for rollback" >&2
  exit 1
fi

rollback_release() {
  log "Rolling back to $PREV_RELEASE_PATH..."
  ssh "ozand@${HOST}" "sudo ln -sfn \"$PREV_RELEASE_PATH\" /opt/eeepc-agent/runtimes/self-evolving-agent/current && sudo systemctl restart eeebot-dashboard.service && sudo systemctl restart eeepc-self-evolving-subagent-bridge.service"
}

# 3. Extract, build venv, update symlink, restart
log "installing on $HOST..."
run ssh "ozand@${HOST}" "REMOTE_ARCHIVE='${REMOTE_ARCHIVE:-}' RELEASE_NAME='${RELEASE_NAME:-}' FULL_COMMIT='${FULL_COMMIT:-}' PREV_RELEASE_PATH='${PREV_RELEASE_PATH:-}' VERIFY_ONLY='$VERIFY_ONLY' bash -s" <<'REMOTE'
# Make ERR trap inherit to shell functions (the rollback trap)
set -eEuo pipefail

# `set -E` alone does not make the rollback trap cover a CRITICAL: the ERR trap
# fires on a command that FAILS, and `exit 1` is not a failing command, so
# every `echo CRITICAL; exit 1` in this block left `current` flipped with no
# rollback (four half-activated releases on 2026-09-03, #1246). `die` returns
# non-zero instead: under `set -eE` that runs the ERR trap (rollback_remote,
# once installed) and then aborts the block with status 1. Fail direction of
# every CRITICAL below: abort, and roll back if `current` was flipped.
die() {
  echo "CRITICAL: $*" >&2
  return 1
}

# Every read that feeds a gate goes through this (#1259). `cmd 2>/dev/null ||
# true` collapsed "the read failed" into "the value is empty" and the gate then
# described the emptiness: `cwd is ''` meant EITHER wrong release OR a failed
# read, and the four 2026-09-03 deploys died on the second while the message
# said the first. gate_read prints the command's stdout unchanged (an EMPTY
# successful read is still returned — the consuming gate decides what empty
# means); a non-zero exit is reported as `unreadable: <what> (exit N: <stderr
# tail>)` and propagates through the assignment, so `set -e` aborts and the ERR
# trap rolls back. Fail direction: a gate that cannot read must not guess.
gate_read() {
  local what="$1"; shift
  local out err rc=0
  out="$(mktemp)"; err="$(mktemp)"
  # Stream through a file, not a variable: a bash command substitution drops
  # NUL bytes, and /proc/<pid>/cmdline is NUL-separated — capturing it here
  # would hand the caller's `| tr "\0" " "` nothing to convert (seen on the
  # healthy host as `python3/opt/…--serve--port8080`, via --verify-only).
  if "$@" >"$out" 2>"$err"; then rc=0; else rc=$?; fi
  if [ "$rc" -ne 0 ]; then
    echo "CRITICAL: unreadable: $what (exit $rc: $(tr '\n' ' ' <"$err" | tail -c 200))" >&2
    rm -f "$out" "$err"
    return "$rc"
  fi
  cat "$out"
  rm -f "$out" "$err"
}

ARCHIVE="${REMOTE_ARCHIVE:-}"
RELEASE_NAME="${RELEASE_NAME:-}"
FULL_COMMIT="${FULL_COMMIT:-}"
PREV_RELEASE_PATH="${PREV_RELEASE_PATH:-}"
VERIFY_ONLY="${VERIFY_ONLY:-0}"

RELEASES_DIR=/opt/eeepc-agent/runtimes/self-evolving-agent/releases
CURRENT_SYMLINK=/opt/eeepc-agent/runtimes/self-evolving-agent/current
VENV_BASE=/opt/eeepc-agent/runtimes/self-evolving-agent/venv

if [ "$VERIFY_ONLY" -eq 1 ]; then
  echo "[remote] VERIFY-ONLY mode: using current live release for checks"
  RELEASE_DIR="$(gate_read "$CURRENT_SYMLINK" sudo readlink -v "$CURRENT_SYMLINK")"
  if [ -z "$RELEASE_DIR" ]; then
    die "current release symlink $CURRENT_SYMLINK resolved to an empty target in verify-only mode"
  fi
  if [ ! -f "$RELEASE_DIR/SOURCE_COMMIT" ]; then
    die "no SOURCE_COMMIT found in current release $RELEASE_DIR"
  fi
  FULL_COMMIT="$(sudo cat "$RELEASE_DIR/SOURCE_COMMIT")"
else
  RELEASE_DIR="$RELEASES_DIR/$RELEASE_NAME"

  echo "[remote] extracting to $RELEASE_DIR"
  sudo mkdir -p "$RELEASES_DIR"
  cd /tmp
  sudo tar xzf "$ARCHIVE" -C "$RELEASES_DIR"
  echo "$FULL_COMMIT" | sudo tee "$RELEASE_DIR/SOURCE_COMMIT" > /dev/null
  if [ "$(cat "$RELEASE_DIR/SOURCE_COMMIT")" != "$FULL_COMMIT" ]; then
    die "SOURCE_COMMIT does not match full deployed commit"
  fi

  echo "[remote] linking venv into release"
  sudo ln -sfn "/opt/eeepc-agent/venv" "$RELEASE_DIR/.venv"

  echo "[remote] syncing operator presets (#906)"
  sudo mkdir -p /etc/eeepc-agent/presets
  sudo cp "$RELEASE_DIR/host/eeepc/etc/presets/"*.env /etc/eeepc-agent/presets/ 2>/dev/null || true

  echo "[remote] migrating goal priorities to derived_priorities.json (#944)"
  STATE_DIR=/var/lib/eeepc-agent/self-evolving-agent/state
  sudo mkdir -p "$STATE_DIR/goals"
  DERIVED="$STATE_DIR/goals/derived_priorities.json"
  GOAL_TEXT="$STATE_DIR/goals/goal_text.json"
  if [ -f "$GOAL_TEXT" ]; then
    sudo python3 "$RELEASE_DIR/host/eeepc/scripts/migrate_goal_priorities.py" "$GOAL_TEXT" "$DERIVED"
    sudo chown eeepc-agent:eeepc-agent "$DERIVED"
  elif [ ! -f "$DERIVED" ]; then
    echo "[remote] no legacy or derived priorities found; goal_review will mint from the charter"
  fi

  echo "[remote] fixing ownership and permissions on release"
  sudo chown -R root:root "$RELEASE_DIR" "$VENV_BASE"

  sudo chown root:root /opt/eeepc-agent
  sudo chown root:root /opt/eeepc-agent/runtimes
  sudo chown root:root /opt/eeepc-agent/runtimes/self-evolving-agent
  sudo chown root:root "$RELEASES_DIR"
  sudo chown -h root:root "$RELEASE_DIR/.venv"
  sudo chown root:root /opt/eeepc-agent/venv

  sudo chmod -R go-w "$RELEASE_DIR"

  if [ "$(stat -c '%u:%g' "$RELEASE_DIR")" != "0:0" ]; then
    die "$RELEASE_DIR is not owned by root:root"
  fi
  if [ "$(stat -c '%u:%g' /opt/eeepc-agent/runtimes/self-evolving-agent)" != "0:0" ]; then
    die "/opt/eeepc-agent/runtimes/self-evolving-agent is not owned by root:root"
  fi

  echo "[remote] updating current symlink"
  sudo ln -sfn "$RELEASE_DIR" "$CURRENT_SYMLINK"
  sudo chown -h root:root "$CURRENT_SYMLINK"
fi

# Once current has moved, every later remote failure must restore the previous
# release immediately; never leave a failed release active (#1236).
rollback_remote() {
  # A failing gate_read raises ERR twice: once inside the `$(...)` subshell
  # (where nothing set here would survive) and once in the parent for the
  # failed assignment. Act only in the parent, and only once.
  if [ "${BASH_SUBSHELL:-0}" -gt 0 ] || [ -n "${ROLLBACK_DONE:-}" ]; then
    return
  fi
  ROLLBACK_DONE=1
  if [ "$VERIFY_ONLY" -eq 1 ]; then
    echo "[remote] check failed in verify-only mode; leaving live release alone" >&2
    return
  fi
  echo "[remote] rollback after activation failure: $PREV_RELEASE_PATH" >&2
  sudo ln -sfn "$PREV_RELEASE_PATH" "$CURRENT_SYMLINK"
  sudo systemctl restart eeebot-dashboard.service 2>/dev/null || true
  sudo systemctl restart eeepc-self-evolving-subagent-bridge.service 2>/dev/null || true
}
trap rollback_remote ERR

if [ "$(stat -c '%u:%g' "$CURRENT_SYMLINK")" != "0:0" ]; then
  die "$CURRENT_SYMLINK is not owned by root:root"
fi

echo "[remote] goals.md available at: $RELEASE_DIR/goals.md"

if [ "$VERIFY_ONLY" -eq 0 ]; then
  sudo mkdir -p /var/lib/eeepc-promoted
  sudo chown root:eeepc-agent /var/lib/eeepc-promoted
  sudo chmod 0755 /var/lib/eeepc-promoted

  sudo mkdir -p /var/lib/eeepc-agent/self-evolving-agent/state/validator_harness \
    /var/lib/eeepc-agent/self-evolving-agent/state/curator \
    /var/lib/eeepc-agent/self-evolving-agent/state/action_index \
    /var/lib/eeepc-agent/self-evolving-agent/state/reflector
  sudo chown eeepc-agent:eeepc-agent \
    /var/lib/eeepc-agent/self-evolving-agent/state/validator_harness \
    /var/lib/eeepc-agent/self-evolving-agent/state/curator \
    /var/lib/eeepc-agent/self-evolving-agent/state/action_index \
    /var/lib/eeepc-agent/self-evolving-agent/state/reflector

  echo "[remote] syncing libexec scripts from release"
  sudo cp "$RELEASE_DIR/host/eeepc/libexec/"*.py /usr/local/libexec/
  sudo rm -f /usr/local/libexec/eeepc-self-evolving-subagent-bridge.py
  sudo chmod +x /usr/local/libexec/*.py

  echo "[remote] purging retired ghost units (#1037)"
  for ghost_unit in eeepc-network-fallback.timer eeepc-network-fallback.service; do
    ghost_load_state="$(systemctl show "$ghost_unit" -p LoadState --value)"
    if [ "$ghost_load_state" != "not-found" ]; then
      if systemctl is-active --quiet "$ghost_unit"; then
        sudo systemctl stop "$ghost_unit"
      fi
      ghost_file_state="$(systemctl show "$ghost_unit" -p UnitFileState --value)"
      case "$ghost_file_state" in
        enabled|enabled-runtime|linked|linked-runtime|alias|generated|transient)
          sudo systemctl disable "$ghost_unit"
          ;;
      esac
    fi
  done

  sudo rm -f /etc/systemd/system/eeepc-network-fallback.timer /etc/systemd/system/eeepc-network-fallback.service
  sudo systemctl daemon-reload
fi

for ghost_unit in eeepc-network-fallback.timer eeepc-network-fallback.service; do
  ghost_load_state="$(systemctl show "$ghost_unit" -p LoadState --value)"
  if [ "$ghost_load_state" != "not-found" ]; then
    die "$ghost_unit remains loaded after purge (state: $ghost_load_state)"
  fi
  if systemctl is-active --quiet "$ghost_unit"; then
    die "$ghost_unit is still active after purge"
  fi
done
if [ -e /etc/systemd/system/eeepc-network-fallback.timer ] || [ -e /etc/systemd/system/eeepc-network-fallback.service ]; then
  die "ghost unit files still present on disk"
fi

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "[remote] syncing systemd units + reloading"
  sudo cp "$RELEASE_DIR/host/eeepc/systemd/"*.service "$RELEASE_DIR/host/eeepc/systemd/"*.timer /etc/systemd/system/
  sudo systemctl daemon-reload
fi

sync_timer() {
  local timer="$1"
  local required="${2:-optional}"

  local load_state
  load_state="$(systemctl show "$timer" -p LoadState --value)"
  if [ "$load_state" = "not-found" ]; then
    echo "WARNING: unit file $timer not found" >&2
    if [ "$required" = "required" ]; then
      return 1
    fi
    return 0
  fi

  local initial_state
  initial_state="$(systemctl is-enabled "$timer" 2>/dev/null || true)"

  if [ "$initial_state" = "enabled" ]; then
    if [ "$VERIFY_ONLY" -eq 0 ]; then
      sudo systemctl restart "$timer"
    fi
    local final_state
    final_state="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
    if [ "$final_state" != "enabled" ]; then
      echo "CRITICAL: $timer expected enabled, got $final_state" >&2
      return 1
    fi
    if ! systemctl is-active --quiet "$timer"; then
      echo "CRITICAL: $timer is not active after restart" >&2
      return 1
    fi
  elif [ "$initial_state" = "disabled" ]; then
    if [ "$required" = "required" ]; then
      if [ "$VERIFY_ONLY" -eq 1 ]; then
        echo "CRITICAL: required timer $timer is disabled, failing verify-only mode" >&2
        return 1
      fi
      echo "[remote] enabling required timer $timer"
      sudo systemctl enable --now "$timer"
      local final_state
      final_state="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
      if [ "$final_state" != "enabled" ]; then
        echo "CRITICAL: required timer $timer failed to enable (state: $final_state)" >&2
        return 1
      fi
      if ! systemctl is-active --quiet "$timer"; then
        echo "CRITICAL: required timer $timer failed to become active" >&2
        return 1
      fi
    else
      echo "NOTICE: $timer is administratively disabled; preserving disabled state"
      local final_state
      final_state="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
      if [ "$final_state" != "disabled" ]; then
        echo "CRITICAL: $timer expected disabled, got $final_state" >&2
        return 1
      fi
      if systemctl is-active --quiet "$timer"; then
        echo "CRITICAL: disabled timer $timer is unexpectedly active" >&2
        return 1
      fi
    fi
  elif [ "$initial_state" = "masked" ]; then
    if [ "$required" = "required" ]; then
      echo "CRITICAL: required timer $timer is masked!" >&2
      return 1
    fi
    echo "NOTICE: $timer is masked; preserving masked state"
    local final_state
    final_state="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
    if [ "$final_state" != "masked" ]; then
      echo "CRITICAL: $timer expected masked, got $final_state" >&2
      return 1
    fi
    if systemctl is-active --quiet "$timer"; then
      echo "CRITICAL: masked timer $timer is unexpectedly active" >&2
      return 1
    fi
  else
    if [ "$VERIFY_ONLY" -eq 1 ]; then
      echo "CRITICAL: $timer state is $initial_state, failing verify-only mode" >&2
      return 1
    fi
    echo "[remote] enabling standard timer $timer (state was: $initial_state)"
    sudo systemctl enable --now "$timer"
    local final_state
    final_state="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
    if [ "$final_state" != "enabled" ]; then
      echo "CRITICAL: $timer failed to enable (state: $final_state)" >&2
      return 1
    fi
    if ! systemctl is-active --quiet "$timer"; then
      echo "CRITICAL: $timer failed to become active" >&2
      return 1
    fi
  fi

  return 0
}

sync_timer eeepc-promotion-verifier.timer required
sync_timer eeebot-knowledge-curator.timer optional
sync_timer eeebot-action-index.timer optional
sync_timer eeebot-reflector.timer optional
sync_timer eeebot-strategist.timer optional

# Activate the long-running dashboard against the new current release. Unlike
# the static oneshot bridge, it keeps the old Python process alive across a
# symlink flip unless explicitly restarted (#1236).
DASHBOARD_UNIT=eeebot-dashboard.service
DASHBOARD_PREV_PID="$(systemctl show "$DASHBOARD_UNIT" -p MainPID --value 2>/dev/null || true)"
DASHBOARD_PREV_START="$(systemctl show "$DASHBOARD_UNIT" -p ExecMainStartTimestamp --value 2>/dev/null || true)"
DASHBOARD_LOAD_STATE="$(systemctl show "$DASHBOARD_UNIT" -p LoadState --value 2>/dev/null || true)"
DASHBOARD_UNIT_STATE="$(systemctl is-enabled "$DASHBOARD_UNIT" 2>/dev/null || true)"
if [ "$DASHBOARD_LOAD_STATE" = "loaded" ]; then
  if [ "$DASHBOARD_UNIT_STATE" = "enabled" ]; then
    if [ "$VERIFY_ONLY" -eq 1 ]; then
      echo "[remote] verify-only mode: checking $DASHBOARD_UNIT without restart"
      if ! systemctl is-active --quiet "$DASHBOARD_UNIT"; then
        die "$DASHBOARD_UNIT is not active"
      fi
      DASHBOARD_PID="$(systemctl show "$DASHBOARD_UNIT" -p MainPID --value)"
      DASHBOARD_START="$(systemctl show "$DASHBOARD_UNIT" -p ExecMainStartTimestamp --value)"
    else
      echo "[remote] restarting $DASHBOARD_UNIT after current activation"
      DASHBOARD_PREV_PID="$(systemctl show "$DASHBOARD_UNIT" -p MainPID --value)"
      DASHBOARD_PREV_START="$(systemctl show "$DASHBOARD_UNIT" -p ExecMainStartTimestamp --value)"
      sudo systemctl restart "$DASHBOARD_UNIT"
      if ! systemctl is-active --quiet "$DASHBOARD_UNIT"; then
        die "$DASHBOARD_UNIT is not active after restart"
      fi
      DASHBOARD_PID="$(systemctl show "$DASHBOARD_UNIT" -p MainPID --value)"
      DASHBOARD_START="$(systemctl show "$DASHBOARD_UNIT" -p ExecMainStartTimestamp --value)"
      if [ "$DASHBOARD_PID" = "$DASHBOARD_PREV_PID" ] && [ "$DASHBOARD_START" = "$DASHBOARD_PREV_START" ]; then
        die "$DASHBOARD_UNIT restart did not produce a new process identity"
      fi
    fi
    # /proc/<pid>/cwd and cmdline belong to the unit's user, not to the
    # deploying account, so both reads need sudo. Without it readlink returns
    # empty and the check below fails a healthy deploy: observed on release
    # 20260903T141455Z, where the dashboard was correctly running from the new
    # release and the verification could not see it (#1245).
    # Both reads go through gate_read (#1259): a failed read — the process
    # exited between `systemctl show` and here, /proc unreadable, sudo denied —
    # aborts as `unreadable: /proc/<pid>/cwd (exit N: <stderr>)`, never as the
    # `cwd is ''` text below, which now means exactly one thing: the process
    # runs from somewhere other than the release.
    DASHBOARD_CWD="$(gate_read "/proc/$DASHBOARD_PID/cwd" sudo readlink -v "/proc/$DASHBOARD_PID/cwd")"
    # `sudo tr ... < file` would not work: the shell opens the redirect as the
    # deploying user before sudo runs. The read itself has to be the sudo'd
    # command; gate_read streams it (see there) so the NULs reach `tr`.
    DASHBOARD_CMDLINE="$(gate_read "/proc/$DASHBOARD_PID/cmdline" sudo cat "/proc/$DASHBOARD_PID/cmdline" | tr "\0" " ")"
    if [ "$VERIFY_ONLY" -eq 0 ] && [ "$DASHBOARD_PID" = "$DASHBOARD_PREV_PID" ] && [ "$DASHBOARD_START" = "$DASHBOARD_PREV_START" ]; then
      die "$DASHBOARD_UNIT restart did not produce a new process identity"
    fi
    if [ "$DASHBOARD_CWD" != "$RELEASE_DIR" ]; then
      die "$DASHBOARD_UNIT PID $DASHBOARD_PID cwd is '$DASHBOARD_CWD', expected '$RELEASE_DIR'"
    fi
    if [ "$VERIFY_ONLY" -eq 0 ] && [ "$(cat "$RELEASE_DIR/SOURCE_COMMIT")" != "$FULL_COMMIT" ]; then
      die "activated release SOURCE_COMMIT does not equal full requested SHA"
    fi
    # The unit's ExecStart names the `current` symlink; the cwd check resolves
    # that symlink to the release, while this exact check pins script and args.
    case "$DASHBOARD_CMDLINE" in
      *"/current/scripts/eeebot_dashboard.py --serve --port 8080 --host 0.0.0.0"*) : ;;
      *)
        die "$DASHBOARD_UNIT PID $DASHBOARD_PID has unexpected command line: $DASHBOARD_CMDLINE"
        ;;
    esac
    echo "[remote] $DASHBOARD_UNIT active: MainPID=$DASHBOARD_PID start=$DASHBOARD_START previous_pid=$DASHBOARD_PREV_PID previous_start=$DASHBOARD_PREV_START cwd=$DASHBOARD_CWD source=$FULL_COMMIT"

    # Semantic dashboard gate: listener + valid JSON + bounded/no-leak fields.
    # `ss -ltnp` prints the owning process only to root: without sudo the
    # listener row is there but its users:(("python3",pid=...)) field is not,
    # so the owner count reads 0 and this gate fails a healthy deploy — the
    # third read in this block to need the privilege it was missing (#1246).
    # `systemctl restart` returns before the process binds :8080. Wait bounded
    # for the listener in normal deploy mode; verify-only cannot reproduce the
    # bind race because it deliberately does not restart the service.
    # `ss` itself failing (missing binary, sudo denied) is `unreadable: ss
    # -ltnpH`; an empty row set from a successful `ss` is the listener not being
    # up, and the owner gate below says so with the counts it saw.
    DASHBOARD_SOCKET_ROWS=""
    for _ in $(seq 1 40); do
      DASHBOARD_SOCKET_ROWS="$(gate_read "ss -ltnpH" sudo ss -ltnpH | awk '$4 ~ /:8080$/')"
      [ -n "$DASHBOARD_SOCKET_ROWS" ] && break
      sleep 0.25
    done
    DASHBOARD_LISTENER_COUNT="$(printf '%s\n' "$DASHBOARD_SOCKET_ROWS" | sed '/^$/d' | wc -l)"
    DASHBOARD_SOCKET_PIDS="$(printf '%s\n' "$DASHBOARD_SOCKET_ROWS" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
    DASHBOARD_SOCKET_PID_COUNT="$(printf '%s\n' "$DASHBOARD_SOCKET_PIDS" | sed '/^$/d' | wc -l)"
    if [ "$DASHBOARD_LISTENER_COUNT" != "1" ] || [ "$DASHBOARD_SOCKET_PID_COUNT" != "1" ] || [ "$DASHBOARD_SOCKET_PIDS" != "$DASHBOARD_PID" ]; then
      die ":8080 must have exactly one owner, dashboard PID $DASHBOARD_PID; saw listeners=$DASHBOARD_LISTENER_COUNT pids='$DASHBOARD_SOCKET_PIDS'"
    fi
    DASHBOARD_PLAIN_ROWS="$(gate_read "ss -ltnH" sudo ss -ltnH | awk '$4 ~ /:8080$/')"
    if [ -z "$DASHBOARD_PLAIN_ROWS" ]; then
      die "dashboard listener :8080 is not active"
    fi
    DASHBOARD_HEALTH="$(curl --fail --silent --show-error http://127.0.0.1:8080/api/health 2>/dev/null || true)"
    DASHBOARD_METRICS="$(curl --fail --silent --show-error http://127.0.0.1:8080/api/metrics 2>/dev/null || true)"
    if [ -z "$DASHBOARD_HEALTH" ] || [ -z "$DASHBOARD_METRICS" ]; then
      if [ "$VERIFY_ONLY" -eq 1 ]; then
        echo "VERIFY_ONLY HEALTH_FETCH_FAILED" >&2
      fi
      die "could not fetch health or metrics from :8080"
    fi
    # #1270: the page a person actually reads. /api/health and /api/metrics
    # are built by a different code path from the HTML renderer, so a
    # KeyError in the renderer shipped behind a green gate and / returned
    # 500 for 25 minutes. Status code and a body-size floor only — never
    # markup: an unhandled exception yields a non-200, a broken renderer
    # yields a stub, and coupling the gate to HTML content is how a gate
    # gets routed around. gate_read keeps #1259's distinction: curl that
    # cannot connect is `unreadable: …/ (exit 7: …)`, a page that answers
    # with an error is reported as its status code. No --fail here: a 500
    # must reach the status check, not the unreadable branch.
    DASHBOARD_PAGE_BODY="$(mktemp)"
    DASHBOARD_PAGE_STATUS="$(gate_read "http://127.0.0.1:8080/" curl --silent --show-error --max-time 30 --output "$DASHBOARD_PAGE_BODY" --write-out '%{http_code}' http://127.0.0.1:8080/)"
    DASHBOARD_PAGE_BYTES="$(wc -c <"$DASHBOARD_PAGE_BODY" | tr -d ' ')"
    rm -f "$DASHBOARD_PAGE_BODY"
    if [ "$DASHBOARD_PAGE_STATUS" != "200" ]; then
      die "dashboard page / returned HTTP $DASHBOARD_PAGE_STATUS ($DASHBOARD_PAGE_BYTES bytes); the HTML renderer is broken while /api/* may still be healthy"
    fi
    # Live page is ~18.8 KB; the floor catches a stub or an error page that
    # somehow carries a 200, and nothing a working renderer produces.
    if [ "$DASHBOARD_PAGE_BYTES" -lt 1024 ]; then
      die "dashboard page / body is $DASHBOARD_PAGE_BYTES bytes (< 1024); the renderer produced a stub"
    fi
    echo "[remote] dashboard page / HTTP $DASHBOARD_PAGE_STATUS, $DASHBOARD_PAGE_BYTES bytes"
    DASHBOARD_HEALTH="$DASHBOARD_HEALTH" DASHBOARD_METRICS="$DASHBOARD_METRICS" python3 - <<'PY'
import json
import os

health = json.loads(os.environ["DASHBOARD_HEALTH"])
metrics = json.loads(os.environ["DASHBOARD_METRICS"])
required_health = {"overall", "dimensions", "goal", "active_task", "reward_average"}
required_metrics = {"goal", "active_task", "approval_gate_state", "reward_source", "goal_source", "active_task_source", "approval_gate_source"}
source_keys = ("goal_source", "active_task_source", "approval_gate_source", "reward_source")
for payload, required in ((health, required_health), (metrics, required_metrics)):
    if not all(isinstance(payload.get(key), (str, dict, int, float, list)) for key in required):
        raise SystemExit("dashboard endpoint has invalid bounded field types")
if not required_health <= health.keys() or not isinstance(health["dimensions"], dict):
    raise SystemExit("dashboard health JSON missing bounded fields")
if not required_metrics <= metrics.keys():
    raise SystemExit("dashboard metrics JSON missing bounded fields")
raw_markers = ("evolution-", "materialized-cycle-", "reward_signal")
payload = json.dumps({"health": health, "metrics": metrics}, sort_keys=True)
if any(marker in payload for marker in raw_markers):
    raise SystemExit("dashboard endpoint contains raw retired artifact payload")
for key in source_keys:
    source = metrics.get(key)
    if not isinstance(source, dict) or not {"status", "age_hours", "authoritative", "context_only"} <= source.keys():
        raise SystemExit("dashboard endpoint missing bounded source metadata")
    if source["authoritative"] is not False or source["context_only"] is not True:
        raise SystemExit("dashboard endpoint source authority flags are unsafe")
    if source["status"] not in {"fresh", "stale", "missing", "permission", "unreadable", "malformed", "valid-empty"}:
        raise SystemExit("dashboard endpoint source status is invalid")
    if source["status"] != "fresh" and source.get("age_hours") is not None and not isinstance(source["age_hours"], (int, float)):
        raise SystemExit("dashboard endpoint source age is invalid")
if metrics.get("latest_report_path") is not None or metrics.get("materialized_path") is not None:
    raise SystemExit("dashboard endpoint exposes an artifact path")
source_status = {
    key: metrics[key]["status"] for key in source_keys
}
for dimension, source_key in (("reward", "reward_source"), ("gate", "approval_gate_source")):
    detail = health.get("dimensions", {}).get(dimension, {})
    if not isinstance(detail, dict) or detail.get("status") not in {"WARN", "OK", "CRIT"}:
        raise SystemExit("dashboard health dimension is not structured")
    expected = "OK" if source_status[source_key] == "fresh" else "WARN"
    if detail.get("status") != expected:
        raise SystemExit(f"dashboard {dimension} status does not match source state")
    if source_status[source_key] != "fresh" and "source=" + source_status[source_key] not in detail.get("detail", ""):
        raise SystemExit(f"dashboard {dimension} detail lacks bounded source state")
if source_status["reward_source"] != "fresh" and metrics.get("reward_average") != metrics["reward_source"].get("status") + "; age=" + str(metrics["reward_source"].get("age_hours")) + "h (context-only artifact)":
    raise SystemExit("dashboard reward payload is not bounded")
if source_status["approval_gate_source"] != "fresh" and metrics.get("approval_gate_state", "").startswith("materialize_"):
    raise SystemExit("dashboard gate payload is not bounded")
if "0.88 avg over 5 sample(s)" in payload or "materialize_synthesized_improvement" in payload:
    raise SystemExit("dashboard endpoint contains raw legacy dashboard values")
# The bare host-path prefixes were in this list and rejected the healthy
# live payload: `operator_attention` legitimately reports
# `archive=/var/lib/eeepc-agent/self-evolving-agent/state/subagents/archive`,
# which is an operational detail, not a leaked retired artifact. A host path
# appearing anywhere is not evidence of staleness — the frozen cycle id and
# the retired reward/gate strings are. Caught by `--verify-only` against a
# healthy host before it could abort a deploy, which is what that mode is for.
if any(token in payload for token in ("cycle-2f305bf18b42", "0.88 avg over 5 sample(s)", "materialize_synthesized_improvement")):
    raise SystemExit("dashboard endpoint contains a stale cycle id or retired artifact value")
PY
  elif [ "$DASHBOARD_UNIT_STATE" = "disabled" ] || [ "$DASHBOARD_UNIT_STATE" = "masked" ]; then
    echo "NOTICE: $DASHBOARD_UNIT is $DASHBOARD_UNIT_STATE; preserving state"
  else
    die "$DASHBOARD_UNIT is loaded but not enabled (state: $DASHBOARD_UNIT_STATE)"
  fi
elif [ "$DASHBOARD_LOAD_STATE" = "not-found" ] || [ -z "$DASHBOARD_LOAD_STATE" ]; then
  echo "NOTICE: $DASHBOARD_UNIT is not found; preserving absence"
else
  die "unexpected $DASHBOARD_UNIT LoadState=$DASHBOARD_LOAD_STATE"
fi

# Ensure bridge service is restarted correctly. Keep the activation rollback trap
# installed through this restart so a bridge failure restores the previous release.
#
# --- #1303 bridge activation begin ---
# The bridge is Type=oneshot, so `systemctl restart` blocks until its first
# post-flip cycle ends and returns that cycle's exit status. Since #1280 a cycle
# whose model call never returned exits EXIT_EXECUTOR_LLM_ERROR (3) so the
# failure streak stays honest; on 2026-09-05 06:38 a transient gateway 404 in
# that first cycle turned into "Failed to start … status=3", the ERR trap rolled
# `a64470e5` back, and the release carrying #1280 became un-deployable by the
# mechanism its own fix trips.
#
# Arm 1 of #1303, deliberately: distinguish the exit status here. Status 3 is
# "this cycle could not reach the gateway" — the process imported, started, ran
# a cycle and recorded it `blocked` — not "this release cannot run"; every other
# non-zero status or signal still is, and still rolls back. Not arm 2 (retry the
# activation once): it costs a full cycle and a second model call, and the
# health gate below would still need this same distinction for the journal and
# streak it reads. Not arm 3 (exit 0 and read the outcome instead): that moves
# the signal #1280 put on the exit status, and the streak semantics stay exactly
# as they are. The lib is the single home of the value, pinned to
# bridge.EXIT_EXECUTOR_LLM_ERROR by test.
if [ "$VERIFY_ONLY" -eq 0 ]; then
  . "$RELEASE_DIR/host/eeepc/scripts/lib_bridge_exit.sh"
  BRIDGE_RESTART_RC=0
  sudo systemctl restart eeepc-self-evolving-subagent-bridge.service || BRIDGE_RESTART_RC=$?
  BRIDGE_RESULT="$(systemctl show -p Result --value eeepc-self-evolving-subagent-bridge.service 2>/dev/null || true)"
  BRIDGE_EXIT_STATUS="$(systemctl show -p ExecMainStatus --value eeepc-self-evolving-subagent-bridge.service 2>/dev/null || true)"
  case "$(classify_bridge_run "$BRIDGE_RESTART_RC" "$BRIDGE_RESULT" "$BRIDGE_EXIT_STATUS")" in
    ok) ;;
    transport)
      echo "[remote] bridge first post-flip cycle exited EXIT_EXECUTOR_LLM_ERROR ($BRIDGE_EXIT_EXECUTOR_LLM_ERROR): transport failure recorded as blocked, not an activation failure (#1303); release stays active" ;;
    *)
      die "bridge activation failed (restart rc=$BRIDGE_RESTART_RC Result=$BRIDGE_RESULT ExecMainStatus=$BRIDGE_EXIT_STATUS)" ;;
  esac
fi
# --- #1303 bridge activation end ---
trap - ERR

REMOTE

log "cleaning up local archive"
if [ "$VERIFY_ONLY" -eq 0 ]; then
  run rm -f "${ARCHIVE:-}"
fi

# 4. Post-deploy health gate
if [ "$NO_HEALTH_GATE" -eq 1 ] || [ "$DRY_RUN" -eq 1 ] || [ "$VERIFY_ONLY" -eq 1 ]; then
  log "Health gate skipped."
  log "=== Deploy complete ==="
  exit 0
fi

log "Waiting up to ${HEALTH_TIMEOUT}m for post-deploy health signals..."
END_TIME=$(( SECONDS + HEALTH_TIMEOUT * 60 ))
FLIP_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
# The ` UTC` suffix is load-bearing. `journalctl --since` parses a bare
# timestamp in the HOST's local time, and the host runs MSK (UTC+3); `--utc`
# only changes how journalctl formats its output. Without it the window opened
# three hours before the deploy, so both FAIL branches below scanned pre-deploy
# history and could roll back a healthy release over a crash that predated it.
# Verified on the host: `--since '2026-09-01 21:35:36 UTC'` starts at 21:35:36,
# the bare string at 18:36:49.
FLIP_JOURNAL_TS="$(date -u +'%Y-%m-%d %H:%M:%S') UTC"

# #1163: the positive verdicts. Neither is the word PASS, on purpose — that
# label meant "a terminal ledger row appeared" and certified a
# `skipped-duplicate, verdict: reject` cycle on 2026-09-02; a weaker or
# different claim must not inherit its authority.
#   CLEAN-EXIT  an invocation that began after the flip ran to exit 0:
#               systemd's `Finished <unit>` line (logged only on success), or
#               state/bridge/exit_streak.json (#1200) with last_success_ts
#               after the flip and consecutive_failures 0.
#   NO-CRASH    an invocation began after the flip (`Starting <unit>`), no
#               FAIL branch fired for NO_CRASH_HOLD seconds since, and it has
#               not finished yet (a long cycle). Weaker: "did not crash yet".
# Not used as a positive signal, measured on the 2026-09-01 crash loop: the
# ledger's `started`/`dedup` rows (140 each, written before the code that
# broke), "any ledger row" (294 in the window), or a silence detector (the
# max inter-row gap in 51 days is 101.8 min; the loop never goes quiet).
# Honest residual, unchanged from the old PASS: a release that runs, exits 0
# and produces nothing useful passes here too.
INVOKED_AT=""
BRIDGE_UNIT="eeepc-self-evolving-subagent-bridge.service"
EXIT_STREAK_PATH="/var/lib/eeepc-agent/self-evolving-agent/state/bridge/exit_streak.json"

# Poll at least once before honouring the timeout. With `while [ $SECONDS -le
# $END_TIME ]` and a zero timeout the loop could exit without checking anything,
# so whether the gate ran at all depended on how fast the shell got here.
while :; do
  # Look for a real traceback from the service, not for words in the log text.
  # The bridge logs the full arguments of an edit_file tool call at DEBUG, so
  # the source of whatever file a cycle is editing reaches the journal; a bare
  # `error:` substring then matched any ordinary CLI error message in that
  # source and rolled back a healthy release. Drop the `error:`/`exception:`
  # substrings, skip DEBUG lines, and anchor the traceback to the start of the
  # message (after journalctl's `host process[pid]:` prefix).
  TRACEBACK_LINE=$(ssh "ozand@${HOST}" "sudo journalctl -u eeepc-self-evolving-subagent-bridge.service --utc --since \"$FLIP_JOURNAL_TS\" --no-pager | grep -v ' DEBUG ' | grep -E ': Traceback \(most recent call last\):' || true")
  if [ -n "$TRACEBACK_LINE" ]; then
    log "FAIL: Traceback or error observed in journal:"
    echo "$TRACEBACK_LINE"
    rollback_release
    log "Rollback complete."
    exit 1
  fi
  
  # A clean SIGTERM is not a crash. The bridge is timer-driven, so systemd ends
  # every run with `code=killed, status=15/TERM`, and this deploy's own
  # `systemctl restart` logs the same line. A bare `status=[1-9]` matches the
  # "15" in "15/TERM", so once #1155 made these queries readable the pattern
  # matched routine operation and rolled back every deploy. Count only a
  # non-zero exit, a core dump, or a kill by a fault signal.
  # #1303: `code=exited, status=3/` is EXIT_EXECUTOR_LLM_ERROR — a cycle that
  # could not reach its model, recorded `blocked` (#1280). Transport, not a
  # crash of the release; say so once and keep polling for a clean run. The
  # `grep -v` runs on the host inside the same pipeline so the test replay of
  # this command exercises exactly this filter.
  MAIN_PID_EXIT=$(ssh "ozand@${HOST}" "sudo journalctl -u eeepc-self-evolving-subagent-bridge.service --utc --since \"$FLIP_JOURNAL_TS\" --no-pager | grep -iE 'main process exited, (code=exited, status=[1-9]|code=dumped|code=killed, status=(4|6|8|11)/)' | grep -vE 'code=exited, status=${BRIDGE_EXIT_EXECUTOR_LLM_ERROR}/' || true")
  if [ -n "$MAIN_PID_EXIT" ]; then
    log "FAIL: Bridge process crashed:"
    echo "$MAIN_PID_EXIT"
    rollback_release
    exit 1
  fi
  if [ -z "${TRANSPORT_EXIT_LOGGED:-}" ]; then
    TRANSPORT_EXIT=$(ssh "ozand@${HOST}" "sudo journalctl -u eeepc-self-evolving-subagent-bridge.service --utc --since \"$FLIP_JOURNAL_TS\" --no-pager | grep -iE 'main process exited, code=exited, status=${BRIDGE_EXIT_EXECUTOR_LLM_ERROR}/' || true")
    if [ -n "$TRANSPORT_EXIT" ]; then
      log "Health gate: bridge exited EXIT_EXECUTOR_LLM_ERROR (${BRIDGE_EXIT_EXECUTOR_LLM_ERROR}) after the flip — a transport failure recorded as blocked (#1280), not a release failure (#1303); release stays active, waiting for a clean run: $TRANSPORT_EXIT"
      TRANSPORT_EXIT_LOGGED=1
    fi
  fi

  # ORDER MATTERS. Both FAIL branches above run before every positive verdict
  # below, in the same poll iteration, and that ordering is the guard: during
  # the 2026-09-01 crash loop the post-flip journal held 6 `Finished` lines
  # against 139 `Failed with result 'exit-code'` — a few invocations did
  # complete — so a `Finished` line alone does not prove a release sound.
  # Hoisting the positive verdicts above the FAIL checks, or evaluating them
  # concurrently, would re-admit exactly that release, and only during an
  # outage. tests/test_deploy_release.py::test_r pins the order.
  # #1200's recorder: a failure recorded after the flip is a crash the journal
  # grep above may not have seen yet (or a signal/OOM once the ExecStopPost
  # drop-in is installed); a success recorded after the flip is a full run.
  # #1259: an unreadable recorder file is not "no signal yet". It means the
  # CLEAN-EXIT / FAIL verdicts that come from this file can never arrive, and
  # the gate will run to its timeout on the journal alone. Say so once, with
  # the exit status and stderr, instead of polling silently. The DECISION is
  # unchanged (the journal verdicts below still decide; a missing file still
  # counts as no signal) — only what is said changed.
  STREAK_ERR="$(mktemp)"
  if STREAK_RAW=$(ssh "ozand@${HOST}" "sudo cat $EXIT_STREAK_PATH" 2>"$STREAK_ERR"); then
    :
  else
    STREAK_RC=$?
    STREAK_RAW=""
    if [ -z "${STREAK_UNREADABLE_LOGGED:-}" ]; then
      log "Health gate: unreadable: $EXIT_STREAK_PATH (exit $STREAK_RC: $(tr '\n' ' ' <"$STREAK_ERR" | tail -c 200)) — the exit recorder's CLEAN-EXIT/FAIL signals cannot arrive; deciding on the journal alone."
      STREAK_UNREADABLE_LOGGED=1
    fi
  fi
  rm -f "$STREAK_ERR"
  if [ -n "$STREAK_RAW" ]; then
    STREAK_FAIL_TS=$(echo "$STREAK_RAW" | grep -o '"last_failure_ts": "[^"]*"' | cut -d'"' -f4 || true)
    STREAK_OK_TS=$(echo "$STREAK_RAW" | grep -o '"last_success_ts": "[^"]*"' | cut -d'"' -f4 || true)
    STREAK_N=$(echo "$STREAK_RAW" | grep -o '"consecutive_failures": [0-9]*' | grep -o '[0-9]*$' || true)
    STREAK_EXIT=$(echo "$STREAK_RAW" | grep -o '"last_exit_status": [0-9]*' | grep -o '[0-9]*$' || true)
    # #1303: a failure row is only a live verdict while consecutive_failures is
    # non-zero; the recorder resets it on the next clean run, and a transport
    # failure followed by a clean cycle is the expected post-flip sequence now.
    # A real crash in the window is still caught by the journal grep above.
    if [ -n "$STREAK_FAIL_TS" ] && [[ "$STREAK_FAIL_TS" > "$FLIP_TS" ]] && [ "${STREAK_N:-0}" != "0" ]; then
      # #1303: the recorder is meant to advance on a transport failure (#1280)
      # — that is the streak being honest, not the release being broken. Only
      # a failure recorded with any OTHER exit status rolls back.
      if [ "$STREAK_EXIT" = "$BRIDGE_EXIT_EXECUTOR_LLM_ERROR" ]; then
        if [ -z "${TRANSPORT_STREAK_LOGGED:-}" ]; then
          log "Health gate: exit recorder shows a transport failure after the flip (last_exit_status=$STREAK_EXIT = EXIT_EXECUTOR_LLM_ERROR, consecutive_failures=${STREAK_N:-?}); the streak advanced as #1280 intends, not a release failure (#1303); waiting for a clean run."
          TRANSPORT_STREAK_LOGGED=1
        fi
      else
        log "FAIL: bridge exit recorder shows a failure after the flip (consecutive_failures=${STREAK_N:-?}, last_failure_ts=$STREAK_FAIL_TS):"
        echo "$STREAK_RAW" | grep -E '"last_(error|where|exit_status)"' || true
        rollback_release
        exit 1
      fi
    fi
    if [ -n "$STREAK_OK_TS" ] && [[ "$STREAK_OK_TS" > "$FLIP_TS" ]] && [ "${STREAK_N:-0}" = "0" ]; then
      log "Health gate: CLEAN-EXIT. Bridge run recorded exit 0 after the flip (exit_streak.json last_success_ts=$STREAK_OK_TS, consecutive_failures=0)."
      log "=== Deploy complete ==="
      exit 0
    fi
  fi

  # systemd logs `Finished <unit>` only when a oneshot run exits 0; every line
  # in this window postdates the flip (see FLIP_JOURNAL_TS above).
  FINISHED_LINE=$(ssh "ozand@${HOST}" "sudo journalctl -u $BRIDGE_UNIT --utc --since \"$FLIP_JOURNAL_TS\" --no-pager | grep -E 'systemd\[1\]: Finished $BRIDGE_UNIT' | tail -n 1 || true")
  if [ -n "$FINISHED_LINE" ]; then
    log "Health gate: CLEAN-EXIT. Bridge run finished after the flip: $FINISHED_LINE"
    log "=== Deploy complete ==="
    exit 0
  fi

  if [ -z "$INVOKED_AT" ]; then
    STARTING_LINE=$(ssh "ozand@${HOST}" "sudo journalctl -u $BRIDGE_UNIT --utc --since \"$FLIP_JOURNAL_TS\" --no-pager | grep -E 'systemd\[1\]: Starting $BRIDGE_UNIT' | head -n 1 || true")
    if [ -n "$STARTING_LINE" ]; then
      INVOKED_AT=$SECONDS
      log "Bridge invoked after the flip; holding ${NO_CRASH_HOLD}s for a crash before the weaker verdict: $STARTING_LINE"
    fi
  elif [ $(( SECONDS - INVOKED_AT )) -ge "$NO_CRASH_HOLD" ]; then
    log "Health gate: NO-CRASH. Bridge invoked after the flip and no crash for ${NO_CRASH_HOLD}s; the run has not finished yet, so this is weaker than CLEAN-EXIT."
    log "=== Deploy complete ==="
    exit 0
  fi
  
  # An if-block, not `[ ... ] && break`: this script runs under `set -e`, where
  # a false `&&` list exits with status 1 and would kill the deploy on the very
  # first iteration that decides to keep polling.
  if [ $SECONDS -gt $END_TIME ]; then
    break
  fi
  sleep 10
done

log "Health gate: UNKNOWN. Timeout reached without a post-flip invocation, clean exit, or crash."
log "Do not roll back; an active cycle may still be running."
log "=== Deploy complete ==="
exit 0