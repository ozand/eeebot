#!/usr/bin/env bash
# =============================================================================
# eeepc deploy: push a new code release to the running host and activate it
#
# Usage (from dev machine, not on eeepc):
#   bash host/eeepc/scripts/deploy_release.sh [--host eeepc] [--dry-run] [--ref <sha>] [--health-timeout <min>] [--no-health-gate] [--allow-dirty]
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
HOST="eeepc"
DRY_RUN=0
REF=""
HEALTH_TIMEOUT=15
NO_HEALTH_GATE=0
ALLOW_DIRTY=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --host)           HOST="$2"; shift 2 ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --ref)            REF="$2"; shift 2 ;;
    --health-timeout) HEALTH_TIMEOUT="$2"; shift 2 ;;
    --no-health-gate) NO_HEALTH_GATE=1; shift ;;
    --allow-dirty)    ALLOW_DIRTY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

log()  { echo "[deploy] $*"; }
run()  { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY] $*"; else "$@"; fi; }

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
ARCHIVE="/tmp/eeebot-release-${RELEASE_NAME}.tar.gz"

log "repo:    $REPO_ROOT"
log "commit:  $COMMIT ($SUBJECT) ($BRANCH)"
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

# Resolve PREV_RELEASE so we can roll back if needed
PREV_RELEASE_PATH=$(ssh "ozand@${HOST}" "readlink /opt/eeepc-agent/runtimes/self-evolving-agent/current || true")

# 3. Extract, build venv, update symlink, restart
log "installing on $HOST..."
run ssh "ozand@${HOST}" bash -s -- "$REMOTE_ARCHIVE" "$RELEASE_NAME" "$COMMIT" <<'REMOTE'
set -euo pipefail
ARCHIVE="$1"
RELEASE_NAME="$2"
COMMIT="$3"

RELEASES_DIR=/opt/eeepc-agent/runtimes/self-evolving-agent/releases
RELEASE_DIR="$RELEASES_DIR/$RELEASE_NAME"
VENV_BASE=/opt/eeepc-agent/runtimes/self-evolving-agent/venv

echo "[remote] extracting to $RELEASE_DIR"
sudo mkdir -p "$RELEASES_DIR"
cd /tmp
sudo tar xzf "$ARCHIVE" -C "$RELEASES_DIR"
echo "$COMMIT" | sudo tee "$RELEASE_DIR/SOURCE_COMMIT" > /dev/null

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
  echo "CRITICAL: $RELEASE_DIR is not owned by root:root" >&2
  exit 1
fi
if [ "$(stat -c '%u:%g' /opt/eeepc-agent/runtimes/self-evolving-agent)" != "0:0" ]; then
  echo "CRITICAL: /opt/eeepc-agent/runtimes/self-evolving-agent is not owned by root:root" >&2
  exit 1
fi

echo "[remote] updating current symlink"
sudo ln -sfn "$RELEASE_DIR" /opt/eeepc-agent/runtimes/self-evolving-agent/current
sudo chown -h root:root /opt/eeepc-agent/runtimes/self-evolving-agent/current

if [ "$(stat -c '%u:%g' /opt/eeepc-agent/runtimes/self-evolving-agent/current)" != "0:0" ]; then
  echo "CRITICAL: /opt/eeepc-agent/runtimes/self-evolving-agent/current is not owned by root:root" >&2
  exit 1
fi

echo "[remote] goals.md available at: $RELEASE_DIR/goals.md"

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

for ghost_unit in eeepc-network-fallback.timer eeepc-network-fallback.service; do
  ghost_load_state="$(systemctl show "$ghost_unit" -p LoadState --value)"
  if [ "$ghost_load_state" != "not-found" ]; then
    echo "CRITICAL: $ghost_unit remains loaded after purge (state: $ghost_load_state)" >&2
    exit 1
  fi
  if systemctl is-active --quiet "$ghost_unit"; then
    echo "CRITICAL: $ghost_unit is still active after purge" >&2
    exit 1
  fi
done
if [ -e /etc/systemd/system/eeepc-network-fallback.timer ] || [ -e /etc/systemd/system/eeepc-network-fallback.service ]; then
  echo "CRITICAL: ghost unit files still present on disk" >&2
  exit 1
fi

echo "[remote] syncing systemd units + reloading"
sudo cp "$RELEASE_DIR/host/eeepc/systemd/"*.service "$RELEASE_DIR/host/eeepc/systemd/"*.timer /etc/systemd/system/
sudo systemctl daemon-reload

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
    sudo systemctl restart "$timer"
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

# Ensure bridge service is restarted correctly
sudo systemctl restart eeepc-self-evolving-subagent-bridge.service

REMOTE

log "cleaning up local archive"
run rm -f "$ARCHIVE"

# 4. Post-deploy health gate
if [ "$NO_HEALTH_GATE" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
  log "Health gate skipped."
  log "=== Deploy complete ==="
  exit 0
fi

log "Waiting up to ${HEALTH_TIMEOUT}m for post-deploy health signals..."
END_TIME=$(( SECONDS + HEALTH_TIMEOUT * 60 ))
FLIP_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
FLIP_JOURNAL_TS=$(date -u +"%Y-%m-%d %H:%M:%S")

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
    log "Rolling back to $PREV_RELEASE_PATH..."
    ssh "ozand@${HOST}" "sudo ln -sfn \"$PREV_RELEASE_PATH\" /opt/eeepc-agent/runtimes/self-evolving-agent/current && sudo systemctl restart eeepc-self-evolving-subagent-bridge.service"
    log "Rollback complete."
    exit 1
  fi
  
  # A clean SIGTERM is not a crash. The bridge is timer-driven, so systemd ends
  # every run with `code=killed, status=15/TERM`, and this deploy's own
  # `systemctl restart` logs the same line. A bare `status=[1-9]` matches the
  # "15" in "15/TERM", so once #1155 made these queries readable the pattern
  # matched routine operation and rolled back every deploy. Count only a
  # non-zero exit, a core dump, or a kill by a fault signal.
  MAIN_PID_EXIT=$(ssh "ozand@${HOST}" "sudo journalctl -u eeepc-self-evolving-subagent-bridge.service --utc --since \"$FLIP_JOURNAL_TS\" --no-pager | grep -iE 'main process exited, (code=exited, status=[1-9]|code=dumped|code=killed, status=(4|6|8|11)/)' || true")
  if [ -n "$MAIN_PID_EXIT" ]; then
    log "FAIL: Bridge process crashed:"
    echo "$MAIN_PID_EXIT"
    log "Rolling back to $PREV_RELEASE_PATH..."
    ssh "ozand@${HOST}" "sudo ln -sfn \"$PREV_RELEASE_PATH\" /opt/eeepc-agent/runtimes/self-evolving-agent/current && sudo systemctl restart eeepc-self-evolving-subagent-bridge.service"
    exit 1
  fi

  OUTCOME_RAW=$(ssh "ozand@${HOST}" "sudo cat /var/lib/eeepc-agent/self-evolving-agent/state/ledger/cycles.jsonl 2>/dev/null | grep '\"phase\": \"outcome\"' | tail -n 1 || true")
  if [ -n "$OUTCOME_RAW" ]; then
    # Very rudimentary bash grep to extract the timestamp string
    OUTCOME_TS=$(echo "$OUTCOME_RAW" | grep -o '"ts": "[^"]*"' | cut -d'"' -f4)
    if [[ "$OUTCOME_TS" > "$FLIP_TS" ]]; then
      log "Health gate: PASS. Terminal outcome observed: $OUTCOME_RAW"
      log "=== Deploy complete ==="
      exit 0
    fi
  fi
  
  # An if-block, not `[ ... ] && break`: this script runs under `set -e`, where
  # a false `&&` list exits with status 1 and would kill the deploy on the very
  # first iteration that decides to keep polling.
  if [ $SECONDS -gt $END_TIME ]; then
    break
  fi
  sleep 10
done

log "Health gate: UNKNOWN. Timeout reached without observing definitive signal."
log "Do not roll back; an active cycle may still be running."
log "=== Deploy complete ==="
exit 0