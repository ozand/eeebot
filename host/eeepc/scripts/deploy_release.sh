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

echo "[remote] updating current symlink"
sudo ln -sfn "$RELEASE_DIR" /opt/eeepc-agent/runtimes/self-evolving-agent/current

# Since #601 the bridge unit uses the same `current` symlink as everything else
# (PYTHONPATH from the unit; ExecStart runs `-m nanobot.runtime.bridge`).

echo "[remote] syncing operator presets (#906)"
# Presets are non-secret templates checked into the repo
# but, unlike instance env files, kept current on EVERY deploy (not just
# first install) — a new/updated preset should reach the host without a
# full install.sh re-run. apply_preset.sh reads from this canonical /etc
# location, never from the release tree directly, so a preset survives
# release rollovers.
sudo mkdir -p /etc/eeepc-agent/presets
sudo cp "$RELEASE_DIR/host/eeepc/etc/presets/"*.env /etc/eeepc-agent/presets/ 2>/dev/null || true

echo "[remote] seeding goal_text.json into state/goals/"
STATE_DIR=/var/lib/eeepc-agent/self-evolving-agent/state
sudo mkdir -p "$STATE_DIR/goals"
sudo cp "$RELEASE_DIR/host/eeepc/etc/goal_text.json" "$STATE_DIR/goals/goal_text.json"
sudo chown eeepc-agent:eeepc-agent "$STATE_DIR/goals/goal_text.json"
echo "[remote] goal_text.json seeded: $(wc -c < $STATE_DIR/goals/goal_text.json) bytes"

echo "[remote] fixing ownership"
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
sudo chown -R root:root "$RELEASE_DIR" "$VENV_BASE" 2>/dev/null || true

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
sudo chown root:root /opt/eeepc-agent 2>/dev/null || true
sudo chown root:root /opt/eeepc-agent/runtimes 2>/dev/null || true
sudo chown root:root /opt/eeepc-agent/runtimes/self-evolving-agent 2>/dev/null || true
sudo chown root:root "$RELEASES_DIR" 2>/dev/null || true
sudo chown -h root:root /opt/eeepc-agent/runtimes/self-evolving-agent/current 2>/dev/null || true
sudo chown -h root:root "$RELEASE_DIR/.venv" 2>/dev/null || true
sudo chown root:root /opt/eeepc-agent/venv 2>/dev/null || true

# #875 (live-rollout fix): the verifier's fail-closed ownership self-check
# refuses to import the release unless it is root-owned AND has NO group/other
# write bit (`mode & 0o022 == 0`). The release tar/umask can leave directories
# group-writable (0775), which trips that check even when the tree is root:root.
# Strip group/other write so the check passes; read+exec (what the runtime uid
# needs) is untouched.
sudo chmod -R go-w "$RELEASE_DIR" 2>/dev/null || true

# #875 (live-rollout fix): the root-owned promoted tree. Normally created by
# install.sh, but a host updated via deploy alone never runs it — and the
# verifier unit's ReadWritePaths=/var/lib/eeepc-promoted makes systemd fail
# the unit (226/NAMESPACE) if the path is absent. Create it here idempotently,
# root-owned so the eeepc-agent-uid loader can read but never write it.
sudo mkdir -p /var/lib/eeepc-promoted 2>/dev/null || true
sudo chown root:eeepc-agent /var/lib/eeepc-promoted 2>/dev/null || true
sudo chmod 0755 /var/lib/eeepc-promoted 2>/dev/null || true

echo "[remote] syncing libexec scripts from release"
# Bridge is NOT copied since #601 — its unit runs `-m nanobot.runtime.bridge`
# straight from the release; only auxiliary libexec scripts are synced
# (this now includes eeepc_promotion_verifier.py, #875).
sudo cp "$RELEASE_DIR/host/eeepc/libexec/"*.py /usr/local/libexec/ 2>/dev/null || true
sudo rm -f /usr/local/libexec/eeepc-self-evolving-subagent-bridge.py
# NOTE: was previously scoped to eeepc-self-evolving-*.py, which silently
# skipped eeepc_promotion_verifier.py (#875) — broadened to every libexec
# script so a new file here is never quietly left non-executable.
sudo chmod +x /usr/local/libexec/*.py 2>/dev/null || true

echo "[remote] syncing systemd units + reloading"
sudo cp "$RELEASE_DIR/host/eeepc/systemd/"*.service "$RELEASE_DIR/host/eeepc/systemd/"*.timer /etc/systemd/system/ 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now eeepc-promotion-verifier.timer 2>/dev/null || true

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
