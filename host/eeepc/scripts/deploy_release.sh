#!/usr/bin/env bash
# =============================================================================
# eeepc deploy: push a new code release to the running host and activate it
#
# Usage (from dev machine, not on eeepc):
#   bash host/eeepc/scripts/deploy_release.sh [--host eeepc] [--dry-run]
#
# What it does:
#   1. Bundles current repo HEAD into a timestamped release archive
#   2. Sends it to eeepc via scp
#   3. On eeepc: extracts, creates venv, symlinks current, restarts agent
#
# Prerequisites on dev machine:
#   - ssh access to eeepc (key-based)
#   - gh CLI authenticated
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOST="eeepc"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --host)    HOST="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

log()  { echo "[deploy] $*"; }
run()  { if [[ $DRY_RUN -eq 1 ]]; then echo "[DRY] $*"; else "$@"; fi; }

COMMIT=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RELEASE_NAME="${TIMESTAMP}-canonical-${COMMIT}"
ARCHIVE="/tmp/eeebot-release-${RELEASE_NAME}.tar.gz"

log "repo:    $REPO_ROOT"
log "commit:  $COMMIT ($BRANCH)"
log "release: $RELEASE_NAME"
log "host:    $HOST"

# 1. Create archive from git HEAD (excludes .git, .venv, __pycache__)
log "creating archive..."
run git -C "$REPO_ROOT" archive --format=tar.gz --prefix="${RELEASE_NAME}/" HEAD \
  -o "$ARCHIVE"
log "archive: $ARCHIVE ($(du -sh "$ARCHIVE" | cut -f1))"

# 2. Copy to host
REMOTE_ARCHIVE="/tmp/$(basename "$ARCHIVE")"
log "uploading to $HOST:$REMOTE_ARCHIVE..."
run scp "$ARCHIVE" "ozand@${HOST}:${REMOTE_ARCHIVE}"

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
# Presets are non-secret templates checked into the repo
# but, unlike instance env files, kept current on EVERY deploy (not just
# first install) — a new/updated preset should reach the host without a
# full install.sh re-run. apply_preset.sh reads from this canonical /etc
# location, never from the release tree directly, so a preset survives
# release rollovers.
sudo mkdir -p /etc/eeepc-agent/presets
sudo cp "$RELEASE_DIR/host/eeepc/etc/presets/"*.env /etc/eeepc-agent/presets/ 2>/dev/null || true

echo "[remote] migrating goal priorities to derived_priorities.json (#944)"
STATE_DIR=/var/lib/eeepc-agent/self-evolving-agent/state
sudo mkdir -p "$STATE_DIR/goals"
# #944: goals.md ships in the release tree as the immutable operator charter
# (read from /opt/.../current/goals.md by both LLM actors). Priority entries
# are now solely owned by state/goals/derived_priorities.json.
#
# Migration is fail-closed, atomic, and runs BEFORE the release becomes current.
# Existing derived priorities are validated and preserved; legacy priority numbers
# are added only when absent, so repeated deploys are idempotent.
DERIVED="$STATE_DIR/goals/derived_priorities.json"
GOAL_TEXT="$STATE_DIR/goals/goal_text.json"
if [ -f "$GOAL_TEXT" ]; then
  sudo python3 "$RELEASE_DIR/host/eeepc/scripts/migrate_goal_priorities.py" "$GOAL_TEXT" "$DERIVED"
  sudo chown eeepc-agent:eeepc-agent "$DERIVED"
elif [ ! -f "$DERIVED" ]; then
  echo "[remote] no legacy or derived priorities found; goal_review will mint from the charter"
fi

echo "[remote] fixing ownership and permissions on release"
# #875 RED1 fix (opus-review): root:root, NOT eeepc-agent:eeepc-agent. #880
# already proved the runtime uid never writes into /opt (ProtectSystem=strict
# makes /opt read-only inside every app-lane sandbox regardless of on-disk
# ownership, and PYTHONDONTWRITEBYTECODE=1 stops even a stray .pyc) — so the
# runtime uid only ever needs READ+EXEC here, which world-read from the
# release tar/umask already provides. The root-run promotion verifier
# (host/eeepc/libexec/eeepc_promotion_verifier.py) imports straight out of
# this tree AS ROOT; if it were eeepc-agent-owned, the runtime uid could
# plant/mutate a module the verifier would then import with root privilege —
# a straightforward root RCE. The verifier independently fails closed if it
# ever finds this tree not root-owned (see its ownership self-check), so
# this chown is not just defense-in-depth, it is the thing that check relies
# on being true.
sudo chown -R root:root "$RELEASE_DIR" "$VENV_BASE"

# YELLOW-1 fix (opus-review round 2): the RELEASE CONTENTS being root:root
# is not enough on its own — every directory the `current`/`.venv` symlinks
# themselves LIVE IN was still eeepc-agent-owned, meaning the runtime uid
# (the SAME uid the instance's subagent runs as) could delete+recreate
# `current` (or `.venv`) itself and re-point it at attacker-controlled
# content — relying entirely on #880's ProtectSystem=strict sandbox to stop
# that, which does not protect this root verifier itself. Root:root every
# directory in the chain (non-recursively — the release CONTENTS already
# got -R above; this is just the path scaffolding around it), plus the
# symlinks' own ownership (-h, so `chown` doesn't follow them).
sudo chown root:root /opt/eeepc-agent
sudo chown root:root /opt/eeepc-agent/runtimes
sudo chown root:root /opt/eeepc-agent/runtimes/self-evolving-agent
sudo chown root:root "$RELEASES_DIR"
sudo chown -h root:root "$RELEASE_DIR/.venv"
sudo chown root:root /opt/eeepc-agent/venv

# #875 (live-rollout fix): the verifier's fail-closed ownership self-check
# refuses to import the release unless it is root-owned AND has NO group/other
# write bit (`mode & 0o022 == 0`). The release tar/umask can leave directories
# group-writable (0775), which trips that check even when the tree is root:root.
# Strip group/other write so the check passes; read+exec (what the runtime uid
# needs) is untouched.
sudo chmod -R go-w "$RELEASE_DIR"

# Post-hoc critical ownership verification BEFORE activating symlink (#1037)
if [ "$(stat -c '%u:%g' "$RELEASE_DIR")" != "0:0" ]; then
  echo "CRITICAL: $RELEASE_DIR is not owned by root:root" >&2
  exit 1
fi
if [ "$(stat -c '%u:%g' /opt/eeepc-agent/runtimes/self-evolving-agent)" != "0:0" ]; then
  echo "CRITICAL: /opt/eeepc-agent/runtimes/self-evolving-agent is not owned by root:root" >&2
  exit 1
fi

echo "[remote] updating current symlink"
# Symlink activation is only performed once release ownership and permissions are verified (#1037)
sudo ln -sfn "$RELEASE_DIR" /opt/eeepc-agent/runtimes/self-evolving-agent/current
sudo chown -h root:root /opt/eeepc-agent/runtimes/self-evolving-agent/current

if [ "$(stat -c '%u:%g' /opt/eeepc-agent/runtimes/self-evolving-agent/current)" != "0:0" ]; then
  echo "CRITICAL: /opt/eeepc-agent/runtimes/self-evolving-agent/current is not owned by root:root" >&2
  exit 1
fi
# Since #601 the bridge unit uses this same current symlink (PYTHONPATH from
# the unit; ExecStart runs -m nanobot.runtime.bridge).
echo "[remote] goals.md available at: $RELEASE_DIR/goals.md"

# #875 (live-rollout fix): the root-owned promoted tree. Normally created by
# install.sh, but a host updated via deploy alone never runs it — and the
# verifier unit's ReadWritePaths=/var/lib/eeepc-promoted makes systemd fail
# the unit (226/NAMESPACE) if the path is absent. Create it here idempotently,
# root-owned so the eeepc-agent-uid loader can read but never write it.
sudo mkdir -p /var/lib/eeepc-promoted
sudo chown root:eeepc-agent /var/lib/eeepc-promoted
sudo chmod 0755 /var/lib/eeepc-promoted

# #925: the validator harness's own bookkeeping dir. Same 226/NAMESPACE class
# as the block above (its unit carves this path in as ReadWritePaths), so
# create it here idempotently too — agent-owned, since the harness writes it.
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
# Bridge is NOT copied since #601 — its unit runs `-m nanobot.runtime.bridge`
# straight from the release; only auxiliary libexec scripts are synced
# (this now includes eeepc_promotion_verifier.py, #875).
sudo cp "$RELEASE_DIR/host/eeepc/libexec/"*.py /usr/local/libexec/
sudo rm -f /usr/local/libexec/eeepc-self-evolving-subagent-bridge.py
# NOTE: was previously scoped to eeepc-self-evolving-*.py, which silently
# skipped eeepc_promotion_verifier.py (#875) — broadened to every libexec
# script so a new file here is never quietly left non-executable.
sudo chmod +x /usr/local/libexec/*.py

echo "[remote] purging retired ghost units (#1037)"
# Retire eeepc-network-fallback if present on host (#1037).  Use LoadState,
# not list-unit-files: the latter returns success even when no unit matches.
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

# Post-verify that ghost units are completely unloaded, inactive, and absent.
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

# Timer sync policy (#1037):
# 1. Essential loop timers (eeepc-promotion-verifier.timer) MUST be enabled and active.
# 2. Auxiliary service timers:
#    - If already enabled: restart/keep enabled, verify enabled + active.
#    - If disabled/masked administratively by operator: do not silently resurrect without warning.
#      Preserve and post-verify disabled/masked + inactive.
#    - If not yet enabled (new unit/preset): enable --now, verify enabled + active.
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
    # Not yet enabled or static/preset: enable if required or standard service
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


echo "[remote] current release: $(readlink /opt/eeepc-agent/runtimes/self-evolving-agent/current)"
echo "[remote] done — commit $COMMIT"
REMOTE

log "cleaning up local archive"
run rm -f "$ARCHIVE"

log ""
log "=== Deploy complete ==="
log "Release: $RELEASE_NAME"
log "To verify:"
log "  ssh ozand@$HOST 'sudo journalctl -u eeepc-self-evolving-subagent-bridge.service --since \"1 min ago\" --no-pager'"
log "  ssh ozand@$HOST 'sudo systemctl status eeepc-self-evolving-subagent-bridge.service'"
